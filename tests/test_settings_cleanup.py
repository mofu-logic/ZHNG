"""settings 整理（refactor/settings-cleanup）の仕様テスト。

このPRの内容は2つ。

1. 設定キーの改名: ``target_yield_value`` → ``target_yield_portfolio``
   （「配当利回り（評価額ベース）の目標値」の意味）。コード側の参照
   （settings.py／main.py／templates/settings.html、および派生名の
   ``target_yield_value_pct`` のような API・フォーム項目名を含む
   全箇所）が新キーに揃い、旧キーを持つ既存DBは初期化
   （initialize_database）で値が新キーへ引き継がれ、旧キーの行が
   消える。画面・集計の振る舞い（目標値の表示・保存）は不変。
2. 移行用フラグと移行コードの削除: ``master_created_on_backfilled``
   （配当・優待マスタの created_on 一括設定）と
   ``policy_master_migrated``（方針のマスタへのコピー）は一度きりの
   移行で、本番では実行済み。初期化しても二度と走らず、フラグの
   settings 行自体も初期化後に残らない。移行済みの正常データには
   影響しない。

Red / Green の区別（実装前の現行コードに対する結果）:

- Red（このPRの実装で初めて満たされる）:
  - 旧キーの値が新キーへ引き継がれ、旧キーが消えること（冪等な
    ことも含む）
  - 両キーがある場合は新しい方の値を保ち、旧キーだけが消えること
  - 設定 API（読み書き）・設定画面・保有株一覧の KPI 目標が
    新キー／新項目名で従来どおり動くこと
  - created_on のバックフィルと方針コピーが走らないこと
  - フラグの settings 行が初期化で消えること
  - web/app のソースに旧キー名・フラグ名が残っていないこと
- Green（回帰防止。現行コードでも満たされ、変更後も保たれる）:
  - 新キーだけを持つDBを初期化しても値が変わらないこと
  - 移行済みデータ（移行で入った created_on・方針）が初期化で
    変わらないこと
"""
import re
from pathlib import Path

import pytest

from conftest import seed_stock_holding

from app.db import initialize_database

APP_DIR = Path(__file__).resolve().parent.parent / "web" / "app"

OLD_KEY = "target_yield_value"
NEW_KEY = "target_yield_portfolio"
FLAG_KEYS = ("master_created_on_backfilled", "policy_master_migrated")
LEGACY_NAMES = (OLD_KEY,) + FLAG_KEYS


# ---------------------------------------------------------------- helpers


def _put_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (%s, %s)"
        " ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )
    conn.commit()


def _settings(conn):
    return {
        r["key"]: r["value"]
        for r in conn.execute("SELECT key, value FROM settings").fetchall()
    }


def _master_row(conn, code):
    return conn.execute(
        "SELECT created_on, policy FROM dividend_benefits WHERE code = %s",
        (code,),
    ).fetchone()


# ------------------------------------------------------- rename: DB migration


def test_old_key_value_carried_over_to_new_key(db):
    # 本番DBの形: 旧キーに値（現在値は 0.04）がある状態から初期化する。
    # 既定値と区別できるよう、あえて 0.055 を入れて引き継ぎを確認する。
    _put_setting(db, OLD_KEY, "0.055")
    initialize_database(db)
    rows = _settings(db)
    assert rows.get(NEW_KEY) == "0.055"
    assert OLD_KEY not in rows


def test_rename_migration_is_idempotent(db):
    _put_setting(db, OLD_KEY, "0.055")
    initialize_database(db)
    initialize_database(db)
    rows = _settings(db)
    assert rows.get(NEW_KEY) == "0.055"
    assert OLD_KEY not in rows


def test_new_key_only_db_is_unchanged_by_initialize(db):
    # Green（回帰防止）: 新キーだけのDBでは初期化で何も変わらない。
    _put_setting(db, NEW_KEY, "0.06")
    initialize_database(db)
    rows = _settings(db)
    assert rows.get(NEW_KEY) == "0.06"
    assert OLD_KEY not in rows


def test_both_keys_present_keeps_new_value_and_drops_old(db):
    # 両キーがある場合は、新キー側の値（新しいコードが書いた値）を
    # 正とし、旧キーの行だけが消える。
    _put_setting(db, OLD_KEY, "0.055")
    _put_setting(db, NEW_KEY, "0.06")
    initialize_database(db)
    rows = _settings(db)
    assert rows.get(NEW_KEY) == "0.06"
    assert OLD_KEY not in rows


# ------------------------------------------------------- rename: API / screens


def test_settings_api_roundtrip_uses_new_key(client, db):
    r = client.put(
        "/api/v1/settings",
        json={
            "tax_rate_pct": 20.315,
            "target_dividend_total": 1200000,
            "target_yield_buy_pct": 3.0,
            "target_yield_now_pct": 3.5,
            "target_yield_portfolio_pct": 5.5,
        },
    )
    assert r.status_code == 200
    assert r.json()["target_yield_portfolio_pct"] == pytest.approx(5.5)
    rows = _settings(db)
    assert float(rows[NEW_KEY]) == pytest.approx(0.055)
    assert OLD_KEY not in rows
    got = client.get("/api/v1/settings").json()
    assert got["target_yield_portfolio_pct"] == pytest.approx(5.5)


