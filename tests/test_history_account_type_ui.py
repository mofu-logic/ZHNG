"""History screen: the account type is shown and asked again.

Owner decision (revising the PR #44 round): the account type
(口座区分) returns to the history screen -- a column in both tables
and a required select in both add dialogs (which are modal dialogs
now; see tests/test_history_dialog.py for the full dialog spec).
Without it a screen registration could never name the account its
holding lives under, so screen trades stayed unlinked. The API
itself still accepts registrations without an account type; those
stay unlinked, as the existing rows are.

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


def test_history_page_shows_account_type(client, db):
    page = client.get("/history").text
    # A form input, a JS payload field and a visible label are all
    # back on the page.
    assert 'name="account_type"' in page
    assert "f.get('account_type')" in page
    assert "口座区分" in page
    # Both tables carry the account type column alongside the
    # neighbouring columns.
    heads = re.findall(r"<thead>(.*?)</thead>", page, re.S)
    assert len(heads) == 2
    for head in heads:
        assert "口座区分" in head
        assert "約定状態" in head
        assert "保有への反映" in head


def test_history_forms_have_account_type_input(client, db):
    page = client.get("/history").text
    for form_id in ("buy-form", "sell-form"):
        m = re.search(
            rf'<form[^>]*id="{form_id}"[^>]*>(.*?)</form>', page, re.S
        )
        assert m, form_id
        assert 'name="account_type"' in m.group(1), form_id


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
