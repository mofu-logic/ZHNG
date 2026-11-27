"""Per-operation failure messages for API writes.

PR #60 unified failure wording to the 〜できませんでした form, but
the shared apiJson helper (base.html) uses 保存できませんでした as
its only message, so on /history a failed cancel or delete also
reported a "save" failure. The user pointed out that a delete
failing with a save message feels wrong, so each operation now
carries its own message:

- /history cancel  -> 取り消しできませんでした
- /history delete  -> 削除できませんでした
- saves (the settings PUT and the buy/sell add forms on /history)
  keep the apiJson default 保存できませんでした

Inventory of the other write paths: they call fetch directly and
already pass a dedicated message to showDialog, so they are pinned
here as guards and do not change:

- dashboard: 追加できませんでした / 削除できませんでした /
  インポートできませんでした
- candidates: 登録できませんでした / 保存できませんでした /
  削除できませんでした
- master, benefit-memos: 保存できませんでした (their own dialogs)
"""
from conftest import seed_settings, seed_stock_holding


def test_history_cancel_failure_message(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/history").text
    assert "取り消しできませんでした" in page


def test_history_delete_failure_message(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/history").text
    assert "削除できませんでした" in page


def test_history_save_failure_message_kept(client, db):
    # The buy/sell add forms are saves through apiJson; their
    # failure wording stays 保存できませんでした.
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/history").text
    assert "保存できませんでした" in page


def test_dashboard_dedicated_failure_messages_kept(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/").text
    assert "追加できませんでした" in page
    assert "削除できませんでした" in page
    assert "インポートできませんでした" in page


def test_candidates_dedicated_failure_messages_kept(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/candidates").text
    assert "登録できませんでした" in page
    assert "保存できませんでした" in page
    assert "削除できませんでした" in page


def test_master_and_memo_save_failure_messages_kept(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    for path in ("/master", "/benefit-memos"):
        page = client.get(path).text
        assert "保存できませんでした" in page, path
