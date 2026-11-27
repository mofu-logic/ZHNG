"""売買・マスタ・購入候補のサービス層。

画面と JSON API (/api/v1) が共有する書き込み／読み出しロジック。
導出ロジック（約定状態、売却損益、候補の到達判定）はこの層に
1か所だけ置く。サービス層は割らず、ここにまとめたままにする。

db.py と config.py にのみ依存する（循環 import を作らない）。
関数は接続済みの conn を受け取り、呼び出し側がコミットする。
"""

from datetime import datetime
from decimal import Decimal

from app.config import JST, TRADE_STATUSES


class LinkageError(Exception):
    """A trade cannot be linked to holdings as requested (the
    caller turns this into a 422 and nothing is registered)."""


def _upsert_stock(conn, code, name):
    if code and name:
        conn.execute(
            "INSERT INTO stocks(code, name) VALUES (%s, %s)"
            " ON CONFLICT(code) DO NOTHING",
            (code, name),
        )


def upsert_stock_info(conn, code, name, market):
    """Register a stock-master row from fetched info (name and
    market), filling in only what an existing row is missing."""
    conn.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, %s)"
        " ON CONFLICT(code) DO UPDATE SET"
        " name = COALESCE(EXCLUDED.name, stocks.name),"
        " market = COALESCE(EXCLUDED.market, stocks.market)",
        (code, name, market),
    )


# ------------------------------------------------------------ holdings linkage
# A trade that names an account type moves the holding for
# (code, account_type) when it registers, and every such move is
# recorded in holding_events with the before/after state. Cancelling
# or deleting the trade posts a reversal event that restores the
# recorded "before" state exactly. Trades without an account type
# (including every row from before this feature) never link.


def _holding_row(conn, code, account_type):
    return conn.execute(
        "SELECT * FROM holdings WHERE code = %s AND account_type = %s",
        (code, account_type),
    ).fetchone()


def _record_event(conn, *, code, account_type, kind, source_type,
                  source_id, delta_shares, before, after, reverses=None,
                  row_before=None):
    # row_before carries the holdings row's other columns (lent /
    # ordered shares, custody type) when the event deletes the row:
    # without them a later reversal could not restore the row whole.
    lent, ordered, custody = row_before or (None, None, None)
    conn.execute(
        "INSERT INTO holding_events(code, account_type, kind,"
        " source_type, source_id, delta_shares, shares_before,"
        " shares_after, avg_price_before, avg_price_after,"
        " lent_shares_before, ordered_shares_before,"
        " custody_type_before, reverses_event_id)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (code, account_type, kind, source_type, source_id, delta_shares,
         before[0], after[0], before[1], after[1],
         lent, ordered, custody, reverses),
    )


def _row_extras(row):
    """The holdings columns a shares/price restore alone would lose."""
    return (row["lent_shares"], row["ordered_shares"], row["custody_type"])


def _linkage_event(conn, source_type, source_id, kind):
    return conn.execute(
        "SELECT * FROM holding_events WHERE source_type = %s"
        " AND source_id = %s AND kind = %s",
        (source_type, source_id, kind),
    ).fetchone()


def _latest_open_event(conn, code, account_type):
    """The newest buy/sell event for a holding that no reversal has
    undone yet. Only that event may be rolled back: undoing an older
    one would silently rewrite the newer changes on top of it."""
    return conn.execute(
        "SELECT * FROM holding_events e"
        " WHERE e.code = %s AND e.account_type = %s"
        " AND e.kind IN ('buy', 'sell')"
        " AND NOT EXISTS (SELECT 1 FROM holding_events r"
        "                 WHERE r.reverses_event_id = e.id)"
        " ORDER BY e.id DESC LIMIT 1",
        (code, account_type),
    ).fetchone()


