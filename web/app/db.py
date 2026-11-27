"""PostgreSQL への接続とスキーマ・起動時の移行処理。

SCHEMA 文字列、マイグレーションの内容・順序、起動時の処理は
main.py にあったものをそのまま移したもので、振る舞いは変えない。
"""

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks(
  code text PRIMARY KEY, name text NOT NULL, market text);
CREATE TABLE IF NOT EXISTS holdings(
  id serial PRIMARY KEY, code text NOT NULL, account_type text, custody_type text,
  avg_price numeric NOT NULL, shares integer NOT NULL,
  lent_shares integer DEFAULT 0, ordered_shares integer DEFAULT 0,
  updated_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS buy_lots(
  id serial PRIMARY KEY, code text, name text, avg_price numeric, shares integer,
  purchase_date date, policy text, trigger_note text, note text,
  status text DEFAULT '約定済');
CREATE TABLE IF NOT EXISTS sales(
  id serial PRIMARY KEY, code text, name text, buy_price numeric, shares integer,
  purchase_date date, sell_price numeric, sell_shares integer,
  executed_shares integer DEFAULT 0, order_start_date date, executed_date date,
  profit numeric, status text DEFAULT '注文中', memo text);
CREATE TABLE IF NOT EXISTS dividend_benefits(
  code text PRIMARY KEY, dividend_forecast numeric, dividend_auto numeric,
  benefit_value numeric, benefit_desc text, info_updated_on date,
  funding_target boolean, judgment_on date, judgment_note text);
CREATE TABLE IF NOT EXISTS price_points(
  id serial PRIMARY KEY, code text NOT NULL, captured_at timestamptz NOT NULL,
  price numeric NOT NULL, prev_close numeric, day_high numeric, day_low numeric,
  volume bigint, source text);
CREATE TABLE IF NOT EXISTS settings(key text PRIMARY KEY, value text);
CREATE TABLE IF NOT EXISTS fetch_runs(
  id serial PRIMARY KEY, started_at timestamptz, finished_at timestamptz,
  ok_count integer, ng_count integer, note text);
ALTER TABLE fetch_runs ADD COLUMN IF NOT EXISTS total_count integer;
ALTER TABLE fetch_runs ADD COLUMN IF NOT EXISTS done_count integer DEFAULT 0;
CREATE TABLE IF NOT EXISTS dividend_changes(
  id serial PRIMARY KEY, run_id integer, code text NOT NULL,
  old_div numeric, new_div numeric, changed_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS daily_snapshots(
  code text NOT NULL, snap_date date NOT NULL,
  close_price numeric, prev_close numeric, open_price numeric,
  day_high numeric, day_low numeric, volume bigint,
  market_cap_m numeric, dividend_forecast numeric, dividend_yield numeric,
  per numeric, pbr numeric,
  captured_at timestamptz, source text,
  PRIMARY KEY (code, snap_date));
CREATE TABLE IF NOT EXISTS app_logs(
  id serial PRIMARY KEY, ts timestamptz NOT NULL, severity text,
  severity_number integer, body text, attributes jsonb,
  trace_id text, span_id text);
CREATE INDEX IF NOT EXISTS app_logs_ts_idx ON app_logs(ts DESC);
CREATE TABLE IF NOT EXISTS candidates(
  code text PRIMARY KEY, added_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS benefit_memos(
  code text PRIMARY KEY, shareholder_no text,
  deadline_1 date, deadline_2 date,
  delivery_waiting boolean NOT NULL DEFAULT FALSE, status text,
  form_arrival_1 date, form_arrival_2 date,
  item_arrival_1 date, item_arrival_2 date);
CREATE TABLE IF NOT EXISTS holding_events(
  id serial PRIMARY KEY, code text NOT NULL, account_type text,
  kind text NOT NULL, source_type text NOT NULL, source_id integer,
  delta_shares integer NOT NULL,
  shares_before integer NOT NULL, shares_after integer NOT NULL,
  avg_price_before numeric, avg_price_after numeric,
  lent_shares_before integer, ordered_shares_before integer,
  custody_type_before text,
  reverses_event_id integer, created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE holding_events ADD COLUMN IF NOT EXISTS lent_shares_before integer;
ALTER TABLE holding_events ADD COLUMN IF NOT EXISTS ordered_shares_before integer;
ALTER TABLE holding_events ADD COLUMN IF NOT EXISTS custody_type_before text;
ALTER TABLE buy_lots ADD COLUMN IF NOT EXISTS account_type text;
ALTER TABLE buy_lots ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS account_type text;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS policy text;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS memo text;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS target_price numeric;
ALTER TABLE dividend_benefits ADD COLUMN IF NOT EXISTS policy text;
ALTER TABLE dividend_benefits ADD COLUMN IF NOT EXISTS created_on date DEFAULT CURRENT_DATE;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS industry text;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS industry_fetched_on date;
"""


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
