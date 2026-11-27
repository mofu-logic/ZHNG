"""Holdings create/delete and the per-holding event history.

Domain rules under test (the manual counterpart of the trade
linkage in test_holdings_linkage.py):

- POST /api/v1/holdings registers a holding directly — the cover
  for initial registration and for trades that left no history.
  Registering the same (code, account_type) twice is a 409. A
  code the stock master does not know yet is fetched from Yahoo
  first (the same way as purchase-candidate registration and
  linked buys); when the fetch fails the answer is 502 and
  nothing is created. Missing or invalid shares / avg_price /
  account_type / code is a 422.
- A manual registration is recorded in holding_events as kind
  "add" with source_type "manual": delta_shares is the added
  count, shares_before is 0 and avg_price_before is NULL,
  shares_after / avg_price_after carry the registered values.
- DELETE /api/v1/holdings/{id} records kind "delete" /
  source_type "manual" (delta_shares negative, shares_after 0,
  avg_price_after NULL) and then removes the row. When the
  holding's latest unreversed linkage event is a buy/sell event
  the delete is a 409 — the trade side must be cancelled first.
  Snapshot rows (no events) and manually added rows delete
  freely, and a holding whose linkage event was cancelled is
  deletable again. An unknown id is a 404.
- GET /api/v1/holdings/{id}/events returns the events of that
  holding's (code, account_type), newest first: kind, source,
  share and average-price before/after, the reversal reference
  and the timestamp. An unknown id is a 404.
- The dashboard (GET /) offers an add button with a modal
  (code, account type, average price, shares, lent and ordered
  shares) and, per row, a delete button and a history button
  that shows the row's events in a modal via the events API.

Naming pinned here, as the natural extension of the linkage
events (kind buy/sell/cancel/delete, source_type buy_lot/sale):
manual operations use source_type "manual", and a manual
registration uses kind "add" (kind "delete" is shared with the
trade-side deletion reversal, told apart by source_type).
"""
import pytest
from conftest import seed_stock_holding
from test_fetch import make_fake_client
from test_parse import JP_PAGE

from app import main

_TITLE = "<html><head><title>テスト候補(株)【7777】：株価・株式情報 - Yahoo!ファイナンス</title></head>"
UNKNOWN_CODE_PAGE = JP_PAGE.replace("<html>", _TITLE, 1)


def _named_fake_client():
    base = make_fake_client()

    class NamedClient(base):
        async def get(self, url):
            r = await super().get(url)
            if "/7777." in url and r.status_code == 200:
                return type(r)(200, UNKNOWN_CODE_PAGE)
            return r

    return NamedClient


def _events(db):
    return db.execute(
        "SELECT * FROM holding_events ORDER BY id"
    ).fetchall()


def _holding(db, code="9999", account_type="特定"):
    return db.execute(
        "SELECT * FROM holdings WHERE code = %s AND account_type = %s",
        (code, account_type),
    ).fetchone()


def _seed_stock_only(db, code="9999"):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, 'テスト銘柄', '東証')",
        (code,),
    )
    db.commit()


def _add(client, **over):
    payload = {
        "code": "9999",
        "account_type": "特定",
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
        "account_type": "特定",
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
        "account_type": "特定",
    }
    payload.update(over)
    return client.post("/api/v1/sales", json=payload)


# ---------------------------------------------------------------- OpenAPI


def test_openapi_holdings_paths_and_schemas(client, db):
    doc = client.get("/openapi.json").json()
    paths = doc["paths"]
    assert "post" in paths["/api/v1/holdings"]
    assert "delete" in paths["/api/v1/holdings/{id}"]
    assert "get" in paths["/api/v1/holdings/{id}/events"]
    schemas = doc["components"]["schemas"]
    hin = schemas["HoldingIn"]
    for f in ("code", "account_type", "avg_price", "shares",
              "lent_shares", "ordered_shares", "custody_type"):
        assert f in hin["properties"], f
    for f in ("code", "account_type", "avg_price", "shares"):
        assert f in hin["required"], f
    hout = schemas["HoldingOut"]["properties"]
    for f in ("id", "code", "name", "account_type", "custody_type",
              "avg_price", "shares", "lent_shares", "ordered_shares"):
        assert f in hout, f
    ev = schemas["HoldingEventOut"]["properties"]
    for f in ("id", "code", "account_type", "kind", "source_type",
              "source_id", "delta_shares", "shares_before",
              "shares_after", "avg_price_before", "avg_price_after",
              "reverses_event_id", "created_at"):
        assert f in ev, f


# ---------------------------------------------------------------- create


def test_add_holding_creates_row_event_and_response(client, db):
    _seed_stock_only(db)
    r = _add(client)
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == "9999"
    assert body["name"] == "テスト銘柄"
    assert body["account_type"] == "特定"
    assert float(body["avg_price"]) == 800.0
    assert body["shares"] == 100
    # Optional fields default: no lent / ordered shares, no custody.
    assert body["lent_shares"] == 0
    assert body["ordered_shares"] == 0
    assert body["custody_type"] is None

    holding = _holding(db)
    assert holding is not None
    assert holding["id"] == body["id"]
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0

    events = _events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "add"
    assert ev["source_type"] == "manual"
    assert ev["code"] == "9999"
    assert ev["account_type"] == "特定"
    assert ev["delta_shares"] == 100
    assert ev["shares_before"] == 0
    assert ev["avg_price_before"] is None
    assert ev["shares_after"] == 100
    assert float(ev["avg_price_after"]) == 800.0
    assert ev["reverses_event_id"] is None