def _apply_reversal(conn, event, *, kind, source_type, source_id):
    """Undo a linkage event, restoring the holding to the exact
    state the event recorded as its "before" (shares and average
    price; a holding row the event deleted comes back, one it
    created disappears). The reversal is itself recorded as an
    event pointing at the original."""
    code, account_type = event["code"], event["account_type"]
    holding = _holding_row(conn, code, account_type)
    current = (
        (holding["shares"], holding["avg_price"]) if holding else (0, None)
    )
    target = (event["shares_before"], event["avg_price_before"])
    row_before = None
    if target[0] == 0:
        if holding is not None:
            row_before = _row_extras(holding)
            conn.execute(
                "DELETE FROM holdings WHERE id = %s", (holding["id"],)
            )
    elif holding is None:
        conn.execute(
            "INSERT INTO holdings(code, account_type, avg_price, shares,"
            " lent_shares, ordered_shares, custody_type)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (code, account_type, target[1], target[0],
             event["lent_shares_before"] or 0,
             event["ordered_shares_before"] or 0,
             event["custody_type_before"]),
        )
    else:
        conn.execute(
            "UPDATE holdings SET shares = %s, avg_price = %s,"
            " updated_at = now() WHERE id = %s",
            (target[0], target[1], holding["id"]),
        )
    _record_event(
        conn, code=code, account_type=account_type, kind=kind,
        source_type=source_type, source_id=source_id,
        delta_shares=-event["delta_shares"],
        before=current, after=target, reverses=event["id"],
        row_before=row_before,
    )


def _rollback_plan(conn, source_type, source_id, kind):
    """How cancelling/deleting a trade interacts with its linkage
    event: 'plain' (no linkage event, nothing to roll back),
    'rollback' (the event is still the holding's latest open event)
    or 'blocked' (a later event sits on top of it)."""
    event = _linkage_event(conn, source_type, source_id, kind)
    if event is None:
        return "plain", None
    latest = _latest_open_event(conn, event["code"], event["account_type"])
    if latest is None or latest["id"] != event["id"]:
        return "blocked", event
    return "rollback", event


def final_buy_status(status):
    """The status a buy is stored with: anything outside the shared
    vocabulary (including None) means the order has executed."""
    return status if status in TRADE_STATUSES else "約定済"


def buy_linkage_applies(*, account_type, status, shares, avg_price):
    """Whether registering a buy also moves the holding: the trade
    must name an account, be executed, and carry a quantity and a
    price (the holding's average cannot be derived without both)."""
    return (
        account_type is not None
        and final_buy_status(status) == "約定済"
        and bool(shares)
        and avg_price is not None
    )


def _apply_buy_linkage(conn, lot):
    code, account_type = lot["code"], lot["account_type"]
    shares = lot["shares"]
    price = Decimal(str(lot["avg_price"]))
    holding = _holding_row(conn, code, account_type)
    if holding is None:
        before = (0, None)
        after = (shares, price)
        conn.execute(
            "INSERT INTO holdings(code, account_type, avg_price, shares)"
            " VALUES (%s, %s, %s, %s)",
            (code, account_type, price, shares),
        )
    else:
        total = holding["shares"] + shares
        new_avg = (
            holding["avg_price"] * holding["shares"] + price * shares
        ) / total
        before = (holding["shares"], holding["avg_price"])
        after = (total, new_avg)
        conn.execute(
            "UPDATE holdings SET shares = %s, avg_price = %s,"
            " updated_at = now() WHERE id = %s",
            (total, new_avg, holding["id"]),
        )
    _record_event(
        conn, code=code, account_type=account_type, kind="buy",
        source_type="buy_lot", source_id=lot["id"],
        delta_shares=shares, before=before, after=after,
    )


def create_buy(conn, *, code, name, avg_price, shares, purchase_date,
               policy, trigger_note, note, status, account_type=None):
    final_status = final_buy_status(status)
    _upsert_stock(conn, code, name)
    row = conn.execute(
        "INSERT INTO buy_lots(code, name, avg_price, shares, purchase_date,"
        " policy, trigger_note, note, status, account_type)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " RETURNING *",
        (code, name, avg_price, shares, purchase_date, policy,
         trigger_note, note, final_status, account_type),
    ).fetchone()
    if buy_linkage_applies(account_type=account_type, status=status,
                           shares=shares, avg_price=avg_price):
        _apply_buy_linkage(conn, row)
    return row


