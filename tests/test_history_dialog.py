"""History screen: add forms become dialogs, account type returns.

Owner decisions behind this PR:
- The buy/sell add forms leave the top of the page and become modal
  dialogs in the same shell as the holdings add form (dashboard):
  a .dlg-overlay wrapping a .dlg with role="dialog", opened by
  "購入を追加" / "売却を追加" buttons. The fields move to a vertical
  layout and each field carries a short hint saying what it is for.
  Field names and the JSON payloads stay exactly as they were.
- The account type (口座区分) is asked again -- the holdings key is
  (code, account type), so a trade that never names its account can
  never be tied to a holding. On this screen it is required: the
  select starts on an empty "選択してください" option and the
  options are rendered on the server from ACCOUNT_TYPES. The API
  itself still accepts registrations without an account type (they
  stay unlinked, as the existing rows are).
- Both history tables show the account type again; rows registered
  without one render an empty cell.
- Long memos render like the purchase candidates' memo: first 20
  chars + …, full text in the .memo-tip hover tooltip. Short memos
  render as-is.

Two existing files pin the previous round of decisions and must be
revised together with the implementation (they assert the exact
opposite of the dialog / account-type parts above):
- tests/test_history_account_type_ui.py (PR #44: account type hidden)
- tests/test_history_form_simplify.py (form field sets and payloads
  without account_type)
"""
import re
from html.parser import HTMLParser

from conftest import seed_stock_holding

from app.config import ACCOUNT_TYPES


# ------------------------------------------------------------- page tree

_VOID_TAGS = {"input", "br", "img", "meta", "link", "hr"}


class _El:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _El("[root]", {}, None)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        el = _El(tag, attrs, self._stack[-1])
        self._stack[-1].children.append(el)
        if tag not in _VOID_TAGS:
            self._stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_El(tag, attrs, self._stack[-1]))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def _parse(page):
    tree = _Tree()
    tree.feed(page)
    return tree.root


def _walk(el):
    yield el
    for child in el.children:
        if isinstance(child, _El):
            yield from _walk(child)


def _text(el):
    parts = []
    for child in el.children:
        if isinstance(child, _El):
            parts.append(_text(child))
        else:
            parts.append(child)
    return "".join(parts)


def _has_class(el, name):
    return name in (el.attrs.get("class") or "").split()


def _find_by_id(root, element_id):
    for el in _walk(root):
        if el.attrs.get("id") == element_id:
            return el
    return None


def _ancestors(el):
    node = el.parent
    while node is not None:
        yield node
        node = node.parent


def _is_inside(el, ancestor):
    node = el
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


# ------------------------------------------------------------ form blocks


def _form_block(page, form_id):
    m = re.search(
        rf'<form[^>]*id="{form_id}"[^>]*>(.*?)</form>', page, re.S
    )
    assert m, form_id
    return m.group(1)


def _field_text(block, field):
    """Visible text of one field's block: its label plus any hint
    element placed with it, up to the next field. Whitespace is
    removed so phrase checks survive markup line breaks."""
    idx = block.index(f'name="{field}"')
    start = block.rfind("<label", 0, idx)
    if start < 0:
        start = idx
    nxt = block.find("<label", idx)
    chunk = block[start:nxt if nxt >= 0 else len(block)]
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", chunk))


def _handler_js(page, form_id, end_marker):
    start = page.index(f"getElementById('{form_id}')")
    end = page.index(end_marker, start)
    return page[start:end]


def _seed_stock(db, code="9999", name="テスト銘柄"):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, '東証')",
        (code, name),
    )
    db.commit()


# ---------------------------------------------------------------- dialogs


def test_add_forms_live_in_modal_dialogs(client, db):
    root = _parse(client.get("/history").text)
    for form_id, title_word in (("buy-form", "購入"), ("sell-form", "売却")):
        form = _find_by_id(root, form_id)
        assert form is not None, form_id
        chain = list(_ancestors(form))
        dlg = next((a for a in chain if _has_class(a, "dlg")), None)
        assert dlg is not None, form_id
        assert dlg.attrs.get("role") == "dialog", form_id
        assert any(_has_class(a, "dlg-overlay") for a in chain), form_id
        titles = [el for el in _walk(dlg) if _has_class(el, "dlg-title")]
        assert titles and title_word in _text(titles[0]), form_id


def test_dialog_open_buttons_exist_outside_forms(client, db):
    root = _parse(client.get("/history").text)
    forms = {
        fid: _find_by_id(root, fid) for fid in ("buy-form", "sell-form")
    }
    buttons = [el for el in _walk(root) if el.tag == "button"]
    for label, form_id in (
        ("購入を追加", "buy-form"),
        ("売却を追加", "sell-form"),
    ):
        openers = [
            b
            for b in buttons
            if _text(b).strip() == label
            and not _is_inside(b, forms[form_id])
        ]
        assert openers, label


# ------------------------------------------------------- account type UI


def _account_type_select(root, form_id):
    form = _find_by_id(root, form_id)
    assert form is not None, form_id
    for el in _walk(form):
        if el.tag == "select" and el.attrs.get("name") == "account_type":
            return el
    return None


def _check_account_type_select(client, form_id):
    root = _parse(client.get("/history").text)
    select = _account_type_select(root, form_id)
    assert select is not None, form_id
    # Required on this screen: no registration without an account.
    assert "required" in select.attrs, form_id
    options = [
        c
        for c in select.children
        if isinstance(c, _El) and c.tag == "option"
    ]
    assert options, form_id
    # The first option is the empty placeholder; nothing is
    # pre-selected, so the user always chooses deliberately.
    first = options[0]
    assert first.attrs.get("value", "") == "", form_id
    assert _text(first).strip() == "選択してください", form_id
    assert all("selected" not in o.attrs for o in options[1:]), form_id
    values = [o.attrs.get("value", _text(o).strip()) for o in options[1:]]
    assert values == list(ACCOUNT_TYPES), form_id


