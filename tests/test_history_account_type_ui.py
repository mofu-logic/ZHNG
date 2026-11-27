"""History screen: the account type stays internal.

Owner decision: the account type (口座区分) is not shown or asked on
the history screen -- no column in either table, no select in either
add form, and the forms no longer submit it. Internally nothing
changes: the API still accepts account_type, and registrations that
name one keep linking to holdings exactly as before (covered by
test_holdings_linkage.py).

The same screen fix also keeps the status tag on one line: the
約定状態 cell gets the shared .nowrap class, the same mechanism the
date cells use (DESIGN.md: dates and datetimes never wrap).
"""
import re


def _seed_stock(db, code="9999", name="テスト銘柄"):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, '東証')",
        (code, name),
    )
    db.commit()


def _seed_buy_and_sale(client):
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "avg_price": 900,
            "shares": 10,
            "status": "約定済",
        },
    )
    assert r.status_code == 201
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "sell_price": 1200,
            "sell_shares": 10,
            "executed_shares": 10,
            "status": "約定済",
        },
    )
    assert r.status_code == 201


def test_history_page_hides_account_type(client, db):
    page = client.get("/history").text
    # No form input, no JS payload field, no visible label anywhere
    # on the page.
    assert 'name="account_type"' not in page
    assert "f.get('account_type')" not in page
    assert "口座区分" not in page
    # Both tables keep the neighbouring columns; only 口座区分 goes.
    heads = re.findall(r"<thead>(.*?)</thead>", page, re.S)
    assert len(heads) == 2
    for head in heads:
        assert "約定状態" in head
        assert "保有への反映" in head


def test_history_forms_have_no_account_type_input(client, db):
    page = client.get("/history").text
    for form_id in ("buy-form", "sell-form"):
        m = re.search(
            rf'<form[^>]*id="{form_id}"[^>]*>(.*?)</form>', page, re.S
        )
        assert m, form_id
        assert "account_type" not in m.group(1), form_id


def test_status_cells_do_not_wrap(client, db):
    _seed_stock(db)
    _seed_buy_and_sale(client)
    page = client.get("/history").text
    cells = re.findall(
        r'<td([^>]*)><span class="tag">約定済</span></td>', page
    )
    # One status cell in the buy table and one in the sell table.
    assert len(cells) == 2
    for attrs in cells:
        assert "nowrap" in attrs
