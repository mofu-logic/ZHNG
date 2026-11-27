"""未使用カラム削除（refactor/drop-unused-columns）の仕様テスト。

カラム棚卸し（コードでの読み書きと本番データの実態を突き合わせた
調査）で削除が決まった全14列について、スキーマ・移行・API・
インポート・ソースの各面から「もう存在しない」ことを固定する。

削除対象（14列）:
- holdings: custody_type（保管区分）、lent_shares（貸株数）、
  ordered_shares（発注数）、updated_at
- holding_events: custody_type_before、lent_shares_before、
  ordered_shares_before
- sales: purchase_date、shares
- daily_snapshots: source、captured_at
- price_points: prev_close、source
- dividend_benefits: info_updated_on

残すもの（削除対象と取り違えやすいもの）:
- buy_lots の purchase_date / shares（買い履歴では現役）
- daily_snapshots の prev_close と日次データ列一式（将来のグラフ用）
- price_points の captured_at / volume / day_high / day_low
  （出来高と日中の高値・安値は将来のAI解析の材料として保持）

株価取得の保存処理（price_points / daily_snapshots への INSERT）が
対象カラムなしで動くことは、既存の tests/test_fetch.py（フェイク
HTTPでの通しテスト）が新しいスキーマに対して通ることで担保される
ため、このファイルでは専用テストを持たない。新しいスキーマでは
対象カラム自体が存在しないので、INSERT が旧カラムを参照したまま
なら test_fetch 側が失敗する。

Red / Green の区別（実装前の現行コードに対する結果）:

- Red（このPRの実装で初めて満たされる）:
  - 新規DB（initialize_database で作った素のスキーマ）に対象14列が
    存在しないこと
  - 旧スキーマ相当（対象列あり＋データあり）のDBを初期化すると、
    対象列だけが落ち、行数と残りのカラムの値が変わらないこと。
    再初期化してもエラーにならないこと（冪等）
  - 保有・売り・配当マスタのAPI応答に対象フィールドが含まれない
    こと
  - CSVインポートの対応付け候補（services.IMPORT_FIELDS）と
    保有株一覧のインポートフォームから対象3項目が消えていること
  - web/app のソース（移行コードの置き場所である db.py と
    schema.sql を除く）に対象カラム名が残っていないこと
- Green（回帰防止。現行コードでも満たされ、変更後も保たれる）:
  - 保持対象のカラム（buy_lots.purchase_date、daily_snapshots.
    prev_close、price_points.captured_at / volume 等）が新規
    スキーマに存在すること
  - 買いAPIの応答に purchase_date / shares が従来どおり含まれる
    こと（sales 側だけが削除対象）
  - 保有イベントAPIの応答に対象の *_before 3件が含まれないこと
    （現行の HoldingEventOut にもともと含まれていない）
  - 対応付けなし項目を除いたCSVインポートが従来どおり動くこと
"""
import re
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from conftest import TEST_DB_URL, seed_settings

from app.db import initialize_database
from app.services import IMPORT_FIELDS

APP_DIR = Path(__file__).resolve().parent.parent / "web" / "app"

# 削除対象: テーブル名 -> カラム名の列
DROPPED = {
    "holdings": ("custody_type", "lent_shares", "ordered_shares",
                 "updated_at"),
    "holding_events": ("custody_type_before", "lent_shares_before",
                       "ordered_shares_before"),
    "sales": ("purchase_date", "shares"),
    "daily_snapshots": ("source", "captured_at"),
    "price_points": ("prev_close", "source"),
    "dividend_benefits": ("info_updated_on",),
}

# 旧スキーマ相当を再現するための、対象カラムの旧定義（型つき）
_OLD_COLUMN_DEFS = {
    "holdings": (
        ("custody_type", "text"),
        ("lent_shares", "integer DEFAULT 0"),
        ("ordered_shares", "integer DEFAULT 0"),
        ("updated_at", "timestamptz DEFAULT now()"),
    ),
    "holding_events": (
        ("custody_type_before", "text"),
        ("lent_shares_before", "integer"),
        ("ordered_shares_before", "integer"),
    ),
    "sales": (
        ("purchase_date", "date"),
        ("shares", "integer"),
    ),
    "daily_snapshots": (
        ("source", "text"),
        ("captured_at", "timestamptz"),
    ),
    "price_points": (
        ("prev_close", "numeric"),
        ("source", "text"),
    ),
    "dividend_benefits": (
        ("info_updated_on", "date"),
    ),
}

