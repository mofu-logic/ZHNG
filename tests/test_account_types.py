"""Account types （口座区分）: the allowlist, and NOT NULL on holdings.

Domain rules under test (owner decisions, 2026-11-28):

- The only valid account types are 「特定」「NISA」「普通」
  (allowlist). 「新NISA」 is deliberately not included yet, and
  free-form values such as 「一般」 are rejected everywhere an
  account type is accepted.
- POST /api/v1/holdings accepts 「普通」. The same code can be
  held in all three accounts as separate rows. Any other value
  is a 422 and registers nothing.
- Buys and sales link to the holding of a 「普通」 account just
  like the other accounts. A trade without an account type still
  registers without linking. A trade naming a value outside the
  allowlist is a 422 and the trade itself is not registered.
- The CSV import takes 「普通」 rows. A row naming a value
  outside the allowlist fails on its own (the reason names the
  account type as the problem) and the other rows still import.
  (A blank account cell defaulting to 「特定」 is already pinned
  by test_holdings_import.py and is not repeated here.)
- The dashboard's holding-add form offers exactly the three
  account types as its options.
- Database constraints, as applied by the schema:
  holdings.account_type and holding_events.account_type reject
  both NULL and values outside the allowlist on a direct INSERT;
  the (code, account_type) unique index on holdings is a plain
  (not partial) index, and a duplicate (code, 「普通」) INSERT
  fails. buy_lots / sales keep accepting a NULL account type
  (the "does not link" marker) but reject outside values.

Note on the current code: account_type is still free text, so
the 「普通が使える」 tests below already pass today. They are
kept as regression guards so the allowlist implementation
cannot break 「普通」 while learning to reject the rest; the
rejection and constraint tests are the ones that fail (Red).
"""
import re

import psycopg
import pytest


# ---------------------------------------------------------------- helpers


def _seed_stock(db, code="9999", name="テスト銘柄"):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, '東証')",
        (code, name),
    )
    db.commit()


def _seed_holding(db, code="9999", account_type="普通", avg_price=800,
                  shares=100):
    _seed_stock(db, code)
    db.execute(
        "INSERT INTO holdings(code, account_type, avg_price, shares)"
        " VALUES (%s, %s, %s, %s)",
        (code, account_type, avg_price, shares),
    )
    db.commit()


def _holding(db, code="9999", account_type="普通"):
    return db.execute(
        "SELECT * FROM holdings WHERE code = %s AND account_type = %s",
        (code, account_type),
    ).fetchone()


def _events(db):
    return db.execute(
        "SELECT * FROM holding_events ORDER BY id"
    ).fetchall()


def _count(db, table):
    return db.execute(
        f"SELECT count(*) AS c FROM {table}"
    ).fetchone()["c"]


def _add(client, **over):
    payload = {
        "code": "9999",
        "account_type": "普通",
        "avg_price": 800,
        "shares": 100,
    }
    payload.update(over)
    return client.post("/api/v1/holdings", json=payload)


def _buy(client, **over):
    payload = {
        "code": "9999",
        "name": "テスト銘柄",
        "avg_price": 1000,
        "shares": 50,
        "purchase_date": "2026-11-20",
        "status": "約定済",
        "account_type": "普通",
    }
    payload.update(over)
    return client.post("/api/v1/buys", json=payload)


def _sale(client, **over):
    payload = {
        "code": "9999",
        "name": "テスト銘柄",
        "buy_price": 800,
        "sell_price": 1200,
        "sell_shares": 30,
        "executed_shares": 30,
        "status": "自動",
        "account_type": "普通",
    }
    payload.update(over)
    return client.post("/api/v1/sales", json=payload)


def _insert_must_fail(db, sql, params):
    with pytest.raises(psycopg.Error):
        db.execute(sql, params)
        db.commit()
    db.rollback()


# ------------------------------------------------------- holdings API


def test_holding_add_futsu_accepted(client, db):
    _seed_stock(db)
    r = _add(client)
    assert r.status_code == 201
    assert r.json()["account_type"] == "普通"
    holding = _holding(db)
    assert holding is not None
    assert holding["shares"] == 100
    events = _events(db)
    assert len(events) == 1
    assert events[0]["account_type"] == "普通"
    assert events[0]["kind"] == "add"


