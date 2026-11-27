"""Terminology unification: one name per concept on every screen.

Follows the 2026-11-28 terminology audit (A-1..A-14). The exact
table headers are pinned in test_common_sort_memo.py; this file pins
the screen-level wording that lives outside those header lists:

- A-1  benefit memos: 銘柄コード/銘柄名, not コード/会社名
- A-2  holdings avg_price: 平均取得単価 (the buy-lot price on
       /history stays 取得単価 -- a lot price, not an average)
- A-3  dashboard 購入日 -> 買付日 (same buy_lots.purchase_date)
- A-4  per-share dividend: 1株配当 on master / candidates / summary
- A-5  yield headers: full-width parentheses, 現在値, 優待込み
- A-6  settings target label: 年間配当予想, not 年間配当合計
- A-7  dialog titles: 保有株の追加 / 保有株のインポート
- audit addendum: the /logs retention note follows the
  log_retention_days setting instead of a hard-coded 90
- A-8  save button 保存 (not 保存する); save failures read
  保存できませんでした, and the shared apiJson alert no longer
  says 保存に失敗しました
- A-9  dialog confirm buttons use the verb form (削除する)
- A-10 OpenAPI description: 配当・優待マスタ (with the ・)
- A-11 404 wording: 銘柄が見つかりませんでした
- A-12 benefit-memo arrival labels: 到着日, not 到着時期
- A-13 candidates: 取得日, not データ日
- A-14 placeholders use 例： (full-width colon); the yield filter
  percent labels use the bracketed （%） form like the settings page
"""
from conftest import seed_settings, seed_stock_holding
from test_api import KPI_PAYLOAD

ALL_PAGES = [
    "/",
    "/history",
    "/settings",
    "/master",
    "/candidates",
    "/benefit-memos",
    "/summary",
    "/data",
    "/logs",
]


def _seed_summary_dividend_change(db):
    seed_stock_holding(db)
    run_id = db.execute(
        "INSERT INTO fetch_runs(started_at, finished_at, ok_count,"
        " ng_count, note, total_count, done_count) VALUES (now(),"
        " now(), 1, 0, 'batch', 1, 1) RETURNING id"
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO dividend_changes(run_id, code, old_div, new_div)"
        " VALUES (%s, '9999', 40, 50)",
        (run_id,),
    )
    db.commit()


# --- A-1: benefit memos use the standard stock terms -----------------

def test_benefit_memos_page_has_no_company_name(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/benefit-memos").text
    assert "会社名" not in page


# --- A-2: avg_price is 平均取得単価 outside the buy-lot form ---------

def test_dashboard_kpi_tooltip_uses_average_price(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/").text
    assert "1株配当 ÷ 平均取得単価" in page
    assert "1株配当 ÷ 取得単価" not in page


# --- A-3: the dashboard date column is 買付日 -------------------------

def test_dashboard_has_no_purchase_date_label(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/").text
    assert "購入日" not in page
    assert "買付日" in page


# --- A-4: per-share dividend is 1株配当 everywhere --------------------

def test_summary_dividend_change_header(client, db):
    seed_settings(db)
    _seed_summary_dividend_change(db)
    page = client.get("/summary").text
    assert '<th class="num">1株配当</th>' in page
    assert "配当予想（1株・円）" not in page


def test_master_page_has_no_yearly_per_share_label(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/master").text
    assert "配当（年・1株）" not in page


# --- A-5: yield headers match the filter wording ---------------------

def test_dashboard_yield_headers_unified(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/").text
    assert "利回り（買値）" in page
    assert "利回り（現在値）" in page
    assert "優待込み（現在値）" in page
    assert "利回り(買値)" not in page
    assert "利回り(現在)" not in page
    assert "優待込(現在)" not in page


# --- A-6: the settings target is 年間配当予想 --------------------------

def test_settings_dividend_target_label(client, db):
    seed_settings(db)
    page = client.get("/settings").text
    assert "目標：年間配当予想（円）" in page
    assert "年間配当予想 ×（1 − 税率）÷ 12 で自動算出" in page
    assert "年間配当合計" not in page


# --- A-7: dialog titles name the target like the buttons do ---------

def test_dashboard_dialog_titles(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/").text
    assert 'id="holding-add-title">保有株の追加</div>' in page
    assert 'id="holding-import-title">保有株のインポート</div>' in page


# --- addendum: the /logs retention note follows the setting ---------

def test_logs_page_shows_default_retention(client, db):
    seed_settings(db)
    page = client.get("/logs").text
    assert "保持期間は90日です。" in page


def test_logs_page_follows_log_retention_days(client, db):
    seed_settings(db)
    r = client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "log_retention_days": 45},
    )
    assert r.status_code == 200
    page = client.get("/logs").text
    assert "保持期間は45日です。" in page
    assert "保持期間は90日です。" not in page


# --- A-8: save button and save-failure wording -----------------------

def test_settings_save_button_label(client, db):
    seed_settings(db)
    page = client.get("/settings").text
    assert '<button type="submit">保存</button>' in page
    assert '<button type="submit">保存する</button>' not in page


def test_no_page_says_save_failed_old_form(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    for path in ALL_PAGES:
        page = client.get(path).text
        assert "保存に失敗しました" not in page, path


def test_api_json_save_failure_wording(client, db):
    # The settings and history saves go through the shared apiJson
    # helper in base.html; their failure wording is the unified
    # 保存できませんでした form used by the edit dialogs.
    seed_settings(db)
    seed_stock_holding(db)
    for path in ("/settings", "/history"):
        page = client.get(path).text
        assert "保存できませんでした" in page, path


# --- A-9: dialog confirm buttons use the verb form -------------------

def test_dashboard_delete_dialog_button_is_verb_form(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/").text
    assert "okText: '削除する'" in page
    assert "okText: '削除'," not in page


# --- A-10: OpenAPI description spells the master name in full --------

def test_openapi_description_uses_master_name(client, db):
    doc = client.get("/openapi.json").json()
    desc = doc["info"]["description"]
    assert "配当・優待マスタ" in desc
    assert "配当優待マスタ" not in desc


# --- A-11: not-found wording is the past form -------------------------

def test_benefit_memo_put_unknown_code_message(client, db):
    r = client.put(
        "/api/v1/benefit-memos/0000", json={"status": "対象なし"}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "銘柄が見つかりませんでした"


# --- A-12: benefit-memo arrival labels are dates ----------------------

def test_benefit_memos_arrival_labels_are_dates(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/benefit-memos").text
    assert "到着時期" not in page
    assert "申込書到着日" in page
    assert "優待品到着日" in page


# --- A-13: candidates date column is 取得日 ----------------------------

def test_candidates_page_has_no_data_date_label(client, db):
    seed_settings(db)
    page = client.get("/candidates").text
    assert "データ日" not in page


# --- A-14: example placeholders and percent labels --------------------

def test_example_placeholders_use_fullwidth_colon(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    dash = client.get("/").text
    assert 'placeholder="例：4"' in dash
    assert 'placeholder="例：2"' in dash
    assert 'placeholder="例 4"' not in dash
    assert 'placeholder="例 2"' not in dash
    cand = client.get("/candidates").text
    assert 'placeholder="例：9984"' in cand
    assert "'例：1500'" in cand
    assert "例: " not in cand


def test_dashboard_yield_filter_percent_labels(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    page = client.get("/").text
    assert "下限（%）" in page
    assert "上限（%）" in page
    assert "下限 %" not in page
    assert "上限 %" not in page
