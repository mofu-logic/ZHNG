"""History form simplification: inputs the app can fill stay out.

Owner policy:
(1) Data that never appears in the table is not asked for.
(2) Values the app already knows are not typed in; the server fills
    them in at registration time.

What changes:
- Buy form: the name input goes away (and out of the submitted
  payload). When no name is sent, the server fills it from the stock
  master; a code the master does not know is fetched from Yahoo the
  same way purchase-candidate registration does it, and registered
  in the master. When the fetch fails the registration itself is a
  502 and nothing is stored. This applies to every registration
  without a name, including non-linked ones (no account type,
  unfilled orders) -- today the fetch only happens for linked buys.
  An explicitly sent name is used as-is, as before.
- Sell form: the name, buy_price, shares (buy lot size),
  purchase_date and profit inputs go away. When no buy_price is
  sent and the named account holds the stock, the holding's average
  price becomes the sale's buy_price and the profit is derived from
  it. With no holding (or no account type) buy_price and profit
  stay NULL. Explicitly sent buy_price / profit keep winning.
- The history tables themselves do not change in this PR.
  Follow-ups (owner decisions): the account type was first hidden
  on the history screen (PR #44), then brought back -- a required
  select in the add dialogs and a column in both tables -- because
  a trade that never names its account cannot be tied to a holding.
  The API still accepts registrations without one. The form tests
  below pin the current state; see tests/test_history_dialog.py and
  tests/test_history_account_type_ui.py.
"""
import re

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


def _seed_stock_only(db, code="9999", name="テスト銘柄"):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, '東証')",
        (code, name),
    )
    db.commit()


def _form_names(page, form_id):
    m = re.search(
        rf'<form[^>]*id="{form_id}"[^>]*>(.*?)</form>', page, re.S
    )
    assert m, form_id
    names = set()
    for tag in re.findall(r"<(?:input|select|textarea)[^>]*>", m.group(1)):
        name = re.search(r'name="([^"]+)"', tag)
        if name:
            names.add(name.group(1))
    return names


def _handler_js(page, form_id, end_marker):
    start = page.index(f"getElementById('{form_id}')")
    end = page.index(end_marker, start)
    return page[start:end]


# ------------------------------------------------------------------ forms


def test_buy_form_has_no_name_input(client, db):
    page = client.get("/history").text
    names = _form_names(page, "buy-form")
    assert "name" not in names
    # Everything else the buy table shows stays an input, including
    # the account type (asked again so screen trades can link to
    # holdings; see test_history_account_type_ui.py).
    for field in ("code", "account_type", "avg_price", "shares",
                  "purchase_date", "policy", "trigger_note", "note",
                  "status"):
        assert field in names, field


def test_sell_form_drops_filled_and_unlisted_inputs(client, db):
    page = client.get("/history").text
    names = _form_names(page, "sell-form")
    # name / buy_price are filled by the server; shares /
    # purchase_date never appear in the sell table; profit is
    # always derived now.
    for field in ("name", "buy_price", "shares", "purchase_date",
                  "profit"):
        assert field not in names, field
    for field in ("code", "account_type", "sell_price", "sell_shares",
                  "executed_shares", "order_start_date",
                  "executed_date", "status", "memo"):
        assert field in names, field


def test_forms_do_not_submit_removed_fields(client, db):
    page = client.get("/history").text
    buy_js = _handler_js(page, "buy-form", "getElementById('sell-form')")
    assert "f.get('name')" not in buy_js
    for kept in ("code", "account_type", "avg_price", "shares",
                 "purchase_date", "policy", "trigger_note", "note",
                 "status"):
        assert f"f.get('{kept}')" in buy_js, kept
    sell_js = _handler_js(page, "sell-form", "location.hash = 'sell'")
    for removed in ("name", "buy_price", "shares", "purchase_date",
                    "profit"):
        assert f"f.get('{removed}')" not in sell_js, removed
    for kept in ("code", "account_type", "sell_price", "sell_shares",
                 "executed_shares", "order_start_date",
                 "executed_date", "status", "memo"):
        assert f"f.get('{kept}')" in sell_js, kept


# ------------------------------------------------------- buy name filling


def test_buy_name_filled_from_master_when_omitted(client, db):
    # Non-linked registration (no account type, unfilled order): the
    # master fill is not limited to linked buys.
    _seed_stock_only(db)
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "avg_price": 900,
            "shares": 10,
            "purchase_date": "2026-11-20",
            "status": "注文中",
        },
    )
    assert r.status_code == 201
    assert r.json()["name"] == "テスト銘柄"
    row = db.execute("SELECT name FROM buy_lots").fetchone()
    assert row["name"] == "テスト銘柄"


