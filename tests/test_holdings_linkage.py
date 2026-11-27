"""Holdings linkage （売買履歴と保有の連動） and cancellation.

Domain rules under test:
- buy_lots and sales carry an account_type （特定 / NISA). A trade
  without one (including every pre-existing row, whose account_type
  stays NULL) never touches holdings.
- A buy links only when its final status is 約定済: it creates the
  holdings row for (code, account_type) or adds to it with a
  weighted-average price, recording a holding_events row. When the
  code is not in the stock master yet, the master row is fetched
  from Yahoo (same as purchase-candidate registration); if the
  fetch fails the buy itself is rejected with 502.
- A sale links by executed_shares: it subtracts from the holding
  and deletes the row at zero shares. Exceeding the holding (or
  having no holding at all) is a 422 and registers nothing.
- Cancelling a trade (POST .../cancel) marks it cancelled_at and,
  when it has a linkage event that is still the latest event for
  that holding, posts a reversal event (kind "cancel",
  reverses_event_id -> the original) restoring the holding exactly
  (shares and average price; a row deleted by a sale comes back, a
  row created by a buy disappears). With a later event in between
  the cancel is a 409, as is a second cancel.
- Deleting a linked, uncancelled trade rolls the linkage back
  first (kind "delete" reversal event, same 409 condition);
  deleting a cancelled trade leaves holdings alone.

Conventions pinned here for the event log: delta_shares is signed
(buy +, sale -), shares_before is 0 and avg_price_before is NULL
when the holding did not exist, and avg_price_after is NULL when
the holding no longer exists after the event.
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
    exists = db.execute(
        "SELECT to_regclass('holding_events') AS t"
    ).fetchone()["t"]
    if not exists:
        return []
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


def _buy(client, **over):
    payload = {
        "code": "9999",
        "name": "テスト銘柄",
        "avg_price": 1000,
        "shares": 100,
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
        "sell_shares": 100,
        "executed_shares": 30,
        "status": "自動",
        "account_type": "特定",
    }
    payload.update(over)
    return client.post("/api/v1/sales", json=payload)


# ---------------------------------------------------------------- OpenAPI


def test_openapi_cancel_paths_and_trade_fields(client, db):
    doc = client.get("/openapi.json").json()
    paths = doc["paths"]
    for p in ("/api/v1/buys/{lot_id}/cancel", "/api/v1/sales/{sale_id}/cancel"):
        assert p in paths, p
        assert "post" in paths[p], p
    schemas = doc["components"]["schemas"]
    for name in ("BuyIn", "SaleIn"):
        assert "account_type" in schemas[name]["properties"], name
    for name in ("BuyOut", "SaleOut"):
        props = schemas[name]["properties"]
        assert "account_type" in props, name
        assert "cancelled_at" in props, name


# ---------------------------------------------------------------- buy linkage


def test_buy_executed_creates_holding_and_event(client, db):
    _seed_stock_only(db)
    r = _buy(client)
    assert r.status_code == 201
    body = r.json()
    assert body["account_type"] == "特定"
    assert body["cancelled_at"] is None

    holding = _holding(db)
    assert holding is not None
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 1000.0

    events = _events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev["code"] == "9999"
    assert ev["account_type"] == "特定"
    assert ev["kind"] == "buy"
    assert ev["source_type"] == "buy_lot"
    assert ev["source_id"] == body["id"]
    assert ev["delta_shares"] == 100
    assert ev["shares_before"] == 0
    assert ev["shares_after"] == 100
    assert ev["avg_price_before"] is None
    assert float(ev["avg_price_after"]) == 1000.0
    assert ev["reverses_event_id"] is None
    assert ev["created_at"] is not None


def test_buy_executed_adds_with_weighted_average(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    r = _buy(client)
    assert r.status_code == 201

    holding = _holding(db)
    assert holding["shares"] == 200
    # (800*100 + 1000*100) / 200
    assert float(holding["avg_price"]) == 900.0

    events = _events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev["delta_shares"] == 100
    assert ev["shares_before"] == 100
    assert ev["shares_after"] == 200
    assert float(ev["avg_price_before"]) == 800.0
    assert float(ev["avg_price_after"]) == 900.0


def test_buy_default_status_links(client, db):
    # No status in the payload: the stored status defaults to 約定済,
    # so the buy links.
    _seed_stock_only(db)
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "avg_price": 500,
            "shares": 10,
            "account_type": "NISA",
        },
    )
    assert r.status_code == 201
    assert r.json()["status"] == "約定済"
    holding = _holding(db, account_type="NISA")
    assert holding is not None
    assert holding["shares"] == 10
    assert float(holding["avg_price"]) == 500.0
    assert len(_events(db)) == 1


@pytest.mark.parametrize("status", ["注文中", "一部約定"])
def test_buy_unexecuted_does_not_link(client, db, status):
    _seed_stock_only(db)
    r = _buy(client, status=status)
    assert r.status_code == 201
    assert r.json()["account_type"] == "特定"
    assert _holding(db) is None
    assert _events(db) == []


def test_buy_without_account_type_does_not_link(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "avg_price": 1000,
            "shares": 100,
            "status": "約定済",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["account_type"] is None
    assert body["cancelled_at"] is None
    holding = _holding(db)
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0
    assert _events(db) == []


def test_buy_links_only_its_account(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    r = _buy(client, account_type="NISA", avg_price=1200, shares=50)
    assert r.status_code == 201
    nisa = _holding(db, account_type="NISA")
    assert nisa is not None
    assert nisa["shares"] == 50
    assert float(nisa["avg_price"]) == 1200.0
    tokutei = _holding(db, account_type="特定")
    assert tokutei["shares"] == 100
    assert float(tokutei["avg_price"]) == 800.0


def test_buy_unknown_code_fetches_stock_info(client, db, monkeypatch):
    # The code is not in the stock master: registering the buy (with
    # linkage) fetches the master row from Yahoo, like candidate
    # registration. (Verified without a name in the payload; when a
    # name is supplied the fetch-vs-supplied-name precedence is not
    # pinned by the spec.)
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "7777",
            "avg_price": 900,
            "shares": 10,
            "status": "約定済",
            "account_type": "特定",
        },
    )
    assert r.status_code == 201
    stock = db.execute(
        "SELECT name, market FROM stocks WHERE code = '7777'"
    ).fetchone()
    assert stock is not None
    assert stock["name"] == "テスト候補(株)"
    assert stock["market"] == "東証"
    holding = _holding(db, code="7777")
    assert holding is not None
    assert holding["shares"] == 10
    assert float(holding["avg_price"]) == 900.0
    assert len(_events(db)) == 1


def test_buy_unknown_code_fetch_failure_is_502(client, db, monkeypatch):
    monkeypatch.setattr(
        main.httpx, "AsyncClient", make_fake_client(fail_codes=("0000",))
    )
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "0000",
            "avg_price": 900,
            "shares": 10,
            "status": "約定済",
            "account_type": "特定",
        },
    )
    assert r.status_code == 502
    # Nothing is registered when the master info cannot be fetched.
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0
    assert db.execute("SELECT count(*) c FROM stocks").fetchone()["c"] == 0
    assert db.execute("SELECT count(*) c FROM holdings").fetchone()["c"] == 0
    assert _events(db) == []


# ---------------------------------------------------------------- sale linkage


def test_sale_executed_shares_reduce_holding(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    r = _sale(client)
    assert r.status_code == 201
    body = r.json()
    assert body["account_type"] == "特定"
    assert body["cancelled_at"] is None

    holding = _holding(db)
    assert holding["shares"] == 70
    assert float(holding["avg_price"]) == 800.0  # a sale never moves the average

    events = _events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev["code"] == "9999"
    assert ev["account_type"] == "特定"
    assert ev["kind"] == "sell"
    assert ev["source_type"] == "sale"
    assert ev["source_id"] == body["id"]
    assert ev["delta_shares"] == -30
    assert ev["shares_before"] == 100
    assert ev["shares_after"] == 70
    assert float(ev["avg_price_before"]) == 800.0
    assert float(ev["avg_price_after"]) == 800.0
    assert ev["reverses_event_id"] is None


def test_sale_all_shares_deletes_holding(client, db):
    seed_stock_holding(db)
    r = _sale(client, executed_shares=100)
    assert r.status_code == 201
    assert _holding(db) is None
    events = _events(db)
    assert len(events) == 1
    assert events[0]["shares_after"] == 0
    assert events[0]["avg_price_after"] is None


def test_sale_exceeding_holding_is_422(client, db):
    seed_stock_holding(db)  # 100 shares
    r = _sale(client, executed_shares=101, sell_shares=101)
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0
    assert _holding(db)["shares"] == 100
    assert _events(db) == []


def test_sale_without_holding_is_422(client, db):
    _seed_stock_only(db)
    r = _sale(client, executed_shares=10)
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0
    assert _events(db) == []


def test_sale_wrong_account_is_422(client, db):
    seed_stock_holding(db)  # 特定 only
    r = _sale(client, account_type="NISA", executed_shares=10)
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0
    assert _holding(db)["shares"] == 100


def test_sale_zero_executed_does_not_link(client, db):
    seed_stock_holding(db)
    r = _sale(client, executed_shares=0)
    assert r.status_code == 201
    assert r.json()["account_type"] == "特定"
    assert _holding(db)["shares"] == 100
    assert _events(db) == []


def test_sale_without_account_type_does_not_link(client, db):
    seed_stock_holding(db)
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "buy_price": 800,
            "sell_price": 1200,
            "sell_shares": 100,
            "executed_shares": 30,
            "status": "自動",
        },
    )
    assert r.status_code == 201
    assert r.json()["account_type"] is None
    assert _holding(db)["shares"] == 100
    assert _events(db) == []


# ---------------------------------------------------------------- cancel: buys


def test_cancel_buy_restores_new_holding(client, db):
    _seed_stock_only(db)
    buy_id = _buy(client).json()["id"]
    original = _events(db)[0]

    r = client.post(f"/api/v1/buys/{buy_id}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled_at"] is not None

    # The holding row created by the buy is gone again.
    assert _holding(db) is None

    events = _events(db)
    assert len(events) == 2
    rev = events[1]
    assert rev["kind"] == "cancel"
    assert rev["source_type"] == "buy_lot"
    assert rev["source_id"] == buy_id
    assert rev["reverses_event_id"] == original["id"]
    assert rev["delta_shares"] == -100
    assert rev["shares_before"] == 100
    assert rev["shares_after"] == 0
    assert float(rev["avg_price_before"]) == 1000.0
    assert rev["avg_price_after"] is None

    listed = client.get("/api/v1/buys").json()
    assert listed[0]["cancelled_at"] is not None


def test_cancel_buy_restores_existing_holding(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    buy_id = _buy(client).json()["id"]
    assert float(_holding(db)["avg_price"]) == 900.0

    r = client.post(f"/api/v1/buys/{buy_id}/cancel")
    assert r.status_code == 200
    holding = _holding(db)
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0


def test_cancel_twice_is_409(client, db):
    _seed_stock_only(db)
    buy_id = _buy(client).json()["id"]
    assert client.post(f"/api/v1/buys/{buy_id}/cancel").status_code == 200
    assert client.post(f"/api/v1/buys/{buy_id}/cancel").status_code == 409


def test_cancel_unknown_id_is_404(client, db):
    assert client.post("/api/v1/buys/424242/cancel").status_code == 404
    assert client.post("/api/v1/sales/424242/cancel").status_code == 404


def test_cancel_blocked_by_later_event(client, db):
    _seed_stock_only(db)
    buy1 = _buy(client, avg_price=1000, shares=100).json()["id"]
    buy2 = _buy(client, avg_price=1200, shares=100).json()["id"]
    holding = _holding(db)
    assert holding["shares"] == 200
    assert float(holding["avg_price"]) == 1100.0

    # buy1's event is no longer the latest for this holding.
    assert client.post(f"/api/v1/buys/{buy1}/cancel").status_code == 409
    holding = _holding(db)
    assert holding["shares"] == 200
    assert float(holding["avg_price"]) == 1100.0
    buys = {b["id"]: b for b in client.get("/api/v1/buys").json()}
    assert buys[buy1]["cancelled_at"] is None

    # Cancelling in reverse order works.
    assert client.post(f"/api/v1/buys/{buy2}/cancel").status_code == 200
    holding = _holding(db)
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 1000.0
    assert client.post(f"/api/v1/buys/{buy1}/cancel").status_code == 200
    assert _holding(db) is None


def test_cancel_blocked_by_later_event_of_other_kind(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    sale_id = _sale(client, executed_shares=30).json()["id"]
    buy_id = _buy(client, avg_price=900, shares=10).json()["id"]
    # The buy event is now the latest for this holding.
    assert client.post(f"/api/v1/sales/{sale_id}/cancel").status_code == 409
    assert client.post(f"/api/v1/buys/{buy_id}/cancel").status_code == 200
    assert client.post(f"/api/v1/sales/{sale_id}/cancel").status_code == 200
    holding = _holding(db)
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0


def test_cancel_unlinked_buy_marks_only(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    plain = client.post(
        "/api/v1/buys",
        json={"code": "9999", "name": "テスト銘柄", "status": "約定済"},
    ).json()
    pending = _buy(client, status="注文中").json()
    for buy_id in (plain["id"], pending["id"]):
        r = client.post(f"/api/v1/buys/{buy_id}/cancel")
        assert r.status_code == 200
        assert r.json()["cancelled_at"] is not None
    holding = _holding(db)
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0
    assert _events(db) == []


# ---------------------------------------------------------------- cancel: sales


def test_cancel_sale_restores_holding(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    sale_id = _sale(client, executed_shares=30).json()["id"]
    assert _holding(db)["shares"] == 70
    original = _events(db)[0]

    r = client.post(f"/api/v1/sales/{sale_id}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled_at"] is not None

    holding = _holding(db)
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0

    events = _events(db)
    assert len(events) == 2
    rev = events[1]
    assert rev["kind"] == "cancel"
    assert rev["source_type"] == "sale"
    assert rev["source_id"] == sale_id
    assert rev["reverses_event_id"] == original["id"]
    assert rev["delta_shares"] == 30
    assert rev["shares_before"] == 70
    assert rev["shares_after"] == 100


def test_cancel_sale_revives_deleted_holding(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    sale_id = _sale(client, executed_shares=100).json()["id"]
    assert _holding(db) is None

    r = client.post(f"/api/v1/sales/{sale_id}/cancel")
    assert r.status_code == 200
    holding = _holding(db)
    assert holding is not None
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0

    rev = _events(db)[1]
    assert rev["shares_before"] == 0
    assert rev["shares_after"] == 100
    assert rev["avg_price_before"] is None
    assert float(rev["avg_price_after"]) == 800.0


def test_cancel_sale_twice_is_409(client, db):
    seed_stock_holding(db)
    sale_id = _sale(client, executed_shares=30).json()["id"]
    assert client.post(f"/api/v1/sales/{sale_id}/cancel").status_code == 200
    assert client.post(f"/api/v1/sales/{sale_id}/cancel").status_code == 409


# ---------------------------------------------------------------- delete rollback


def test_delete_linked_buy_rolls_back(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    buy_id = _buy(client).json()["id"]
    original = _events(db)[0]
    assert float(_holding(db)["avg_price"]) == 900.0

    assert client.delete(f"/api/v1/buys/{buy_id}").status_code == 204
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0
    holding = _holding(db)
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0

    events = _events(db)
    assert len(events) == 2
    rev = events[1]
    assert rev["kind"] == "delete"
    assert rev["reverses_event_id"] == original["id"]
    assert rev["delta_shares"] == -100
    assert rev["shares_after"] == 100
    assert float(rev["avg_price_after"]) == 800.0


def test_delete_buy_blocked_by_later_event(client, db):
    _seed_stock_only(db)
    buy1 = _buy(client, avg_price=1000, shares=100).json()["id"]
    _buy(client, avg_price=1200, shares=100)
    assert client.delete(f"/api/v1/buys/{buy1}").status_code == 409
    # Nothing was deleted or rolled back.
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 2
    assert _holding(db)["shares"] == 200


def test_delete_cancelled_buy_does_not_touch_holdings(client, db):
    _seed_stock_only(db)
    buy_id = _buy(client).json()["id"]
    assert client.post(f"/api/v1/buys/{buy_id}/cancel").status_code == 200
    assert _holding(db) is None
    assert client.delete(f"/api/v1/buys/{buy_id}").status_code == 204
    assert _holding(db) is None
    # Only the buy event and its cancel reversal exist.
    assert len(_events(db)) == 2


def test_delete_linked_sale_rolls_back(client, db):
    seed_stock_holding(db)  # 特定: 100 shares @ 800
    sale_id = _sale(client, executed_shares=40).json()["id"]
    assert _holding(db)["shares"] == 60

    assert client.delete(f"/api/v1/sales/{sale_id}").status_code == 204
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0
    holding = _holding(db)
    assert holding["shares"] == 100
    assert float(holding["avg_price"]) == 800.0
    events = _events(db)
    assert len(events) == 2
    assert events[1]["kind"] == "delete"
    assert events[1]["reverses_event_id"] == events[0]["id"]


def test_delete_unlinked_buy_still_works(client, db):
    r = client.post(
        "/api/v1/buys", json={"code": "9999", "name": "テスト銘柄"}
    )
    buy_id = r.json()["id"]
    assert client.delete(f"/api/v1/buys/{buy_id}").status_code == 204
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0
    assert _events(db) == []


# ---------------------------------------------------------------- history page


def test_history_page_linkage_ui(client, db):
    seed_stock_holding(db)
    linked = _buy(client).json()
    client.post(
        "/api/v1/buys",
        json={"code": "9999", "name": "テスト銘柄", "status": "約定済"},
    )

    page = client.get("/history").text
    # Both add forms take an account type.
    assert page.count('name="account_type"') >= 2
    assert "口座区分" in page
    # Row states: the linked buy is reflected, the plain one is out
    # of scope, and each row offers a cancel action.
    assert "反映済み" in page
    assert "対象外" in page
    assert "取消" in page

    assert client.post(f"/api/v1/buys/{linked['id']}/cancel").status_code == 200
    page = client.get("/history").text
    assert "取消済み" in page