def cancel_buy(conn, lot_id):
    """Mark a buy cancelled, rolling its holdings linkage back when
    it has one. Returns (outcome, row): 'not_found', 'already' and
    'blocked' map to 404/409/409 at the API layer, 'ok' carries the
    updated row. A buy without a linkage event is only marked."""
    row = conn.execute(
        "SELECT * FROM buy_lots WHERE id = %s", (lot_id,)
    ).fetchone()
    if row is None:
        return "not_found", None
    if row["cancelled_at"] is not None:
        return "already", row
    outcome, event = _rollback_plan(conn, "buy_lot", lot_id, "buy")
    if outcome == "blocked":
        return "blocked", row
    if event is not None:
        _apply_reversal(conn, event, kind="cancel",
                        source_type="buy_lot", source_id=lot_id)
    row = conn.execute(
        "UPDATE buy_lots SET cancelled_at = now() WHERE id = %s"
        " RETURNING *",
        (lot_id,),
    ).fetchone()
    return "ok", row


def delete_buy(conn, lot_id):
    """Delete a buy lot, rolling its linkage back first when it has
    one and is not already cancelled. Returns 'not_found',
    'blocked' (a later event sits on the holding; nothing is
    deleted) or 'deleted'."""
    row = conn.execute(
        "SELECT * FROM buy_lots WHERE id = %s", (lot_id,)
    ).fetchone()
    if row is None:
        return "not_found"
    if row["cancelled_at"] is None:
        outcome, event = _rollback_plan(conn, "buy_lot", lot_id, "buy")
        if outcome == "blocked":
            return "blocked"
        if event is not None:
            _apply_reversal(conn, event, kind="delete",
                            source_type="buy_lot", source_id=lot_id)
    conn.execute("DELETE FROM buy_lots WHERE id = %s", (lot_id,))
    return "deleted"


def _apply_sale_linkage(conn, sale, holding):
    ex = sale["executed_shares"]
    remaining = holding["shares"] - ex
    before = (holding["shares"], holding["avg_price"])
    row_before = None
    if remaining == 0:
        after = (0, None)
        row_before = _row_extras(holding)
        conn.execute("DELETE FROM holdings WHERE id = %s", (holding["id"],))
    else:
        after = (remaining, holding["avg_price"])
        conn.execute(
            "UPDATE holdings SET shares = %s, updated_at = now()"
            " WHERE id = %s",
            (remaining, holding["id"]),
        )
    _record_event(
        conn, code=sale["code"], account_type=sale["account_type"],
        kind="sell", source_type="sale", source_id=sale["id"],
        delta_shares=-ex, before=before, after=after,
        row_before=row_before,
    )


def create_sale(conn, *, code, name, buy_price, shares, purchase_date,
                sell_price, sell_shares, executed_shares, order_start_date,
                executed_date, profit, status, memo, account_type=None):
    ex = executed_shares or 0
    pf = profit
    if pf is None and buy_price is not None and sell_price is not None and ex:
        pf = (sell_price - buy_price) * ex
    if status not in TRADE_STATUSES:
        # "自動" (or anything unspecified): derive from executed shares.
        if sell_shares and ex >= sell_shares:
            status = "約定済"
        elif ex > 0:
            status = "一部約定"
        else:
            status = "注文中"
    holding = None
    if account_type is not None and ex > 0:
        # The sale links by its executed shares: the holding for
        # this exact account must cover them, otherwise the sale
        # itself is refused (LinkageError -> 422, nothing stored).
        holding = _holding_row(conn, code, account_type)
        if holding is None or holding["shares"] < ex:
            raise LinkageError(
                "約定株数が保有株数を超えているため、売却を登録できません"
            )
    _upsert_stock(conn, code, name)
    row = conn.execute(
        "INSERT INTO sales(code, name, buy_price, shares, purchase_date,"
        " sell_price, sell_shares, executed_shares, order_start_date,"
        " executed_date, profit, status, memo, account_type)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (code, name, buy_price, shares, purchase_date, sell_price,
         sell_shares, ex, order_start_date, executed_date, pf, status,
         memo, account_type),
    ).fetchone()
    if holding is not None:
        _apply_sale_linkage(conn, row, holding)
    return row


