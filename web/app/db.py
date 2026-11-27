"""PostgreSQL への接続とスキーマ・起動時の移行処理。

スキーマ定義は schema.sql に置き、起動時に読み込んで適用する。
マイグレーションの内容・順序、起動時の処理は main.py にあった
ものをそのまま移したもので、振る舞いは変えない。
"""

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.config import ACCOUNT_TYPES, DATABASE_URL

_ACCOUNT_TYPE_LIST_SQL = ", ".join(f"'{t}'" for t in ACCOUNT_TYPES)

# schema.sql 内の目印。許可値の正の定義は ACCOUNT_TYPES のままで、
# .sql 側へ値を複写せず、読み込み時にこの目印を生成したリテラル列
# へ置換する。
_ACCOUNT_TYPE_LIST_TOKEN = "__ACCOUNT_TYPE_LIST__"


def _load_schema() -> str:
    """schema.sql を読み、口座区分の許可リストを差し込んで返す。"""
    text = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    return text.replace(_ACCOUNT_TYPE_LIST_TOKEN, _ACCOUNT_TYPE_LIST_SQL)


SCHEMA = _load_schema()


def db():
    return psycopg.connect(
        DATABASE_URL, row_factory=dict_row, options="-c timezone=Asia/Tokyo"
    )


def initialize_database(conn):
    """起動時の DB 初期化を行う。

    スキーマ適用、一度きりの移行（方針のマスタ移行と created_on の
    バックフィル）、中断したままの取得 run のクローズを、main.py の
    lifespan にあった順序・内容のまま行う。呼び出し側がコミットする
    部分（app_config / prune_app_logs）は lifespan に残す。

    戻り値は起動時にクローズした stale な fetch run の件数。
    """
    conn.execute(SCHEMA)
    # One-time migration: the per-stock policy used to be derived
    # from the buy lots on every read; it now lives on the dividend
    # master, where it is edited. Seed it once from the latest lot
    # (verified to match the derived value for every held code).
    # One-time backfill: every master row present at this point
    # came in with the initial spreadsheet import (2026-11-25);
    # rows created later get created_on from the column default.
    cflag = conn.execute(
        "SELECT value FROM settings"
        " WHERE key = 'master_created_on_backfilled'"
    ).fetchone()
    if cflag is None:
        conn.execute(
            "UPDATE dividend_benefits SET created_on = DATE '2026-11-25'"
        )
        conn.execute(
            "INSERT INTO settings(key, value)"
            " VALUES ('master_created_on_backfilled', '1')"
            " ON CONFLICT(key) DO NOTHING"
        )
        conn.commit()
    flag = conn.execute(
        "SELECT value FROM settings"
        " WHERE key = 'policy_master_migrated'"
    ).fetchone()
    if flag is None:
        conn.execute(
            "UPDATE dividend_benefits m SET policy = ("
            " SELECT b.policy FROM buy_lots b WHERE b.code = m.code"
            "  AND b.policy IS NOT NULL AND b.policy <> ''"
            "  ORDER BY b.purchase_date DESC NULLS LAST, b.id DESC"
            "  LIMIT 1)"
            " WHERE m.policy IS NULL"
        )
        conn.execute(
            "INSERT INTO settings(key, value)"
            " VALUES ('policy_master_migrated', '1')"
            " ON CONFLICT(key) DO NOTHING"
        )
        conn.commit()
    # No fetch can be running right after process start, so any run
    # still marked unfinished is stale (interrupted by a restart, or
    # its completion update was lost). Close it so the history never
    # shows a run as 'running' forever.
    cur = conn.execute(
        "UPDATE fetch_runs SET finished_at=now(),"
        " note = CASE WHEN note LIKE '%interrupted%' THEN note"
        " ELSE note || ' / interrupted (closed at startup)' END"
        " WHERE finished_at IS NULL"
    )
    conn.commit()
    return cur.rowcount