def test_holding_same_code_in_three_accounts(client, db):
    _seed_stock(db)
    assert _add(client, account_type="特定").status_code == 201
    assert _add(client, account_type="NISA", avg_price=900,
                shares=20).status_code == 201
    assert _add(client, account_type="普通", avg_price=950,
                shares=30).status_code == 201
    rows = db.execute(
        "SELECT account_type FROM holdings WHERE code = '9999'"
        " ORDER BY account_type"
    ).fetchall()
    assert [r["account_type"] for r in rows] == ["NISA", "普通", "特定"]
    assert _holding(db, account_type="普通")["shares"] == 30


@pytest.mark.parametrize("bad", ["新NISA", "一般"])
def test_holding_add_outside_allowlist_is_422(client, db, bad):
    _seed_stock(db)
    r = _add(client, account_type=bad)
    assert r.status_code == 422
    assert _count(db, "holdings") == 0
    assert _events(db) == []


# ------------------------------------------------------- trade linkage


def test_buy_futsu_links_to_holding(client, db):
    _seed_stock(db)
    r = _buy(client)
    assert r.status_code == 201
    assert r.json()["account_type"] == "普通"
    holding = _holding(db)
    assert holding is not None
    assert holding["shares"] == 50
    assert float(holding["avg_price"]) == 1000.0
    events = _events(db)
    assert len(events) == 1
    assert events[0]["account_type"] == "普通"
    assert events[0]["kind"] == "buy"


def test_sale_futsu_links_to_holding(client, db):
    _seed_holding(db)  # 普通: 100 shares @ 800
    r = _sale(client)
    assert r.status_code == 201
    assert r.json()["account_type"] == "普通"
    holding = _holding(db)
    assert holding["shares"] == 70
    events = _events(db)
    assert len(events) == 1
    assert events[0]["account_type"] == "普通"
    assert events[0]["kind"] == "sell"


@pytest.mark.parametrize("bad", ["新NISA", "一般"])
def test_buy_outside_allowlist_is_422_and_registers_nothing(client, db, bad):
    _seed_stock(db)
    r = _buy(client, account_type=bad)
    assert r.status_code == 422
    assert _count(db, "buy_lots") == 0
    assert _count(db, "holdings") == 0
    assert _events(db) == []


def test_sale_outside_allowlist_is_422_and_registers_nothing(client, db):
    # A holding under the outside value exists (inserted directly,
    # as the current schema still allows it), so the 422 can only
    # come from the allowlist, not from a missing holding.
    _seed_holding(db, account_type="新NISA")
    r = _sale(client, account_type="新NISA")
    assert r.status_code == 422
    assert _count(db, "sales") == 0
    assert _holding(db, account_type="新NISA")["shares"] == 100
    assert _events(db) == []


# ------------------------------------------------------- CSV import

IMPORT_HEADER = "コード,口座,平均取得単価,株数,貸株数,発注数,保管区分"
IMPORT_MAPPING = {
    "code_col": "コード",
    "account_type_col": "口座",
    "avg_price_col": "平均取得単価",
    "shares_col": "株数",
    "lent_shares_col": "貸株数",
    "ordered_shares_col": "発注数",
    "custody_type_col": "保管区分",
}


def _import(client, rows):
    text = "\n".join([IMPORT_HEADER] + rows) + "\n"
    return client.post(
        "/api/v1/holdings/import",
        files={"file": ("holdings.csv", text.encode("utf-8"), "text/csv")},
        data=dict(IMPORT_MAPPING),
    )


def test_import_futsu_row(client, db):
    _seed_stock(db)
    r = _import(client, ["9999,普通,800,100,0,0,"])
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1
    assert body["failed"] == 0
    assert _holding(db) is not None


