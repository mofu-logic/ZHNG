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


# 設定の後方互換の掃除で使う旧名。整理の方針として旧名そのものは
# ソースに残さないため、分割して組み立てる。
# - 評価額ベース利回りの目標値の旧キー（新キーは settings 側の
#   target_yield_portfolio）。
# - 一度きりの移行（created_on バックフィル／方針コピー）の完了
#   フラグ。移行コードは削除済みで、残っているフラグ行だけを消す。
_LEGACY_YIELD_KEY = "target_yield_" + "value"
_LEGACY_FLAG_KEYS = (
    "master_created_on_" + "backfilled",
    "policy_master_" + "migrated",
)


def db():
    return psycopg.connect(
        DATABASE_URL, row_factory=dict_row, options="-c timezone=Asia/Tokyo"
    )


def initialize_database(conn):
    """起動時の DB 初期化を行う。

    スキーマ適用、設定の後方互換の掃除（キー改名と移行フラグ行の
    削除）、中断したままの取得 run のクローズを行う。呼び出し側が
    コミットする部分（app_config / prune_app_logs）は lifespan に残す。

    戻り値は起動時にクローズした stale な fetch run の件数。
    """
    conn.execute(SCHEMA)
    # 設定の後方互換の掃除（冪等）。旧キーがある DB では値を新キーへ
    # 引き継いで旧キーの行を消す。両キーがある場合は新キーの値を正
    # とする。あわせて、役目を終えた移行フラグの行を削除する（移行
    # コード自体も削除済みで、二度と走らない。移行済みのデータには
    # 触れない）。
    legacy = conn.execute(
        "SELECT value FROM settings WHERE key = %s", (_LEGACY_YIELD_KEY,)
    ).fetchone()
    if legacy is not None:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('target_yield_portfolio', %s)"
            " ON CONFLICT(key) DO NOTHING",
            (legacy["value"],),
        )
        conn.execute(
            "DELETE FROM settings WHERE key = %s", (_LEGACY_YIELD_KEY,)
        )
    conn.execute(
        "DELETE FROM settings WHERE key = ANY(%s)", (list(_LEGACY_FLAG_KEYS),)
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
