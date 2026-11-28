"""Success result dialogs for add / delete / cancel operations.

PR #62 gave candidate registration a success dialog ("{code} {name}
を登録しました。"). The user asked for the same feedback on the
other add / delete / cancel operations, which until now reloaded
silently on success. Failure dialogs already exist for every
operation and do not change (they are pinned in
test_api_failure_messages.py).

The wording follows the PR #62 shape (a short title plus the full
sentence as the message), always naming the stock by code and name:

- 保有株の追加 (dashboard, POST /api/v1/holdings):
  title 追加しました / "{code} {name} を追加しました。"
  (code and name from the POST response)
- 保有の削除 (dashboard, DELETE /api/v1/holdings/{id}):
  title 削除しました / "{code} {name} を削除しました。"
  (the response is 204 with no body, so the values come from the
  delete button's dataset; the button gains data-code next to the
  data-name it already carries)
- 購入の追加 (history, POST /api/v1/buys):
  title 追加しました / "{code} {name} の購入を追加しました。"
- 売却の追加 (history, POST /api/v1/sales):
  title 追加しました / "{code} {name} の売却を追加しました。"
- 履歴の取消 (history, POST /api/v1/{buys,sales}/{id}/cancel):
  title 取り消しました /
  "{code} {name} の購入を取り消しました。" (売却 for sales)
  (code and name from the cancel response)
- 履歴の削除 (history, DELETE /api/v1/{buys,sales}/{id}):
  title 削除しました /
  "{code} {name} の購入を削除しました。" (売却 for sales)
  (204 response, so the buttons gain data-code / data-name)
- 候補の削除 (candidates, DELETE /api/v1/candidates/{code}):
  title 削除しました / "{code} {name} を削除しました。"
  (from the delete button's dataset, which already carries both)

In every case the success dialog is composed before the
location.reload() that refreshes the list, so the dialog is seen
first -- the same order as PR #62.
"""
from conftest import seed_settings, seed_stock_holding
from test_holdings_crud import _add, _buy, _seed_stock_only


# ------------------------------------------------------------ extraction

def _holding_add_handler(page):
    start = page.index("getElementById('holding-add-save')")
    end = page.index("querySelectorAll('[data-holding-delete]')", start)
    return page[start:end]


def _holding_delete_handler(page):
    start = page.index("querySelectorAll('[data-holding-delete]')")
    end = page.index("holding-import-overlay", start)
    return page[start:end]


def _buy_form_handler(page):
    start = page.index("getElementById('buy-form')")
    end = page.index("getElementById('sell-form')", start)
    return page[start:end]


def _sell_form_handler(page):
    start = page.index("getElementById('sell-form')")
    end = page.index("querySelectorAll('[data-cancel]')", start)
    return page[start:end]


def _cancel_handler(page):
    start = page.index("querySelectorAll('[data-cancel]')")
    end = page.index("querySelectorAll('[data-del]')", start)
    return page[start:end]


def _history_delete_handler(page):
    start = page.index("querySelectorAll('[data-del]')")
    end = page.index("const tabs", start)
    return page[start:end]


def _candidate_delete_handler(page):
    start = page.index("querySelectorAll('[data-del-candidate]')")
    return page[start:]


def _seed_history_rows(db):
    db.execute(
        "INSERT INTO buy_lots(code, name, avg_price, shares,"
        " purchase_date, status)"
        " VALUES ('9999', 'テスト銘柄', 1000, 50, '2026-11-20', '約定済')"
    )
    db.execute(
        "INSERT INTO sales(code, name, buy_price, sell_price,"
        " sell_shares, executed_shares, status)"
        " VALUES ('9999', 'テスト銘柄', 800, 1200, 30, 30, '約定済')"
    )
    db.commit()


# ------------------------------------------------------- dashboard (/)

def test_holding_add_success_dialog(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _holding_add_handler(client.get("/").text)
    assert "を追加しました。" in js
    # The wording is built from the POST response values.
    assert ".code" in js
    assert ".name" in js
    assert "showDialog({" in js
    assert js.index("を追加しました。") < js.index("location.reload()")


def test_holding_delete_success_dialog(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _holding_delete_handler(client.get("/").text)
    assert "を削除しました。" in js
    # 204 response: the wording comes from the button's dataset.
    assert "dataset.code" in js
    assert "dataset.name" in js
    # Confirmation, success and failure dialogs alike.
    assert js.count("showDialog({") >= 3
    assert js.index("を削除しました。") < js.index("location.reload()")


def test_holding_delete_button_carries_code(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/").text
    assert 'data-code="9999"' in page


# ------------------------------------------------------ history (/history)

def test_buy_add_success_dialog(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _buy_form_handler(client.get("/history").text)
    assert "の購入を追加しました。" in js
    assert ".code" in js
    assert ".name" in js
    assert "showDialog({" in js
    assert js.index("の購入を追加しました。") < js.index(
        "location.reload()"
    )


def test_sell_add_success_dialog(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _sell_form_handler(client.get("/history").text)
    assert "の売却を追加しました。" in js
    assert ".code" in js
    assert ".name" in js
    assert "showDialog({" in js
    assert js.index("の売却を追加しました。") < js.index(
        "location.reload()"
    )
    # The sell tab is restored after the reload, as before.
    assert "location.hash = 'sell'" in js


def test_history_cancel_success_dialog(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _cancel_handler(client.get("/history").text)
    assert "を取り消しました。" in js
    # The wording is built from the cancel response values.
    assert ".code" in js
    assert ".name" in js
    # Confirmation dialog (before) and success dialog (after).
    assert "を取り消しますか？" in js
    assert js.count("showDialog({") >= 2
    assert js.index("を取り消しました。") < js.index("location.reload()")


def test_history_delete_success_dialog(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _history_delete_handler(client.get("/history").text)
    assert "を削除しました。" in js
    # 204 response: the wording comes from the button's dataset.
    assert "dataset.code" in js
    assert "dataset.name" in js
    assert "showDialog({" in js
    assert js.index("を削除しました。") < js.index("location.reload()")


def test_history_delete_buttons_carry_code_and_name(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    _seed_history_rows(db)
    page = client.get("/history").text
    assert 'data-code="9999"' in page
    assert 'data-name="テスト銘柄"' in page


# ------------------------------------------------- candidates (/candidates)

def test_candidate_delete_success_dialog(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _candidate_delete_handler(client.get("/candidates").text)
    assert "を削除しました。" in js
    # Confirmation, success and failure dialogs alike.
    assert js.count("showDialog({") >= 3
    assert js.index("を削除しました。") < js.index("location.reload()")


# ------------------------------------------- data sources (guards, green)

def test_holding_add_response_carries_code_and_name(client, db):
    # The data source for the dashboard add message: the 201
    # response already returns the registered code and name.
    _seed_stock_only(db)
    r = _add(client)
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == "9999"
    assert body["name"] == "テスト銘柄"


def test_buy_cancel_response_carries_code_and_name(client, db):
    # The data source for the history cancel message: the cancel
    # response (BuyOut) already returns code and name.
    seed_stock_holding(db)
    r = _buy(client)
    assert r.status_code == 201
    lot_id = r.json()["id"]
    r = client.post(f"/api/v1/buys/{lot_id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "9999"
    assert body["name"] == "テスト銘柄"