def test_add_holding_optional_fields_are_stored(client, db):
    _seed_stock_only(db)
    r = _add(client, lent_shares=10, ordered_shares=5,
             custody_type="保護預り")
    assert r.status_code == 201
    body = r.json()
    assert body["lent_shares"] == 10
    assert body["ordered_shares"] == 5
    assert body["custody_type"] == "保護預り"
    holding = _holding(db)
    assert holding["lent_shares"] == 10
    assert holding["ordered_shares"] == 5
    assert holding["custody_type"] == "保護預り"


def test_add_holding_same_code_other_account_is_allowed(client, db):
    seed_stock_holding(db)  # 9999 特定 already held
    r = _add(client, account_type="NISA", avg_price=900, shares=20)
    assert r.status_code == 201
    rows = db.execute(
        "SELECT account_type FROM holdings WHERE code = '9999'"
        " ORDER BY account_type"
    ).fetchall()
    assert len(rows) == 2
    assert _holding(db, account_type="NISA")["shares"] == 20


def test_add_holding_duplicate_is_409(client, db):
    seed_stock_holding(db)  # 9999 特定 already held
    r = _add(client)
    assert r.status_code == 409
    rows = db.execute("SELECT * FROM holdings").fetchall()
    assert len(rows) == 1
    assert _events(db) == []


