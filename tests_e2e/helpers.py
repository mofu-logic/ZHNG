"""画面テスト共通の小さなヘルパー。"""


def _dialog(page):
    """showDialog（base.html）が動的生成するダイアログのうち最前面のもの。

    各画面の入力ダイアログは <main> 内に静的に置かれ、開いている
    間も .dlg が見えるため、body 直下という位置で区別する。さらに
    確認ダイアログの除去（150ms 後）と結果ダイアログの表示が重なる
    瞬間があるため、DOM で最後＝最前面の1つに絞る。
    """
    return page.locator("body > .dlg-overlay").last.locator(".dlg")


def visible_dialog(page):
    """body 直下に動的生成されたダイアログすべて（件数確認用）。"""
    return page.locator("body > .dlg-overlay .dlg")


def dialog_title(page):
    return _dialog(page).locator(".dlg-title")


def dialog_message(page):
    return _dialog(page).locator(".dlg-msg")


def dialog_ok(page):
    """ダイアログの OK 系ボタン（キャンセル以外のボタン）。"""
    return _dialog(page).locator(".dlg-btns button:not(.dlg-cancel)")


def dialog_cancel(page):
    return _dialog(page).locator(".dlg-cancel")


def dialog_danger(page):
    """確認ダイアログの確定ボタン（danger 指定の OK ボタン）。"""
    return _dialog(page).locator("button.dlg-danger")


def single_line_info(page, selector):
    """各要素のテキストが何行で描画されているかを返す。

    折り返し（複数行）の検出は Range の矩形数で行う。テキストを
    持たない要素は lines=0 として返す。
    """
    return page.locator(selector).evaluate_all(
        """(els) => els.map(el => {
          const node = [...el.childNodes].find(
            n => n.nodeType === 3 && n.textContent.trim());
          if (!node) return {text: el.textContent.trim(), lines: 0};
          const range = document.createRange();
          range.selectNodeContents(node);
          return {
            text: node.textContent.trim(),
            lines: range.getClientRects().length,
          };
        })"""
    )
