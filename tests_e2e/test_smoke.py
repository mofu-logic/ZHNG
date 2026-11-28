"""E2E スモークテスト。

主要フローを実ブラウザで通し、「配線はあるが動かない」問題を
固定する。対象は少数に絞る（追加・ソート・確認ダイアログ・連動）。
"""

from playwright.sync_api import expect

from helpers import (
    dialog_cancel,
    dialog_danger,
    dialog_message,
    dialog_ok,
    dialog_title,
    visible_dialog,
)

HOLDING_ROWS = "#holdings tbody tr"


def _row_by_code(page, code):
    return page.locator(HOLDING_ROWS).filter(has_text=code)


def _codes_in_order(page):
    return page.locator("#holdings tbody td.code").all_text_contents()


def test_holding_add_shows_result_dialog(page, app_server):
    """保有株の追加で結果ダイアログが出て、一覧に行が増える。"""
    page.goto(app_server + "/")
    expect(page.locator(HOLDING_ROWS)).to_have_count(3)

    page.click("#holding-add-open")
    form = page.locator("#holding-add-form")
    form.locator("input[name='code']").fill("4444")
    form.locator("select[name='account_type']").select_option("特定")
    form.locator("input[name='avg_price']").fill("2000")
    form.locator("input[name='shares']").fill("50")
    page.click("#holding-add-save")

    expect(dialog_message(page)).to_have_text("4444 ノート工業 を追加しました。")
    dialog_ok(page).click()

    expect(page.locator(HOLDING_ROWS)).to_have_count(4)
    expect(_row_by_code(page, "4444")).to_have_count(1)


def test_holdings_sort_reorders_rows(page, app_server):
    """ソート見出しのクリックで保有株一覧の行順が実際に変わる。"""
    page.goto(app_server + "/")
    # 既定順は銘柄コード順（シード: 1111=100株, 2222=200株, 3333=50株）
    assert [c.strip() for c in _codes_in_order(page)] == [
        "1111", "2222", "3333",
    ]

    header = page.locator("#holdings thead th", has_text="株数")
    header.click()  # 昇順: 50, 100, 200
    expect(page.locator("#holdings tbody tr td.code").first).to_have_text(
        "3333"
    )
    assert [c.strip() for c in _codes_in_order(page)] == [
        "3333", "1111", "2222",
    ]

    header.click()  # 降順: 200, 100, 50
    expect(page.locator("#holdings tbody tr td.code").first).to_have_text(
        "2222"
    )
    assert [c.strip() for c in _codes_in_order(page)] == [
        "2222", "1111", "3333",
    ]


def test_holding_delete_with_confirm_dialog(page, app_server):
    """確認ダイアログ有効時: 削除前に確認が出て、確定で結果が出る。"""
    page.goto(app_server + "/")
    row = _row_by_code(page, "1111")
    row.locator("button[data-holding-delete]").click()

    expect(dialog_title(page)).to_have_text("保有の削除")
    expect(dialog_message(page)).to_have_text(
        "テスト電気（特定）の保有を削除しますか？"
    )
    # キャンセルすると何も消えない
    dialog_cancel(page).click()
    expect(visible_dialog(page)).to_have_count(0)
    expect(page.locator(HOLDING_ROWS)).to_have_count(3)

    row.locator("button[data-holding-delete]").click()
    expect(dialog_title(page)).to_have_text("保有の削除")
    dialog_danger(page).click()

    expect(dialog_message(page)).to_have_text(
        "1111 テスト電気 を削除しました。"
    )
    dialog_ok(page).click()
    expect(page.locator(HOLDING_ROWS)).to_have_count(2)
    expect(_row_by_code(page, "1111")).to_have_count(0)


def test_holding_delete_without_confirm_dialog(page, app_server):
    """確認ダイアログ無効時: 確認なしで削除され、結果ダイアログが出る。"""
    # 設定画面でスイッチをオフにして保存する
    page.goto(app_server + "/settings")
    switch = page.locator("input.switch[name='confirm_dialogs_enabled']")
    expect(switch).to_be_checked()
    switch.uncheck()
    # 保存（PUT）の完了と再読み込みを待ってから遷移する。待たずに
    # 遷移すると PUT が中断され、設定が反映されないことがある。
    with page.expect_response(
        lambda r: r.request.method == "PUT" and "/api/v1/settings" in r.url
    ) as response_info:
        page.locator("#settings-form button[type='submit']").click()
    assert response_info.value.ok
    page.wait_for_load_state("load")
    expect(
        page.locator("input.switch[name='confirm_dialogs_enabled']")
    ).not_to_be_checked()

    page.goto(app_server + "/")
    _row_by_code(page, "2222").locator(
        "button[data-holding-delete]"
    ).click()

    # 確認ダイアログ（タイトル「保有の削除」）を挟まず、結果が直接出る
    expect(dialog_title(page)).to_have_text("削除しました")
    expect(dialog_message(page)).to_have_text(
        "2222 サンプル食品 を削除しました。"
    )
    dialog_ok(page).click()
    expect(page.locator(HOLDING_ROWS)).to_have_count(2)
    expect(_row_by_code(page, "2222")).to_have_count(0)


def test_buy_links_to_holding(page, app_server):
    """売買履歴から口座を選んで購入を登録すると、保有の株数が連動する。"""
    page.goto(app_server + "/history")
    page.click("#buy-add-open")
    form = page.locator("#buy-form")
    form.locator("input[name='code']").fill("4444")
    form.locator("select[name='account_type']").select_option("特定")
    form.locator("input[name='avg_price']").fill("1800")
    form.locator("input[name='shares']").fill("30")
    form.locator("input[name='purchase_date']").fill("2026-04-01")
    # 約定状態は既定の「約定済」（保有に連動する条件）
    form.locator("button[type='submit']").click()

    expect(dialog_message(page)).to_have_text(
        "4444 ノート工業 の購入を追加しました。"
    )
    dialog_ok(page).click()

    page.goto(app_server + "/")
    row = _row_by_code(page, "4444")
    expect(row).to_have_count(1)
    # 株数セル（6列目）: 連動で 30 株の保有ができている
    expect(row.locator("td").nth(5)).to_have_text("30")