def test_add_holding_unknown_code_fetches_stock(client, db, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    r = _add(client, code="7777")
    assert r.status_code == 201
    assert r.json()["name"] == "テスト候補(株)"
    stock = db.execute(
        "SELECT name, market FROM stocks WHERE code = '7777'"
    ).fetchone()
    assert stock is not None
    assert stock["name"] == "テスト候補(株)"
    assert stock["market"] == "東証"
    assert _holding(db, code="7777") is not None
    events = _events(db)
    assert len(events) == 1
    assert events[0]["kind"] == "add"


def test_add_holding_unknown_code_fetch_failure_is_502(client, db, monkeypatch):
    monkeypatch.setattr(
        main.httpx, "AsyncClient", make_fake_client(fail_codes=("0000",))
    )
    r = _add(client, code="0000")
    assert r.status_code == 502
    # Nothing is registered when the master info cannot be fetched.
    assert db.execute("SELECT count(*) c FROM stocks").fetchone()["c"] == 0
    assert db.execute("SELECT count(*) c FROM holdings").fetchone()["c"] == 0
    assert _events(db) == []


@pytest.mark.parametrize("payload", [
    {},  # everything missing
    {"code": "9999", "account_type": "特定", "avg_price": 800},  # no shares
    {"code": "9999", "account_type": "特定", "shares": 100},  # no avg_price
    {"code": "9999", "avg_price": 800, "shares": 100},  # no account_type
    {"account_type": "特定", "avg_price": 800, "shares": 100},  # no code
    {"code": "", "account_type": "特定", "avg_price": 800, "shares": 100},
    {"code": "9999", "account_type": "特定", "avg_price": 800, "shares": -1},
    {"code": "9999", "account_type": "特定", "avg_price": -1, "shares": 100},
])
def test_add_holding_validation_is_422(client, db, payload):
    r = client.post("/api/v1/holdings", json=payload)
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM holdings").fetchone()["c"] == 0
    assert _events(db) == []


# ---------------------------------------------------------------- delete


def test_delete_snapshot_holding_records_event(client, db):
    seed_stock_holding(db)
    holding = _holding(db)
    r = client.delete(f"/api/v1/holdings/{holding['id']}")
    assert r.status_code == 204
    assert _holding(db) is None
    events = _events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "delete"
    assert ev["source_type"] == "manual"
    assert ev["delta_shares"] == -100
    assert ev["shares_before"] == 100
    assert float(ev["avg_price_before"]) == 800.0
    assert ev["shares_after"] == 0
    assert ev["avg_price_after"] is None


def test_delete_manually_added_holding(client, db):
    _seed_stock_only(db)
    created = _add(client).json()
    r = client.delete(f"/api/v1/holdings/{created['id']}")
    assert r.status_code == 204
    assert _holding(db) is None
    assert [e["kind"] for e in _events(db)] == ["add", "delete"]


def test_delete_blocked_by_buy_linkage(client, db):
    _seed_stock_only(db)
    assert _buy(client).status_code == 201  # creates the holding
    holding = _holding(db)
    r = client.delete(f"/api/v1/holdings/{holding['id']}")
    assert r.status_code == 409
    # The holding stays, and no delete event is recorded.
    assert _holding(db) is not None
    assert [e["kind"] for e in _events(db)] == ["buy"]


def test_delete_blocked_by_sell_linkage(client, db):
    seed_stock_holding(db)  # 100 shares, snapshot
    assert _sale(client).status_code == 201  # 30 executed -> 70 left
    holding = _holding(db)
    assert holding["shares"] == 70
    r = client.delete(f"/api/v1/holdings/{holding['id']}")
    assert r.status_code == 409
    assert _holding(db)["shares"] == 70
    assert [e["kind"] for e in _events(db)] == ["sell"]


def test_delete_after_buy_cancelled_is_allowed(client, db):
    seed_stock_holding(db)  # snapshot: 100 @ 800
    buy = _buy(client).json()  # linked: 150 shares
    assert client.post(f"/api/v1/buys/{buy['id']}/cancel").status_code == 200
    holding = _holding(db)
    assert holding["shares"] == 100  # restored to the snapshot state
    r = client.delete(f"/api/v1/holdings/{holding['id']}")
    assert r.status_code == 204
    assert _holding(db) is None


def test_delete_unknown_id_is_404(client, db):
    r = client.delete("/api/v1/holdings/999999")
    assert r.status_code == 404


# ---------------------------------------------------------------- events


def test_holding_events_newest_first_with_fields(client, db):
    seed_stock_holding(db)  # 100 @ 800
    buy = _buy(client).json()  # +50 @ 1000 -> 150 @ 866.67
    sale = _sale(client).json()  # -30 -> 120 @ 866.67
    holding = _holding(db)

    r = client.get(f"/api/v1/holdings/{holding['id']}/events")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 2
    sell_ev, buy_ev = events  # newest first

    assert sell_ev["kind"] == "sell"
    assert sell_ev["source_type"] == "sale"
    assert sell_ev["source_id"] == sale["id"]
    assert sell_ev["delta_shares"] == -30
    assert sell_ev["shares_before"] == 150
    assert sell_ev["shares_after"] == 120
    assert float(sell_ev["avg_price_before"]) == pytest.approx(866.67, abs=0.01)
    assert float(sell_ev["avg_price_after"]) == pytest.approx(866.67, abs=0.01)
    assert sell_ev["reverses_event_id"] is None
    assert sell_ev["created_at"]

    assert buy_ev["kind"] == "buy"
    assert buy_ev["source_type"] == "buy_lot"
    assert buy_ev["source_id"] == buy["id"]
    assert buy_ev["delta_shares"] == 50
    assert buy_ev["shares_before"] == 100
    assert buy_ev["shares_after"] == 150
    assert float(buy_ev["avg_price_before"]) == 800.0
    assert float(buy_ev["avg_price_after"]) == pytest.approx(866.67, abs=0.01)


def test_holding_events_include_cancel_reversal(client, db):
    seed_stock_holding(db)
    buy = _buy(client).json()
    assert client.post(f"/api/v1/buys/{buy['id']}/cancel").status_code == 200
    holding = _holding(db)

    events = client.get(f"/api/v1/holdings/{holding['id']}/events").json()
    assert len(events) == 2
    cancel_ev, buy_ev = events
    assert cancel_ev["kind"] == "cancel"
    assert cancel_ev["source_type"] == "buy_lot"
    assert cancel_ev["reverses_event_id"] == buy_ev["id"]
    assert cancel_ev["delta_shares"] == -50
    assert cancel_ev["shares_before"] == 150
    assert cancel_ev["shares_after"] == 100
    assert float(cancel_ev["avg_price_after"]) == 800.0


def test_holding_events_for_manual_add(client, db):
    _seed_stock_only(db)
    created = _add(client).json()
    r = client.get(f"/api/v1/holdings/{created['id']}/events")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 1
    assert events[0]["kind"] == "add"
    assert events[0]["source_type"] == "manual"
    assert events[0]["code"] == "9999"
    assert events[0]["account_type"] == "特定"


def test_holding_events_unknown_id_is_404(client, db):
    r = client.get("/api/v1/holdings/999999/events")
    assert r.status_code == 404


def test_holding_events_scoped_per_account(client, db):
    seed_stock_holding(db)  # 特定
    db.execute(
        "INSERT INTO holdings(code, account_type, avg_price, shares)"
        " VALUES ('9999', 'NISA', 900, 20)"
    )
    db.commit()
    assert _buy(client).status_code == 201  # links 特定 only

    nisa = _holding(db, account_type="NISA")
    r = client.get(f"/api/v1/holdings/{nisa['id']}/events")
    assert r.status_code == 200
    assert r.json() == []

    tokutei = _holding(db, account_type="特定")
    events = client.get(f"/api/v1/holdings/{tokutei['id']}/events").json()
    assert len(events) == 1
    assert events[0]["kind"] == "buy"


# ---------------------------------------------------------------- page


def test_dashboard_holdings_controls(client, db):
    seed_stock_holding(db)
    r = client.get("/")
    assert r.status_code == 200
    page = r.text
    # Add button and its modal fields.
    assert "追加" in page
    for field in ("code", "account_type", "avg_price", "shares",
                  "lent_shares", "ordered_shares"):
        assert f'name="{field}"' in page, field
    # Per-row delete and history actions, wired to the holdings API.
    assert "削除" in page
    assert "/api/v1/holdings" in page
    assert "/events" in page
