"""History screen: the trade tables' column layout.

Two owner decisions about the /history tables:

- The link-state column （保有への反映） is gone. It came in with
  the holdings linkage (PR #33) and tagged every row 反映済み /
  対象外 / 取消済み, but in production all 143 rows read 対象外 --
  the pre-linkage rows carry no account type and no screen
  registration had linked yet -- so the column carried no
  information. The linkage itself is unchanged: a trade registered
  with an account type still moves the holding, cancel/delete
  still roll it back, and holding_events / cancelled_at stay (the
  linkage suite pins those behaviours). A cancelled row is still
  told apart on screen, by losing its cancel button -- that
  rendering keys off cancelled_at directly.

- The account column moved next to the stock name and its header
  shortened to 口座, matching the holdings table on the dashboard.
  The cell values （特定 / NISA / 普通, empty when the trade has no
  account type) are unchanged, and so are the add dialogs, whose
  label stays 口座区分. The column stays sortable through the
  shared header wiring (the sort suite pins the full header
  sequence, labels and data-types included).
"""
import re


def _seed_stock(db, code="9999", name="テスト銘柄"):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, '東証')",
        (code, name),
    )
    db.commit()


def _post_buy(client, **over):
    body = {
        "code": "9999",
        "name": "テスト銘柄",
        "avg_price": 1000,
        "shares": 100,
        "status": "約定済",
    }
    body.update(over)
    r = client.post("/api/v1/buys", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _post_sale(client, **over):
    body = {
        "code": "9999",
        "name": "テスト銘柄",
        "sell_price": 1200,
        "sell_shares": 10,
        "executed_shares": 10,
        "status": "約定済",
    }
    body.update(over)
    r = client.post("/api/v1/sales", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _heads(page):
    return re.findall(r"<thead>(.*?)</thead>", page, re.S)


def _labels(head):
    return [t.strip() for t in re.findall(r"<th[^>]*>([^<]*)</th>", head)]


def test_history_tables_have_no_link_state_column(client, db):
    _seed_stock(db)
    # One linked buy (account type named, so it moves the holding),
    # one unlinked buy (no account type, the pre-linkage shape) and
    # one linked sale against the holding the first buy created.
    _post_buy(client, account_type="特定")
    _post_buy(client, avg_price=900, shares=10)
    _post_sale(client, account_type="特定")

    page = client.get("/history").text
    heads = _heads(page)
    assert len(heads) == 2
    for head in heads:
        assert "保有への反映" not in head
        # The neighbouring columns stay.
        assert "約定状態" in head
    # No row carries a state tag any more, whichever kind it is.
    assert "反映済み" not in page
    assert "対象外" not in page
    # Every live row keeps both actions.
    assert page.count('data-cancel="buys"') == 2
    assert page.count('data-del="buys"') == 2
    assert page.count('data-cancel="sales"') == 1
    assert page.count('data-del="sales"') == 1


def test_history_cancelled_row_renders_without_state_tag(client, db):
    _seed_stock(db)
    buy = _post_buy(client, account_type="特定")
    r = client.post(f"/api/v1/buys/{buy['id']}/cancel")
    assert r.status_code == 200, r.text

    page = client.get("/history").text
    # The cancelled row renders like any other row: no state tags.
    assert "取消済み" not in page
    assert "反映済み" not in page
    assert "対象外" not in page
    assert "テスト銘柄" in page
    # Cancellation still shows the one way it always did besides
    # the tag: the row loses its cancel button and keeps delete.
    assert page.count('data-cancel="buys"') == 0
    assert page.count('data-del="buys"') == 1


def test_history_account_column_follows_stock_name(client, db):
    _seed_stock(db)
    _post_buy(client, account_type="特定")
    _post_sale(client, account_type="特定")

    page = client.get("/history").text
    heads = _heads(page)
    assert len(heads) == 2
    for head in heads:
        labels = _labels(head)
        # The header is the short 口座 (never 口座区分 in a table),
        # sitting right after the stock name, as on the dashboard.
        assert "口座区分" not in labels
        assert labels.index("口座") == labels.index("銘柄名") + 1
