"""売買・マスタ・購入候補のサービス層。

画面と JSON API (/api/v1) が共有する書き込み／読み出しロジック。
導出ロジック（約定状態、売却損益、候補の到達判定）はこの層に
1か所だけ置く。サービス層は割らず、ここにまとめたままにする。

db.py と config.py にのみ依存する（循環 import を作らない）。
関数は接続済みの conn を受け取り、呼び出し側がコミットする。
"""

from datetime import datetime

from app.config import JST, TRADE_STATUSES


def _upsert_stock(conn, code, name):
    if code and name:
        conn.execute(
            "INSERT INTO stocks(code, name) VALUES (%s, %s)"
            " ON CONFLICT(code) DO NOTHING",
            (code, name),
        )


def create_buy(conn, *, code, name, avg_price, shares, purchase_date,
               policy, trigger_note, note, status):
    final_status = status if status in TRADE_STATUSES else "約定済"
    _upsert_stock(conn, code, name)
    row = conn.execute(
        "INSERT INTO buy_lots(code, name, avg_price, shares, purchase_date,"
        " policy, trigger_note, note, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " RETURNING *",
        (code, name, avg_price, shares, purchase_date, policy,
         trigger_note, note, final_status),
    ).fetchone()
    return row


def delete_buy(conn, lot_id):
    cur = conn.execute("DELETE FROM buy_lots WHERE id = %s", (lot_id,))
    return cur.rowcount > 0


def create_sale(conn, *, code, name, buy_price, shares, purchase_date,
                sell_price, sell_shares, executed_shares, order_start_date,
                executed_date, profit, status, memo):
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
    _upsert_stock(conn, code, name)
    row = conn.execute(
        "INSERT INTO sales(code, name, buy_price, shares, purchase_date,"
        " sell_price, sell_shares, executed_shares, order_start_date,"
        " executed_date, profit, status, memo)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (code, name, buy_price, shares, purchase_date, sell_price,
         sell_shares, ex, order_start_date, executed_date, pf, status, memo),
    ).fetchone()
    return row


def delete_sale(conn, sale_id):
    cur = conn.execute("DELETE FROM sales WHERE id = %s", (sale_id,))
    return cur.rowcount > 0


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