def cancel_sale(conn, sale_id):
    """Mark a sale cancelled, rolling its holdings linkage back
    when it has one. Same outcomes as cancel_buy."""
    row = conn.execute(
        "SELECT * FROM sales WHERE id = %s", (sale_id,)
    ).fetchone()
    if row is None:
        return "not_found", None
    if row["cancelled_at"] is not None:
        return "already", row
    outcome, event = _rollback_plan(conn, "sale", sale_id, "sell")
    if outcome == "blocked":
        return "blocked", row
    if event is not None:
        _apply_reversal(conn, event, kind="cancel",
                        source_type="sale", source_id=sale_id)
    row = conn.execute(
        "UPDATE sales SET cancelled_at = now() WHERE id = %s"
        " RETURNING *",
        (sale_id,),
    ).fetchone()
    return "ok", row


def delete_sale(conn, sale_id):
    """Delete a sale, rolling its linkage back first when it has
    one and is not already cancelled. Same outcomes as delete_buy."""
    row = conn.execute(
        "SELECT * FROM sales WHERE id = %s", (sale_id,)
    ).fetchone()
    if row is None:
        return "not_found"
    if row["cancelled_at"] is None:
        outcome, event = _rollback_plan(conn, "sale", sale_id, "sell")
        if outcome == "blocked":
            return "blocked"
        if event is not None:
            _apply_reversal(conn, event, kind="delete",
                            source_type="sale", source_id=sale_id)
    conn.execute("DELETE FROM sales WHERE id = %s", (sale_id,))
    return "deleted"


def find_holding(conn, code, account_type):
    """The holding row for (code, account_type), or None."""
    return _holding_row(conn, code, account_type)


def get_holding(conn, holding_id):
    """One holding row by id, with the stock name joined in."""
    return conn.execute(
        "SELECT h.*, s.name AS name FROM holdings h"
        " JOIN stocks s ON s.code = h.code WHERE h.id = %s",
        (holding_id,),
    ).fetchone()


def create_holding(conn, *, code, account_type, avg_price, shares,
                   lent_shares, ordered_shares, custody_type):
    """Register a holding directly (the cover for initial
    registration and for trades that left no history) and record
    the manual add in holding_events: the before state is \"not
    held\" (0 shares, no average price). The caller has already
    checked that no holding exists for (code, account_type) and
    that the stock master knows the code."""
    row = conn.execute(
        "INSERT INTO holdings(code, account_type, custody_type,"
        " avg_price, shares, lent_shares, ordered_shares)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (code, account_type, custody_type, avg_price, shares,
         lent_shares, ordered_shares),
    ).fetchone()
    _record_event(
        conn, code=code, account_type=account_type, kind="add",
        source_type="manual", source_id=None, delta_shares=shares,
        before=(0, None), after=(shares, avg_price),
    )
    return row


def delete_holding(conn, holding_id):
    """Delete a holding row, recording a manual delete event first
    (with the row's other columns as before-values, so the ledger
    keeps the whole row). Returns 'not_found', 'blocked' or
    'deleted': while the holding's latest unreversed linkage event
    is a buy/sell event the delete is blocked — the trade side
    must be cancelled first, otherwise the ledger would lose the
    thread back to the trade that made the change."""
    row = conn.execute(
        "SELECT * FROM holdings WHERE id = %s", (holding_id,)
    ).fetchone()
    if row is None:
        return "not_found"
    if _latest_open_event(conn, row["code"], row["account_type"]) is not None:
        return "blocked"
    _record_event(
        conn, code=row["code"], account_type=row["account_type"],
        kind="delete", source_type="manual", source_id=None,
        delta_shares=-row["shares"],
        before=(row["shares"], row["avg_price"]), after=(0, None),
        row_before=_row_extras(row),
    )
    conn.execute("DELETE FROM holdings WHERE id = %s", (holding_id,))
    return "deleted"