# 保持対象: テーブル名 -> 残るはずのカラム名の列
KEPT = {
    "buy_lots": ("purchase_date", "shares"),
    "sales": ("buy_price", "sell_price", "sell_shares",
              "executed_shares"),
    "price_points": ("captured_at", "volume", "day_high", "day_low"),
    "daily_snapshots": ("close_price", "prev_close", "open_price",
                        "day_high", "day_low", "volume",
                        "market_cap_m"),
    "holdings": ("avg_price", "shares"),
    "dividend_benefits": ("dividend_forecast", "created_on"),
}


# ---------------------------------------------------------------- helpers


def _columns(conn, table, schema="public"):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchall()
    return {r["column_name"] for r in rows}


def _recreate_old_columns(conn):
    """削除対象カラムを旧定義で足し戻し、旧スキーマ相当にする。"""
    for table, defs in _OLD_COLUMN_DEFS.items():
        for name, type_def in defs:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS"
                f" {name} {type_def}"
            )
    conn.commit()


def _seed_old_rows(conn):
    """対象カラムに値を持つ行と、保持対象カラムの値を各テーブルへ。"""
    conn.execute(
        "INSERT INTO stocks(code, name, market)"
        " VALUES ('9999', 'テスト銘柄', '東証')"
    )
    conn.execute(
        "INSERT INTO holdings(code, account_type, custody_type,"
        " avg_price, shares, lent_shares, ordered_shares, updated_at)"
        " VALUES ('9999', '特定', '保護預り', 800, 100, 10, 5, now())"
    )
    conn.execute(
        "INSERT INTO holding_events(code, account_type, kind,"
        " source_type, source_id, delta_shares, shares_before,"
        " shares_after, avg_price_before, avg_price_after,"
        " lent_shares_before, ordered_shares_before,"
        " custody_type_before)"
        " VALUES ('9999', '特定', 'add', 'manual', NULL, 100, 0, 100,"
        " NULL, 800, 1, 2, '代用')"
    )
    conn.execute(
        "INSERT INTO sales(code, name, buy_price, shares,"
        " purchase_date, sell_price, sell_shares, executed_shares,"
        " profit, status)"
        " VALUES ('9999', 'テスト銘柄', 800, 100, '2026-01-05', 1000,"
        " 100, 100, 20000, '約定済')"
    )
    conn.execute(
        "INSERT INTO buy_lots(code, name, avg_price, shares,"
        " purchase_date, status)"
        " VALUES ('9999', 'テスト銘柄', 800, 100, '2026-01-05', '約定済')"
    )
    conn.execute(
        "INSERT INTO price_points(code, captured_at, price,"
        " prev_close, day_high, day_low, volume, source)"
        " VALUES ('9999', now(), 1000, 990, 1010, 980, 12345, 'test')"
    )
    conn.execute(
        "INSERT INTO daily_snapshots(code, snap_date, close_price,"
        " prev_close, open_price, day_high, day_low, volume,"
        " captured_at, source)"
        " VALUES ('9999', '2026-11-27', 1000, 990, 995, 1010, 980,"
        " 12345, now(), 'test')"
    )
    conn.execute(
        "INSERT INTO dividend_benefits(code, dividend_forecast,"
        " info_updated_on) VALUES ('9999', 40, '2026-11-01')"
    )
    conn.commit()


def _count(conn, table):
    return conn.execute(
        f"SELECT count(*) AS c FROM {table}"
    ).fetchone()["c"]


def _seed_stock(conn, code="9999"):
    conn.execute(
        "INSERT INTO stocks(code, name, market)"
        " VALUES (%s, 'テスト銘柄', '東証')",
        (code,),
    )
    conn.commit()


# ------------------------------------------------------- schema: fresh DB


def test_fresh_schema_has_no_unused_columns():
    """新規DB（素のスキーマ）に対象14列がなく、保持列はあること。"""
    scratch = "fresh_schema_check"
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {scratch} CASCADE")
        conn.execute(f"CREATE SCHEMA {scratch}")
    try:
        with psycopg.connect(
            TEST_DB_URL, row_factory=dict_row
        ) as conn:
            conn.execute(f"SET search_path TO {scratch}")
            initialize_database(conn)
            for table, names in DROPPED.items():
                cols = _columns(conn, table, schema=scratch)
                for name in names:
                    assert name not in cols, (
                        f"新規スキーマに削除対象カラムが残っています:"
                        f" {table}.{name}"
                    )
            for table, names in KEPT.items():
                cols = _columns(conn, table, schema=scratch)
                for name in names:
                    assert name in cols, (
                        f"保持対象カラムが新規スキーマにありません:"
                        f" {table}.{name}"
                    )
    finally:
        with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {scratch} CASCADE")