def test_buy_form_account_type_select(client, db):
    _check_account_type_select(client, "buy-form")


def test_sell_form_account_type_select(client, db):
    _check_account_type_select(client, "sell-form")


def test_forms_submit_account_type(client, db):
    page = client.get("/history").text
    buy_js = _handler_js(page, "buy-form", "getElementById('sell-form')")
    assert "f.get('account_type')" in buy_js
    sell_js = _handler_js(page, "sell-form", "location.hash = 'sell'")
    assert "f.get('account_type')" in sell_js


# ------------------------------------------------------------------ hints


def test_buy_form_field_hints(client, db):
    block = _form_block(client.get("/history").text, "buy-form")
    # 銘柄コード: the name is filled from the stock master.
    assert "マスタから自動" in _field_text(block, "code")
    # 購入方針 / 購入契機 / メモ: a hint beyond the bare label, saying
    # what belongs in the field.
    for field, label in (
        ("policy", "購入方針"),
        ("trigger_note", "購入契機"),
        ("note", "メモ"),
    ):
        text = _field_text(block, field)
        assert text.startswith(label), field
        assert len(text) - len(label) >= 4, field


def test_sell_form_field_hints(client, db):
    block = _form_block(client.get("/history").text, "sell-form")
    assert "マスタから自動" in _field_text(block, "code")
    # 約定株数: the buy price comes from the holding's average and
    # the profit is derived from it.
    executed = _field_text(block, "executed_shares")
    assert "平均取得単価" in executed
    assert "自動計算" in executed
    # 約定状態: "自動" is decided from the executed share count.
    assert "約定株数から判定" in _field_text(block, "status")
    memo = _field_text(block, "memo")
    assert memo.startswith("メモ")
    assert len(memo) - len("メモ") >= 4


# ------------------------------------------------- account type in tables


def test_history_tables_show_account_type(client, db):
    _seed_stock(db)
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "avg_price": 900,
            "shares": 10,
            "status": "約定済",
            "account_type": "NISA",
        },
    )
    assert r.status_code == 201
    seed_stock_holding(db, code="8888", name="売却用銘柄")
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "8888",
            "sell_price": 1200,
            "sell_shares": 10,
            "executed_shares": 10,
            "status": "約定済",
            "account_type": "特定",
        },
    )
    assert r.status_code == 201
    page = client.get("/history").text
    heads = re.findall(r"<thead>(.*?)</thead>", page, re.S)
    assert len(heads) == 2
    for head in heads:
        assert "口座区分" in head
    bodies = re.findall(r"<tbody>(.*?)</tbody>", page, re.S)
    assert len(bodies) == 2
    assert "NISA" in bodies[0]
    assert "特定" in bodies[1]


def test_history_page_renders_rows_without_account_type(client, db):
    # Regression guard: rows registered without an account type (all
    # pre-existing data) still render, with an empty cell.
    _seed_stock(db)
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "avg_price": 900,
            "shares": 10,
            "status": "注文中",
        },
    )
    assert r.status_code == 201
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "sell_price": 1200,
            "sell_shares": 10,
            "executed_shares": 0,
            "status": "注文中",
        },
    )
    assert r.status_code == 201
    page = client.get("/history").text
    assert page.count("9999") >= 2


# ------------------------------------------------------------------ memos

_LONG_BUY_NOTE = "これは購入判断のメモです。決算短信を確認し、長期で保有する方針です。"
_LONG_SELL_MEMO = "これは売却判断のメモです。目標の価格に到達したので利益を確定します。"


def _seed_memo_rows(client, db, buy_note, sell_memo):
    _seed_stock(db)
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "avg_price": 900,
            "shares": 10,
            "status": "注文中",
            "note": buy_note,
        },
    )
    assert r.status_code == 201
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "sell_price": 1200,
            "sell_shares": 10,
            "executed_shares": 0,
            "status": "注文中",
            "memo": sell_memo,
        },
    )
    assert r.status_code == 201


def test_history_long_memos_use_hover_tip(client, db):
    _seed_memo_rows(client, db, _LONG_BUY_NOTE, _LONG_SELL_MEMO)
    page = client.get("/history").text
    assert "memo-tip" in page
    for memo in (_LONG_BUY_NOTE, _LONG_SELL_MEMO):
        assert len(memo) > 20
        # The cell shows only the first 20 chars; the full text
        # rides in the hover tooltip attribute (same as candidates).
        assert f'data-tip="{memo}"' in page
        assert memo[:20] + "…" in page
    # The full text is not rendered anywhere outside the tooltip
    # attributes: no more wrapping full-text cells.
    stripped = re.sub(r'data-tip="[^"]*"', "", page)
    assert _LONG_BUY_NOTE not in stripped
    assert _LONG_SELL_MEMO not in stripped


def test_history_short_memos_shown_as_is(client, db):
    # Regression guard: short memos render verbatim, no tooltip.
    _seed_memo_rows(client, db, "短いメモ", "短い売却メモ")
    page = client.get("/history").text
    assert "短いメモ" in page
    assert "短い売却メモ" in page
    assert 'data-tip="短いメモ"' not in page
    assert 'data-tip="短い売却メモ"' not in page