def test_buy_name_omitted_unknown_code_fetches_and_registers(
    client, db, monkeypatch
):
    # A code the master does not know: fetched like candidate
    # registration, even though this buy is not linked to holdings.
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    r = client.post(
        "/api/v1/buys",
        json={"code": "7777", "avg_price": 900, "shares": 10},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "テスト候補(株)"
    stock = db.execute(
        "SELECT name, market FROM stocks WHERE code = '7777'"
    ).fetchone()
    assert stock is not None
    assert stock["name"] == "テスト候補(株)"
    assert stock["market"] == "東証"
    row = db.execute("SELECT name FROM buy_lots").fetchone()
    assert row["name"] == "テスト候補(株)"


def test_buy_name_omitted_unknown_code_fetch_failure_is_502(
    client, db, monkeypatch
):
    monkeypatch.setattr(
        main.httpx, "AsyncClient", make_fake_client(fail_codes=("0000",))
    )
    r = client.post(
        "/api/v1/buys",
        json={"code": "0000", "avg_price": 900, "shares": 10},
    )
    assert r.status_code == 502
    # Nothing is registered when the master info cannot be fetched.
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0
    assert db.execute("SELECT count(*) c FROM stocks").fetchone()["c"] == 0


def test_buy_explicit_name_wins_over_master(client, db):
    # Guard: an explicitly sent name is used as-is (the API keeps the
    # name field; only the form stops asking for it).
    _seed_stock_only(db)
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "name": "明示した名前",
            "avg_price": 900,
            "shares": 10,
            "status": "注文中",
        },
    )
    assert r.status_code == 201
    assert r.json()["name"] == "明示した名前"


# ------------------------------------------------------ sale name filling


def test_sale_name_filled_from_master_when_omitted(client, db):
    _seed_stock_only(db)
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "buy_price": 800,
            "sell_price": 1200,
            "sell_shares": 10,
            "executed_shares": 10,
            "status": "自動",
        },
    )
    assert r.status_code == 201
    assert r.json()["name"] == "テスト銘柄"
    row = db.execute("SELECT name FROM sales").fetchone()
    assert row["name"] == "テスト銘柄"


def test_sale_name_omitted_unknown_code_fetches_and_registers(
    client, db, monkeypatch
):
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "7777",
            "buy_price": 800,
            "sell_price": 1200,
            "sell_shares": 10,
            "executed_shares": 10,
            "status": "自動",
        },
    )
    assert r.status_code == 201
    assert r.json()["name"] == "テスト候補(株)"
    stock = db.execute(
        "SELECT name, market FROM stocks WHERE code = '7777'"
    ).fetchone()
    assert stock is not None
    assert stock["name"] == "テスト候補(株)"
    assert stock["market"] == "東証"
    row = db.execute("SELECT name FROM sales").fetchone()
    assert row["name"] == "テスト候補(株)"


def test_sale_name_omitted_unknown_code_fetch_failure_is_502(
    client, db, monkeypatch
):
    monkeypatch.setattr(
        main.httpx, "AsyncClient", make_fake_client(fail_codes=("0000",))
    )
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "0000",
            "buy_price": 800,
            "sell_price": 1200,
            "sell_shares": 10,
            "executed_shares": 10,
            "status": "自動",
        },
    )
    assert r.status_code == 502
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0
    assert db.execute("SELECT count(*) c FROM stocks").fetchone()["c"] == 0


# ------------------------------------------- sale buy_price / profit fill


def test_sale_buy_price_filled_from_holding_and_profit_derived(client, db):
    seed_stock_holding(db)  # 9999 特定: 100 shares @ avg 800
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "sell_price": 1200,
            "sell_shares": 30,
            "executed_shares": 30,
            "status": "自動",
            "account_type": "特定",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["buy_price"] == 800.0
    assert body["profit"] == 12000.0  # (1200 - 800) x 30
    row = db.execute("SELECT buy_price, profit FROM sales").fetchone()
    assert float(row["buy_price"]) == 800.0
    assert float(row["profit"]) == 12000.0
    # The linkage itself is unchanged: executed shares leave the
    # holding.
    holding = db.execute(
        "SELECT shares FROM holdings"
        " WHERE code = '9999' AND account_type = '特定'"
    ).fetchone()
    assert holding["shares"] == 70


def test_sale_buy_price_stays_null_without_holding(client, db):
    # Guard: the account is named but holds nothing -- there is no
    # average to fill from, so buy_price and profit stay NULL.
    _seed_stock_only(db)
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "sell_price": 1200,
            "sell_shares": 30,
            "executed_shares": 0,
            "status": "自動",
            "account_type": "特定",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["buy_price"] is None
    assert body["profit"] is None


def test_sale_buy_price_stays_null_without_account_type(client, db):
    # Guard: a holding exists under 特定, but the sale names no
    # account, so no average is borrowed from it.
    seed_stock_holding(db)
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "sell_price": 1200,
            "sell_shares": 30,
            "executed_shares": 30,
            "status": "自動",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["buy_price"] is None
    assert body["profit"] is None


def test_sale_explicit_buy_price_wins_over_holding_average(client, db):
    # Guard: an explicitly sent buy_price keeps winning over the
    # holding's average, and the profit derives from the explicit
    # price.
    seed_stock_holding(db)  # avg 800
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "buy_price": 500,
            "sell_price": 1200,
            "sell_shares": 30,
            "executed_shares": 30,
            "status": "自動",
            "account_type": "特定",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["buy_price"] == 500.0
    assert body["profit"] == 21000.0  # (1200 - 500) x 30
