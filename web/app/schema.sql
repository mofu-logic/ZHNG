-- アプリのスキーマ定義。db.py が起動時にこのファイルを読み込み、
-- そのまま実行する。
-- 口座区分の許可リストは、このファイル内の目印を読み込み時に
-- app.config.ACCOUNT_TYPES から生成したリテラル列へ置換して
-- 差し込む。許可値の正の定義は ACCOUNT_TYPES 側にあり、この
-- ファイルへ値は複写しない。
CREATE TABLE IF NOT EXISTS stocks(
  code text PRIMARY KEY, name text NOT NULL, market text);
CREATE TABLE IF NOT EXISTS holdings(
  id serial PRIMARY KEY, code text NOT NULL,
  account_type text NOT NULL
    CONSTRAINT holdings_account_type_allowlist
    CHECK (account_type IN (__ACCOUNT_TYPE_LIST__)),
  avg_price numeric NOT NULL, shares integer NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS holdings_code_account_uidx
  ON holdings (code, account_type);
CREATE TABLE IF NOT EXISTS buy_lots(
  id serial PRIMARY KEY, code text, name text, avg_price numeric, shares integer,
  purchase_date date, policy text, trigger_note text, note text,
  status text DEFAULT '約定済');
CREATE TABLE IF NOT EXISTS sales(
  id serial PRIMARY KEY, code text, name text, buy_price numeric,
  sell_price numeric, sell_shares integer,
  executed_shares integer DEFAULT 0, order_start_date date, executed_date date,
  profit numeric, status text DEFAULT '注文中', memo text);
CREATE TABLE IF NOT EXISTS dividend_benefits(
  code text PRIMARY KEY, dividend_forecast numeric, dividend_auto numeric,
  benefit_value numeric, benefit_desc text,
  funding_target boolean, judgment_on date, judgment_note text);
CREATE TABLE IF NOT EXISTS price_points(
  id serial PRIMARY KEY, code text NOT NULL, captured_at timestamptz NOT NULL,
  price numeric NOT NULL, day_high numeric, day_low numeric,
  volume bigint);
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
  id serial PRIMARY KEY, code text NOT NULL,
  account_type text NOT NULL
    CONSTRAINT holding_events_account_type_allowlist
    CHECK (account_type IN (__ACCOUNT_TYPE_LIST__)),
  kind text NOT NULL, source_type text NOT NULL, source_id integer,
  delta_shares integer NOT NULL,
  shares_before integer NOT NULL, shares_after integer NOT NULL,
  avg_price_before numeric, avg_price_after numeric,
  reverses_event_id integer, created_at timestamptz NOT NULL DEFAULT now());
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
-- 口座区分の移行（既存DB向け。すべて冪等で、起動のたびに走っても
-- 安全な形にしてある）。口座区分は許可リスト（ACCOUNT_TYPES）の
-- 値だけを許し、保有とその変更記録では NULL を許さない。買い・
-- 売り履歴では NULL（保有に連動しない印）は従来どおり許す。
-- SET NOT NULL は繰り返し実行してもエラーにならない。
ALTER TABLE holdings ALTER COLUMN account_type SET NOT NULL;
ALTER TABLE holding_events ALTER COLUMN account_type SET NOT NULL;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'holdings_account_type_allowlist') THEN
    ALTER TABLE holdings ADD CONSTRAINT holdings_account_type_allowlist
      CHECK (account_type IN (__ACCOUNT_TYPE_LIST__));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'holding_events_account_type_allowlist') THEN
    ALTER TABLE holding_events
      ADD CONSTRAINT holding_events_account_type_allowlist
      CHECK (account_type IN (__ACCOUNT_TYPE_LIST__));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'buy_lots_account_type_allowlist') THEN
    ALTER TABLE buy_lots ADD CONSTRAINT buy_lots_account_type_allowlist
      CHECK (account_type IS NULL
             OR account_type IN (__ACCOUNT_TYPE_LIST__));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'sales_account_type_allowlist') THEN
    ALTER TABLE sales ADD CONSTRAINT sales_account_type_allowlist
      CHECK (account_type IS NULL
             OR account_type IN (__ACCOUNT_TYPE_LIST__));
  END IF;
END $$;
-- 一意インデックスは部分インデックス（口座区分が NULL の行を対象
-- 外にする旧定義）から通常の一意インデックスへ張り替える。
-- CREATE ... IF NOT EXISTS では既存の部分定義は置き換わらないため、
-- 部分定義のときだけ落としてから作り直す。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_indexes
             WHERE indexname = 'holdings_code_account_uidx'
               AND indexdef LIKE '%WHERE%') THEN
    DROP INDEX holdings_code_account_uidx;
  END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS holdings_code_account_uidx
  ON holdings (code, account_type);

-- 未使用カラムの削除（既存DB向け。カラム棚卸しで、コードでの
-- 読み書きが無く保持する意味も薄いと判断した14列を落とす。
-- すべて冪等で、起動のたびに走っても安全な形にしてある）。
ALTER TABLE holdings DROP COLUMN IF EXISTS custody_type;
ALTER TABLE holdings DROP COLUMN IF EXISTS lent_shares;
ALTER TABLE holdings DROP COLUMN IF EXISTS ordered_shares;
ALTER TABLE holdings DROP COLUMN IF EXISTS updated_at;
ALTER TABLE holding_events DROP COLUMN IF EXISTS custody_type_before;
ALTER TABLE holding_events DROP COLUMN IF EXISTS lent_shares_before;
ALTER TABLE holding_events DROP COLUMN IF EXISTS ordered_shares_before;
ALTER TABLE sales DROP COLUMN IF EXISTS purchase_date;
ALTER TABLE sales DROP COLUMN IF EXISTS shares;
ALTER TABLE daily_snapshots DROP COLUMN IF EXISTS source;
ALTER TABLE daily_snapshots DROP COLUMN IF EXISTS captured_at;
ALTER TABLE price_points DROP COLUMN IF EXISTS prev_close;
ALTER TABLE price_points DROP COLUMN IF EXISTS source;
ALTER TABLE dividend_benefits DROP COLUMN IF EXISTS info_updated_on;
