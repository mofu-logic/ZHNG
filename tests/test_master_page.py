"""The dividend/benefit master page: read-only table, modal editor,
sortable columns, truncated long texts with hover tooltips."""
from conftest import seed_stock_holding

LONG_DESC = "自社製品の詰め合わせセット（年2回、3,000円相当）"
LONG_NOTE = "優待の利回りが改善しているため継続保有の判断としたメモです。"


def test_master_page_display(client, db):
    seed_stock_holding(db)
    db.execute(
        "UPDATE dividend_benefits SET benefit_desc=%s, judgment_note=%s,"
        " funding_target=true WHERE code='9999'",
        (LONG_DESC, LONG_NOTE),
    )
    db.commit()
    page = client.get("/master").text
    # Read-only table: no inline form anymore, an edit button instead.
    assert "<form data-code" not in page
    assert 'data-edit-benefit="9999"' in page
    # Sortable columns.
    assert 'class="sortable num" data-type="num">優待体感額' in page
    assert 'id="master"' in page
    # Long texts are truncated with the full text in the hover tip.
    assert LONG_DESC[:20] + "…" in page
    assert 'data-tip="' + LONG_DESC + '"' in page
    assert LONG_NOTE[:20] + "…" in page
    assert 'data-tip="' + LONG_NOTE + '"' in page
    # Funding target renders as text now.
    assert "対象" in page


def test_sidebar_layout(client, db):
    page = client.get("/master").text
    assert 'id="sidebar"' in page
    assert 'id="sidebar-toggle"' in page
    assert 'class="side-bottom"' in page
    # Main items on top in order, utility items in the bottom group.
    top = page.index("サマリー")
    for label in ("保有株一覧", "売買履歴", "配当・優待マスタ", "購入候補"):
        nxt = page.index(label)
        assert top < nxt
        top = nxt
    bottom = page.index('class="side-bottom"')
    assert bottom > top
    for label in ("設定", "データ更新", "ログ"):
        assert page.index(label) > bottom
