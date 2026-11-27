"""スキーマの .sql ファイル分離（refactor/schema-sql-file）。

このPRは振る舞いを変えないリファクタで、db.py に文字列として
埋め込まれているテーブル定義（SCHEMA）を web/app/schema.sql へ
分離し、db.py は起動時にそのファイルを読み込んで実行する形に
する。口座区分の許可リストの値リストは、従来どおり
app.config.ACCOUNT_TYPES が正の定義で、.sql 側には値を複写せず
置換用の目印だけを置き、読み込み時に ACCOUNT_TYPES から生成した
リテラル列へ置換する。

Red / Green の区別（実装前の現行コードに対する結果）:

- Red（このPRの実装で初めて満たされる）:
  - web/app/schema.sql が存在すること
  - .sql 側に許可値のリテラルが複写されていないこと
  - db.py のソースに CREATE TABLE の大きなリテラルが残って
    いないこと、および db.py が schema.sql を参照していること
- Green（回帰防止。現行の埋め込み文字列でも満たされる性質で、
  分離後も保たれることを固定する）:
  - db.py の読み込み経路で得られるスキーマ文字列に全14テーブル
    分の CREATE TABLE が含まれ、目印が置換済みであること
  - そのスキーマを空のDBへ適用すると全14テーブル・主要な制約・
    NOT NULL が作られること
  - チェック制約の許可値が ACCOUNT_TYPES と一致すること
  - スキーマを2回適用してもエラーにならないこと（冪等）
"""
import re
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from conftest import DB_AVAILABLE, TABLES, TEST_DB_URL

from app import db as app_db
from app.config import ACCOUNT_TYPES

APP_DIR = Path(__file__).resolve().parent.parent / "web" / "app"
SCHEMA_FILE = APP_DIR / "schema.sql"
DB_PY = APP_DIR / "db.py"

ALLOWLIST_CONSTRAINTS = [
    "holdings_account_type_allowlist",
    "holding_events_account_type_allowlist",
    "buy_lots_account_type_allowlist",
    "sales_account_type_allowlist",
]


# ---------------------------------------------------------------- helpers


def _schema_text():
    """db.py の読み込み経路で得られるスキーマ文字列を返す。

    現行はモジュール定数 SCHEMA（埋め込み文字列）。分離後は
    ファイル読み込み由来の値になる想定で、定数が無くなった場合は
    名前に schema を含むモジュール内ヘルパーの戻り値を使う。
    """
    schema = getattr(app_db, "SCHEMA", None)
    if isinstance(schema, str):
        return schema
    for name in dir(app_db):
        if "schema" not in name.lower():
            continue
        fn = getattr(app_db, name)
        if not callable(fn):
            continue
        try:
            value = fn()
        except TypeError:
            continue
        if isinstance(value, str):
            return value
    raise AssertionError("db.py からスキーマ文字列を取得できませんでした")


def _conninfo(dbname):
    parts = conninfo_to_dict(TEST_DB_URL)
    parts["dbname"] = dbname
    return make_conninfo(**parts)


@pytest.fixture()
def scratch_db():
    """まっさらな使い捨てデータベース（テストごとに作り直す）。

    スキーマ未適用の空のDBが要るため、共有のテストDBではなく
    専用のデータベースを作って使い、終わったら落とす。
    """
    if not DB_AVAILABLE:
        pytest.skip("test database not available (set TEST_DATABASE_URL)")
    base = conninfo_to_dict(TEST_DB_URL).get("dbname") or "stock_test"
    scratch = f"{base}_scratch"
    with psycopg.connect(_conninfo("postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        admin.execute(f'CREATE DATABASE "{scratch}"')
    conn = psycopg.connect(_conninfo(scratch))
    yield conn
    conn.close()
    with psycopg.connect(_conninfo("postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')


def _apply_schema(conn):
    conn.execute(_schema_text())
    conn.commit()


# ---------------------------------------------------------------- file shape


def test_schema_file_exists_and_covers_all_tables():
    assert SCHEMA_FILE.is_file(), "web/app/schema.sql が存在しません"
    text = _schema_text()
    for table in TABLES:
        assert re.search(
            rf"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+{re.escape(table)}\b",
            text,
        ), f"スキーマに {table} の CREATE TABLE がありません"
    assert text.count("CREATE TABLE") >= len(TABLES)


def test_schema_text_has_placeholder_fully_replaced():
    # 置換の実効内容で検証する: ACCOUNT_TYPES から生成される
    # リテラル列がスキーマ文字列に現れていること。
    text = _schema_text()
    literal_list = ", ".join(f"'{t}'" for t in ACCOUNT_TYPES)
    assert literal_list in text
    # 未置換の目印が残っていないこと（目印の具体名には依存せず、
    # 典型的なプレースホルダの形が残っていないことを見る）。
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", text)
    assert "{{" not in text and "}}" not in text


def test_schema_file_does_not_duplicate_allowlist_values():
    # 正の定義は ACCOUNT_TYPES のまま。.sql 側へ許可値のリテラルを
    # 複写していないこと（目印だけが置かれていること）を確認する。
    assert SCHEMA_FILE.is_file(), "web/app/schema.sql が存在しません"
    raw = SCHEMA_FILE.read_text(encoding="utf-8")
    for account_type in ACCOUNT_TYPES:
        assert f"'{account_type}'" not in raw


def test_db_module_has_no_embedded_schema_literal():
    source = DB_PY.read_text(encoding="utf-8")
    assert "CREATE TABLE" not in source
    assert "schema.sql" in source


# ---------------------------------------------------------------- structure


def test_applying_schema_to_empty_database_builds_full_structure(scratch_db):
    _apply_schema(scratch_db)

    tables = {
        row[0]
        for row in scratch_db.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public'"
        ).fetchall()
    }
    assert tables == set(TABLES)

    constraints = {
        row[0]
        for row in scratch_db.execute(
            "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
            (ALLOWLIST_CONSTRAINTS,),
        ).fetchall()
    }
    assert constraints == set(ALLOWLIST_CONSTRAINTS)

    for table in ("holdings", "holding_events"):
        row = scratch_db.execute(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = %s"
            " AND column_name = 'account_type'",
            (table,),
        ).fetchone()
        assert row is not None and row[0] == "NO", (
            f"{table}.account_type が NOT NULL になっていません"
        )

    row = scratch_db.execute(
        "SELECT indexdef FROM pg_indexes"
        " WHERE indexname = 'holdings_code_account_uidx'"
    ).fetchone()
    assert row is not None, "holdings_code_account_uidx がありません"
    indexdef = row[0].upper()
    assert "UNIQUE" in indexdef
    assert "WHERE" not in indexdef, "部分インデックスのままです"


def test_allowlist_constraint_values_match_account_types(scratch_db):
    _apply_schema(scratch_db)
    for name in ALLOWLIST_CONSTRAINTS:
        row = scratch_db.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = %s",
            (name,),
        ).fetchone()
        assert row is not None, f"制約 {name} がありません"
        values = set(re.findall(r"'([^']+)'", row[0]))
        assert values == set(ACCOUNT_TYPES), (
            f"{name} の許可値が ACCOUNT_TYPES と一致しません: {row[0]}"
        )


def test_schema_application_is_idempotent(scratch_db):
    _apply_schema(scratch_db)
    _apply_schema(scratch_db)  # 2回目もエラーにならないこと
    count = scratch_db.execute(
        "SELECT count(*) FROM information_schema.tables"
        " WHERE table_schema = 'public'"
    ).fetchone()[0]
    assert count == len(TABLES)