def test_settings_api_reads_new_key(client, db):
    _put_setting(db, NEW_KEY, "0.055")
    got = client.get("/api/v1/settings").json()
    assert got["target_yield_portfolio_pct"] == pytest.approx(5.5)


def test_settings_api_defaults_portfolio_target(client, db):
    # キーが無いDBでは従来どおり既定値 0.04（= 4.0%）で補完される。
    got = client.get("/api/v1/settings").json()
    assert got["target_yield_portfolio_pct"] == pytest.approx(4.0)


def test_settings_page_renders_new_key_field(client, db):
    _put_setting(db, NEW_KEY, "0.055")
    r = client.get("/settings")
    assert r.status_code == 200
    m = re.search(
        r'name="target_yield_portfolio_pct"[^>]*value="([^"]*)"', r.text
    )
    assert m, "設定画面に target_yield_portfolio_pct の入力欄がありません"
    assert float(m.group(1)) == pytest.approx(5.5)


def test_dashboard_kpi_target_uses_new_key(client, db):
    seed_stock_holding(db)
    _put_setting(db, "tax_rate", "0.20315")
    _put_setting(db, "target_dividend_total", "1200000")
    _put_setting(db, "target_monthly_income", "79685")
    _put_setting(db, "target_yield_buy", "0.03")
    _put_setting(db, "target_yield_now", "0.035")
    _put_setting(db, NEW_KEY, "0.055")
    r = client.get("/")
    assert r.status_code == 200
    # 「配当利回り（評価額ベース）」カードの目標表示が新キーの値で出る。
    assert "目標 5.50%" in r.text


# ------------------------------------------------------- migration flags gone


def test_created_on_backfill_no_longer_runs(db):
    # フラグが無い状態でも、created_on が NULL のマスタ行が一括で
    # 埋められることは二度とない。フラグ行も作られない。
    db.execute(
        "INSERT INTO dividend_benefits(code, created_on)"
        " VALUES ('9999', NULL)"
    )
    db.commit()
    initialize_database(db)
    row = _master_row(db, "9999")
    assert row["created_on"] is None
    rows = _settings(db)
    for flag in FLAG_KEYS:
        assert flag not in rows


def test_policy_copy_no_longer_runs(db):
    # フラグが無い状態でも、買い履歴の方針がマスタへコピーされない。
    db.execute(
        "INSERT INTO dividend_benefits(code, policy) VALUES ('9999', NULL)"
    )
    db.execute(
        "INSERT INTO buy_lots(code, policy, purchase_date)"
        " VALUES ('9999', '配当', DATE '2026-11-20')"
    )
    db.commit()
    initialize_database(db)
    row = _master_row(db, "9999")
    assert row["policy"] is None


def test_migration_flag_rows_are_removed_by_initialize(db):
    # 本番DBに残っているフラグ行は、初期化で削除される。
    for flag in FLAG_KEYS:
        _put_setting(db, flag, "1")
    initialize_database(db)
    rows = _settings(db)
    for flag in FLAG_KEYS:
        assert flag not in rows


def test_already_migrated_data_is_unchanged_by_initialize(db):
    # Green（回帰防止）: 移行済みの正常データは初期化で変わらない。
    for flag in FLAG_KEYS:
        _put_setting(db, flag, "1")
    db.execute(
        "INSERT INTO dividend_benefits(code, created_on, policy)"
        " VALUES ('9998', DATE '2026-11-25', '配当')"
    )
    db.execute(
        "INSERT INTO dividend_benefits(code, created_on, policy)"
        " VALUES ('9999', DATE '2026-12-01', '優待')"
    )
    # マスタ側の方針が買い履歴より新しくても上書きされないこと。
    db.execute(
        "INSERT INTO buy_lots(code, policy, purchase_date)"
        " VALUES ('9998', '値上がり', DATE '2026-11-26')"
    )
    db.commit()
    initialize_database(db)
    row = _master_row(db, "9998")
    assert str(row["created_on"]) == "2026-11-25"
    assert row["policy"] == "配当"
    row = _master_row(db, "9999")
    assert str(row["created_on"]) == "2026-12-01"
    assert row["policy"] == "優待"


# ------------------------------------------------------- source hygiene


def test_app_source_has_no_legacy_setting_names():
    suffixes = {".py", ".html", ".sql", ".css", ".js"}
    offenders = []
    for path in sorted(APP_DIR.rglob("*")):
        if "__pycache__" in path.parts or path.suffix not in suffixes:
            continue
        text = path.read_text(encoding="utf-8")
        for name in LEGACY_NAMES:
            if name in text:
                offenders.append(f"{path.relative_to(APP_DIR)}: {name}")
    assert not offenders, "旧キー名・フラグ名が残っています: " + ", ".join(
        offenders
    )
