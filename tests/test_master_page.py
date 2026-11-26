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
    assert "売却候補" in page
    assert "候補" in page


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


def test_master_policy_roundtrip_and_dashboard_precedence(client, db):
    seed_stock_holding(db)
    # The lot says 値上がり; the master does not set a policy yet, so
    # the dashboard falls back to the lot value.
    db.execute(
        "INSERT INTO buy_lots(code, name, avg_price, shares,"
        " purchase_date, policy, status)"
        " VALUES ('9999', 'テスト銘柄', 800, 100, '2026-01-01',"
        " '値上がり', '約定済')"
    )
    db.commit()
    assert 'data-policy="値上がり"' in client.get("/").text
    # Setting the policy on the master makes it authoritative.
    r = client.put(
        "/api/v1/dividend-benefits/9999",
        json={"benefit_value": 5000, "policy": "配当・優待"},
    )
    assert r.status_code == 200
    assert r.json()["policy"] == "配当・優待"
    assert db.execute(
        "SELECT policy FROM dividend_benefits WHERE code='9999'"
    ).fetchone()["policy"] == "配当・優待"
    page = client.get("/").text
    assert 'data-policy="配当・優待"' in page
    # The master page shows the column and the checkbox editor parts.
    master = client.get("/master").text
    assert ">方針</th>" in master
    assert "POLICY_PARTS" in master
    assert "配当・優待" in master


def test_master_created_on_and_help_tip(client, db):
    seed_stock_holding(db)
    page = client.get("/master").text
    # The code cell shows the registration date, not the update date.
    assert "登録" in page
    assert "tip-right" in page
    assert "優待と判断メモは自身で更新が必要です" in page
    body = client.get("/api/v1/dividend-benefits/9999").json()
    assert body["created_on"] is not None