def list_holding_events(conn, code, account_type):
    """The holding's events, newest first. Events belong to the
    (code, account_type) pair, not to a row id: a holding deleted
    and re-registered keeps its history under the same key."""
    return conn.execute(
        "SELECT * FROM holding_events"
        " WHERE code = %s AND account_type = %s ORDER BY id DESC",
        (code, account_type),
    ).fetchall()


def list_candidate_rows(conn):
    """Purchase candidates with their latest daily snapshot."""
    return conn.execute(
        """
        SELECT c.code, c.added_at, c.policy, c.memo, c.target_price, s.name,
               snap.snap_date, snap.close_price, snap.prev_close,
               snap.dividend_forecast, snap.dividend_yield, snap.per, snap.pbr
        FROM candidates c
        LEFT JOIN stocks s ON s.code = c.code
        LEFT JOIN LATERAL (
            SELECT * FROM daily_snapshots d
            WHERE d.code = c.code ORDER BY d.snap_date DESC LIMIT 1
        ) snap ON true
        ORDER BY c.added_at, c.code
        """
    ).fetchall()


def candidate_exists(conn, code):
    return (
        conn.execute(
            "SELECT 1 FROM candidates WHERE code = %s", (code,)
        ).fetchone()
        is not None
    )


def register_candidate(conn, code, market, name):
    conn.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, %s)"
        " ON CONFLICT(code) DO UPDATE SET"
        " name = COALESCE(EXCLUDED.name, stocks.name),"
        " market = COALESCE(EXCLUDED.market, stocks.market)",
        (code, name, market),
    )
    conn.execute(
        "INSERT INTO candidates(code) VALUES (%s)"
        " ON CONFLICT(code) DO NOTHING",
        (code,),
    )


def delete_candidate(conn, code):
    cur = conn.execute("DELETE FROM candidates WHERE code = %s", (code,))
    return cur.rowcount > 0


def update_candidate(conn, code, **fields):
    """Partial update: only the fields present in `fields` are updated.

    Fields omitted by the caller keep their stored value; a field
    explicitly set to None is cleared. An empty update only checks
    that the row exists (so PUT {} keeps the row unchanged).
    """
    fields = {k: v for k, v in fields.items()
              if k in ("policy", "memo", "target_price")}
    if not fields:
        row = conn.execute(
            "SELECT 1 FROM candidates WHERE code = %s", (code,)
        ).fetchone()
        return row is not None
    sets = [f"{key} = %s" for key in fields]
    params = list(fields.values())
    params.append(code)
    cur = conn.execute(
        f"UPDATE candidates SET {', '.join(sets)} WHERE code = %s",
        tuple(params),
    )
    return cur.rowcount > 0


def candidates_at_target(conn):
    """Candidates whose latest close has reached their target price."""
    return conn.execute(
        """
        SELECT c.code, s.name, c.target_price, snap.close_price
        FROM candidates c
        LEFT JOIN stocks s ON s.code = c.code
        JOIN LATERAL (
            SELECT * FROM daily_snapshots d
            WHERE d.code = c.code ORDER BY d.snap_date DESC LIMIT 1
        ) snap ON true
        WHERE c.target_price IS NOT NULL
          AND snap.close_price <= c.target_price
        ORDER BY c.code
        """
    ).fetchall()