# ------------------------------------------------- schema: migration of old DB


def test_initialize_drops_unused_columns_and_preserves_data(db):
    """旧スキーマ相当のDBを初期化すると対象列だけが落ちること。"""
    _recreate_old_columns(db)
    _seed_old_rows(db)
    tables = list(DROPPED) + ["buy_lots"]
    counts_before = {t: _count(db, t) for t in tables}

    initialize_database(db)

    for table, names in DROPPED.items():
        cols = _columns(db, table)
        for name in names:
            assert name not in cols, (
                f"移行後も削除対象カラムが残っています: {table}.{name}"
            )
    for table in tables:
        assert _count(db, table) == counts_before[table], (
            f"{table} の行数が移行で変わりました"
        )
    # 保持対象カラムの値は移行前と変わらない。
    holding = db.execute(
        "SELECT avg_price, shares FROM holdings WHERE code = '9999'"
    ).fetchone()
    assert float(holding["avg_price"]) == 800
    assert holding["shares"] == 100
    sale = db.execute(
        "SELECT buy_price, sell_price, profit, status FROM sales"
        " WHERE code = '9999'"
    ).fetchone()
    assert float(sale["buy_price"]) == 800
    assert float(sale["sell_price"]) == 1000
    assert float(sale["profit"]) == 20000
    assert sale["status"] == "約定済"
    buy = db.execute(
        "SELECT purchase_date FROM buy_lots WHERE code = '9999'"
    ).fetchone()
    assert buy["purchase_date"].isoformat() == "2026-01-05"
    point = db.execute(
        "SELECT price, volume, day_high, day_low FROM price_points"
        " WHERE code = '9999'"
    ).fetchone()
    assert float(point["price"]) == 1000
    assert point["volume"] == 12345
    snap = db.execute(
        "SELECT close_price, prev_close FROM daily_snapshots"
        " WHERE code = '9999'"
    ).fetchone()
    assert float(snap["close_price"]) == 1000
    assert float(snap["prev_close"]) == 990
    benefit = db.execute(
        "SELECT dividend_forecast FROM dividend_benefits"
        " WHERE code = '9999'"
    ).fetchone()
    assert float(benefit["dividend_forecast"]) == 40


def test_drop_migration_is_idempotent(db):
    """対象列を落とす移行は、繰り返し初期化しても安全なこと。"""
    _recreate_old_columns(db)
    _seed_old_rows(db)
    initialize_database(db)
    initialize_database(db)
    for table, names in DROPPED.items():
        cols = _columns(db, table)
        for name in names:
            assert name not in cols


# ------------------------------------------------------------------- API


def test_holdings_api_has_no_dropped_fields(client, db):
    _seed_stock(db)
    r = client.post(
        "/api/v1/holdings",
        json={"code": "9999", "account_type": "特定",
              "avg_price": 800, "shares": 100},
    )
    assert r.status_code == 201
    body = r.json()
    for field in ("custody_type", "lent_shares", "ordered_shares"):
        assert field not in body, f"保有API応答に {field} が残っています"
    listed = client.get("/api/v1/holdings").json()
    assert listed, "保有の一覧が空です（前提条件）"
    for field in ("custody_type", "lent_shares", "ordered_shares"):
        assert field not in listed[0]


def test_holding_events_api_has_no_before_fields(client, db):
    # Green（回帰防止）: イベント応答にはもともと *_before のうち
    # shares / avg_price しか含まれず、対象3件は含まれていない。
    _seed_stock(db)
    r = client.post(
        "/api/v1/holdings",
        json={"code": "9999", "account_type": "特定",
              "avg_price": 800, "shares": 100},
    )
    assert r.status_code == 201
    holding_id = r.json()["id"]
    events = client.get(f"/api/v1/holdings/{holding_id}/events").json()
    assert events, "イベントが空です（前提条件）"
    for event in events:
        for field in ("custody_type_before", "lent_shares_before",
                      "ordered_shares_before"):
            assert field not in event