def test_import_outside_allowlist_row_fails_others_still_import(client, db):
    _seed_stock(db, "9999")
    _seed_stock(db, "1111", "別銘柄")
    _seed_stock(db, "2222", "第三銘柄")
    r = _import(client, [
        "9999,特定,800,100,0,0,",      # row 2: ok
        "1111,新NISA,900,50,0,0,",     # row 3: outside the allowlist
        "2222,普通,700,30,0,0,",       # row 4: ok
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 2
    assert body["failed"] == 1
    assert body["failures"][0]["row"] == 3
    assert "口座" in body["failures"][0]["reason"]
    assert _holding(db, "9999", "特定") is not None
    assert _holding(db, "2222", "普通") is not None
    assert _count(db, "holdings") == 2


# ------------------------------------------------------- dashboard form


def test_dashboard_add_form_account_options_are_exactly_the_three(client, db):
    _seed_holding(db, account_type="特定")
    page = client.get("/").text
    m = re.search(
        r'<select name="account_type">(.*?)</select>', page, re.S
    )
    assert m is not None
    values = re.findall(r'<option value="([^"]*)"', m.group(1))
    assert values == ["特定", "NISA", "普通"]


# ------------------------------------------------------- DB constraints


def test_db_holdings_null_account_type_fails(db):
    _seed_stock(db)
    _insert_must_fail(
        db,
        "INSERT INTO holdings(code, account_type, avg_price, shares)"
        " VALUES ('9999', NULL, 800, 100)",
        (),
    )
    assert _count(db, "holdings") == 0


@pytest.mark.parametrize("bad", ["新NISA", "一般"])
def test_db_holdings_outside_allowlist_fails(db, bad):
    _seed_stock(db)
    _insert_must_fail(
        db,
        "INSERT INTO holdings(code, account_type, avg_price, shares)"
        " VALUES ('9999', %s, 800, 100)",
        (bad,),
    )
    assert _count(db, "holdings") == 0


_EVENT_INSERT = (
    "INSERT INTO holding_events(code, account_type, kind, source_type,"
    " delta_shares, shares_before, shares_after)"
    " VALUES ('9999', %s, 'add', 'manual', 100, 0, 100)"
)


def test_db_holding_events_null_account_type_fails(db):
    _insert_must_fail(db, _EVENT_INSERT, (None,))
    assert _count(db, "holding_events") == 0


def test_db_holding_events_outside_allowlist_fails(db):
    _insert_must_fail(db, _EVENT_INSERT, ("新NISA",))
    assert _count(db, "holding_events") == 0


def test_db_holdings_unique_index_is_not_partial(db):
    rows = db.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'holdings'"
    ).fetchall()
    defs = [
        r["indexdef"] for r in rows
        if "code" in r["indexdef"] and "account_type" in r["indexdef"]
    ]
    assert defs, "no (code, account_type) index on holdings"
    assert all("WHERE" not in d.upper() for d in defs)


def test_db_holdings_duplicate_futsu_fails(db):
    _seed_holding(db)  # (9999, 普通) already held
    _insert_must_fail(
        db,
        "INSERT INTO holdings(code, account_type, avg_price, shares)"
        " VALUES ('9999', '普通', 900, 50)",
        (),
    )
    assert _count(db, "holdings") == 1


def test_db_buy_lots_null_account_type_still_ok(db):
    db.execute(
        "INSERT INTO buy_lots(code, account_type) VALUES ('9999', NULL)"
    )
    db.commit()
    assert _count(db, "buy_lots") == 1


def test_db_buy_lots_outside_allowlist_fails(db):
    _insert_must_fail(
        db,
        "INSERT INTO buy_lots(code, account_type) VALUES ('9999', %s)",
        ("新NISA",),
    )
    assert _count(db, "buy_lots") == 0


def test_db_sales_null_account_type_still_ok(db):
    db.execute(
        "INSERT INTO sales(code, account_type) VALUES ('9999', NULL)"
    )
    db.commit()
    assert _count(db, "sales") == 1


def test_db_sales_outside_allowlist_fails(db):
    _insert_must_fail(
        db,
        "INSERT INTO sales(code, account_type) VALUES ('9999', %s)",
        ("新NISA",),
    )
    assert _count(db, "sales") == 0