def update_benefit(conn, code, **fields):
    """Partial update: only the fields present in `fields` are updated.

    Fields omitted by the caller keep their stored value; a field
    explicitly set to None is cleared. An empty update only checks
    that the row exists (so PUT {} keeps the row unchanged).
    """
    fields = {k: v for k, v in fields.items() if k in (
        "benefit_value", "benefit_desc", "judgment_on",
        "judgment_note", "funding_target", "policy",
    )}
    if not fields:
        row = conn.execute(
            "SELECT 1 FROM dividend_benefits WHERE code = %s", (code,)
        ).fetchone()
        return row is not None
    sets = []
    params = []
    for key, value in fields.items():
        if key == "funding_target":
            value = True if value else None
        sets.append(f"{key} = %s")
        params.append(value)
    sets.append("info_updated_on = %s")
    params.append(datetime.now(JST).date())
    params.append(code)
    cur = conn.execute(
        f"UPDATE dividend_benefits SET {', '.join(sets)} WHERE code = %s",
        tuple(params),
    )
    return cur.rowcount > 0


BENEFIT_MEMO_FIELDS = (
    "shareholder_no", "deadline_1", "deadline_2", "delivery_waiting",
    "status", "form_arrival_1", "form_arrival_2",
    "item_arrival_1", "item_arrival_2",
)

_BENEFIT_MEMO_SELECT = (
    "m.shareholder_no, m.deadline_1, m.deadline_2,"
    " COALESCE(m.delivery_waiting, FALSE) AS delivery_waiting,"
    " m.status, m.form_arrival_1, m.form_arrival_2,"
    " m.item_arrival_1, m.item_arrival_2"
)


def list_benefit_memo_rows(conn):
    """Benefit memos for the display targets.

    A stock is a target only when its dividend master row carries a
    benefit (benefit_value > 0 or a non-empty benefit_desc) and the
    stock is currently held. Targets without a memo yet come back
    with null memo fields (delivery_waiting falls back to false).
    """
    return conn.execute(
        f"""
        SELECT d.code, s.name, {_BENEFIT_MEMO_SELECT}
        FROM dividend_benefits d
        JOIN stocks s ON s.code = d.code
        LEFT JOIN benefit_memos m ON m.code = d.code
        WHERE (d.benefit_value > 0 OR COALESCE(d.benefit_desc, '') <> '')
          AND EXISTS (
              SELECT 1 FROM holdings h WHERE h.code = d.code
          )
        ORDER BY d.code
        """
    ).fetchall()


def get_benefit_memo_row(conn, code):
    """One stock with its memo (if any), regardless of the display rule."""
    return conn.execute(
        f"""
        SELECT s.code, s.name, {_BENEFIT_MEMO_SELECT}
        FROM stocks s
        LEFT JOIN benefit_memos m ON m.code = s.code
        WHERE s.code = %s
        """,
        (code,),
    ).fetchone()


def upsert_benefit_memo(conn, code, **fields):
    """Create or partially update the memo for a stock.

    Only the fields present in `fields` are written: omitted fields
    keep their stored value, a field explicitly set to None is
    cleared. Returns False when the code is not in the stock master
    (the caller turns that into a 404). The memo is keyed on the
    stock alone, so a stock outside the display rule can still hold
    a memo; it just never shows on the list.
    """
    stock = conn.execute(
        "SELECT 1 FROM stocks WHERE code = %s", (code,)
    ).fetchone()
    if stock is None:
        return False
    fields = {k: v for k, v in fields.items()
              if k in BENEFIT_MEMO_FIELDS}
    # delivery_waiting is NOT NULL: an explicit null means "clear",
    # which for a flag is false.
    if fields.get("delivery_waiting", True) is None:
        fields["delivery_waiting"] = False
    if not fields:
        conn.execute(
            "INSERT INTO benefit_memos(code) VALUES (%s)"
            " ON CONFLICT(code) DO NOTHING",
            (code,),
        )
        return True
    cols = list(fields)
    conn.execute(
        f"INSERT INTO benefit_memos(code, {', '.join(cols)})"
        f" VALUES ({', '.join(['%s'] * (len(cols) + 1))})"
        " ON CONFLICT(code) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols),
        (code, *fields.values()),
    )
    return True