def test_sales_api_has_no_purchase_date_or_shares(client, db):
    _seed_stock(db)
    r = client.post(
        "/api/v1/sales",
        json={"code": "9999", "name": "テスト銘柄", "buy_price": 800,
              "sell_price": 1000, "sell_shares": 100,
              "executed_shares": 100, "status": "約定済"},
    )
    assert r.status_code == 201
    body = r.json()
    assert "purchase_date" not in body
    assert "shares" not in body
    # 残る項目は従来どおり返る。
    assert body["sell_shares"] == 100
    listed = client.get("/api/v1/sales").json()
    assert listed, "売りの一覧が空です（前提条件）"
    assert "purchase_date" not in listed[0]
    assert "shares" not in listed[0]


def test_buys_api_keeps_purchase_date_and_shares(client, db):
    # Green（回帰防止）: 買い側の purchase_date / shares は現役で、
    # sales 側の削除と取り違えて消えていないこと。
    _seed_stock(db)
    r = client.post(
        "/api/v1/buys",
        json={"code": "9999", "name": "テスト銘柄", "avg_price": 800,
              "shares": 100, "purchase_date": "2026-11-20",
              "status": "約定済"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["shares"] == 100
    assert body["purchase_date"] == "2026-11-20"


def test_dividend_benefits_api_has_no_info_updated_on(client, db):
    _seed_stock(db)
    db.execute(
        "INSERT INTO dividend_benefits(code, dividend_forecast)"
        " VALUES ('9999', 40)"
    )
    db.commit()
    listed = client.get("/api/v1/dividend-benefits").json()
    assert listed, "配当マスタの一覧が空です（前提条件）"
    for item in listed:
        assert "info_updated_on" not in item
    one = client.get("/api/v1/dividend-benefits/9999")
    assert one.status_code == 200
    assert "info_updated_on" not in one.json()


# ----------------------------------------------------------------- import


def test_import_fields_exclude_dropped_items():
    for field in ("custody_type", "lent_shares", "ordered_shares"):
        assert field not in IMPORT_FIELDS, (
            f"インポートの対応付け候補に {field} が残っています"
        )
    # 残る項目は従来どおり候補に含まれる。
    for field in ("code", "account_type", "avg_price", "shares"):
        assert field in IMPORT_FIELDS


def test_dashboard_import_form_has_no_dropped_mapping_inputs(client, db):
    seed_settings(db)
    html = client.get("/").text
    for name in ("lent_shares_col", "ordered_shares_col",
                 "custody_type_col"):
        assert f'name="{name}"' not in html, (
            f"インポートフォームに {name} が残っています"
        )
    assert 'name="shares_col"' in html


def test_import_without_dropped_mappings_still_works(client, db):
    # Green（回帰防止）: 対象項目を含まない対応付けでのインポートは
    # 従来どおり動く。
    _seed_stock(db)
    csv = "コード,株数,平均取得単価\n9999,100,800\n".encode("utf-8")
    r = client.post(
        "/api/v1/holdings/import",
        files={"file": ("holdings.csv", csv, "text/csv")},
        data={"code_col": "コード", "shares_col": "株数",
              "avg_price_col": "平均取得単価"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1
    assert body["failed"] == 0


# ------------------------------------------------------------ source scan


# スキャン対象のカラム名。他テーブルと名前が重なるもの
# （purchase_date、shares、prev_close、source、captured_at、
# updated_at）は誤検出を避けるため含めず、スキーマとAPIのテストで
# 担保する。ここに挙げた7件は削除対象テーブルでしか使われない名前。
_SCAN_NAMES = (
    "custody_type", "lent_shares", "ordered_shares",
    "custody_type_before", "lent_shares_before",
    "ordered_shares_before", "info_updated_on",
)

# 移行コード（DROP COLUMN）の置き場所は db.py（initialize_database）
# と schema.sql（SCHEMA）の2ファイルに限るという構成上の境界。
# この2ファイルに対象名が残るのは移行の仕事として正当で、残りの
# 全ファイルに通常経路の残りがないことをここで固定する。
_SCAN_EXCLUDED_FILES = {"db.py", "schema.sql"}


def test_app_source_has_no_dropped_column_names():
    suffixes = {".py", ".html", ".sql", ".css", ".js"}
    patterns = {
        name: re.compile(r"\b" + re.escape(name) + r"\b")
        for name in _SCAN_NAMES
    }
    offenders = []
    for path in sorted(APP_DIR.rglob("*")):
        if "__pycache__" in path.parts or path.suffix not in suffixes:
            continue
        if path.name in _SCAN_EXCLUDED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in patterns.items():
            if pattern.search(text):
                offenders.append(f"{path.relative_to(APP_DIR)}: {name}")
    assert not offenders, (
        "削除対象カラム名がソースに残っています: "
        + ", ".join(offenders)
    )
