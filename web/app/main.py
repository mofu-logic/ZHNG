"""Stock portfolio dashboard.

Data model mirrors the user's spreadsheet:
- holdings: broker snapshot rows (stock x account type) - the source of truth
- buy_lots / sales: trade history incl. unfilled orders and partial fills
- dividend_benefits: per-stock dividend (announced value), benefit, judgment memo
- price_points: fetched price history; the latest row per code is the current price
Indicators are computed on read, reproducing the View sheet formulas.
"""
import asyncio
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import psycopg
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from app.observability import (
    OtelBridgeHandler,
    configure_otlp,
    emit_event,
    setup_observability,
)

DATABASE_URL = os.environ["DATABASE_URL"]

tracer = setup_observability(DATABASE_URL)

logger = logging.getLogger("stock-app")
if not logger.handlers:
    # Records are bridged into the OpenTelemetry logs pipeline
    # (structured JSON on stdout + the app_logs table). See
    # app/observability.py.
    logger.addHandler(OtelBridgeHandler())
    logger.setLevel(logging.INFO)
    logger.propagate = False
JST = ZoneInfo("Asia/Tokyo")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

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
"""


def db():
    return psycopg.connect(
        DATABASE_URL, row_factory=dict_row, options="-c timezone=Asia/Tokyo"
    )


def fnum(v):
    return None if v is None else float(v)


# ---------------------------------------------------------------- formatting
def fmt_price(v):
    if v is None:
        return "―"
    v = float(v)
    s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s


def fmt_yen(v):
    if v is None:
        return "―"
    v = float(v)
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.1f}"


def fmt_signed(v):
    if v is None:
        return "―"
    v = float(v)
    sign = "+" if v > 0 else ""
    return sign + fmt_yen(v)


def fmt_pct(v):
    if v is None:
        return "―"
    return f"{float(v) * 100:.2f}%"


def fmt_pct_value(v):
    """Format a value that is already in percent units (e.g. snapshot yields)."""
    if v is None:
        return "―"
    return f"{float(v):.2f}%"


def fmt_pct_signed(v):
    if v is None:
        return "―"
    v = float(v)
    return ("+" if v > 0 else "") + f"{v:.2f}%"


templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.filters["pctv"] = fmt_pct_value
templates.env.filters["pcts"] = fmt_pct_signed
templates.env.filters["signed2"] = lambda v: (
    "" if v is None else ("+" if float(v) > 0 else "") + fmt_price(v)
)
templates.env.filters["price"] = fmt_price
templates.env.filters["yen"] = fmt_yen
templates.env.filters["signed"] = fmt_signed
templates.env.filters["pct"] = fmt_pct


# ---------------------------------------------------------------- price fetch
_fetch_lock = asyncio.Lock()


def _page_fields(html):
    fields = {}
    for term, dd in re.findall(
        r"_DataListItem__term_[^\" >]*\">(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S
    ):
        name = "".join(
            re.findall(r"_DataListItem__(?:name|sub)_[^\" >]*\">([^<]*)<", term)
        )
        if not name or name in fields:
            continue
        vm = re.search(r"_StyledNumber__value_[^\"]*\">([\d,\.]+)", dd)
        if vm:
            fields[name] = float(vm.group(1).replace(",", ""))
        elif name.startswith("PER"):
            # PER is rendered like （連）11.17 倍 without a styled number span.
            plain = re.sub(r"<[^>]+>", "", dd)
            pm = re.search(r"([\d,\.]+)\s*倍", plain)
            if pm:
                fields[name] = float(pm.group(1).replace(",", ""))
    return fields


def parse_jp_page(html):
    """Extract the current price and the labeled data-list values from a
    Yahoo! Finance Japan quote page (finance.yahoo.co.jp/quote/{code}.T|.N)."""
    m = re.search(r"_PriceBoard.*?_StyledNumber__value_[^\"]*\">([\d,]+)<", html, re.S)
    price = float(m.group(1).replace(",", "")) if m else None
    return price, _page_fields(html)


def parse_index_page(html):
    """Nikkei 225 page (quote/998407.O): the main value sits in a
    _CommonPriceBoard__price_ element; the generic _PriceBoard regex
    picks up a different index (e.g. TOPIX) that appears earlier."""
    m = re.search(
        r"_CommonPriceBoard__price_[^\"]*\">.*?_StyledNumber__value_[^\"]*\">([\d,\.]+)<",
        html,
        re.S,
    )
    price = float(m.group(1).replace(",", "")) if m else None
    return price, _page_fields(html)


async def fetch_all(note="manual"):
    if _fetch_lock.locked():
        return
    async with _fetch_lock:
        with db() as conn:
            targets = conn.execute(
                "SELECT DISTINCT h.code, s.market FROM holdings h"
                " JOIN stocks s ON s.code = h.code ORDER BY h.code"
            ).fetchall()
            run_id = conn.execute(
                "INSERT INTO fetch_runs(started_at, note, total_count, done_count)"
                " VALUES (now(), %s, %s, 0) RETURNING id",
                (note, len(targets)),
            ).fetchone()["id"]
            conn.commit()
        emit_event("fetch.started", run_id=run_id, kind=note, total=len(targets))
        ok = ng = 0
        first_error = None
        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25) as client:
            for t in targets:
                code = t["code"]
                # All quotes come from the Yahoo! Finance Japan quote pages
                # (.T for Tokyo, .N for Nagoya). The pages also carry the
                # company-forecast dividend, which auto-updates the master.
                suffix = "N" if t["market"] == "名証" else "T"
                try:
                    r = await client.get(
                        f"https://finance.yahoo.co.jp/quote/{code}.{suffix}"
                    )
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    price, fields = parse_jp_page(r.text)
                    if price is None or price <= 0:
                        raise RuntimeError("price not found in page")
                    div = fields.get("1株配当（会社予想）")
                    volume = fields.get("出来高")
                    # The 01:00 batch reads the previous trading day's close;
                    # JST-9h maps the fetch moment to that trading day.
                    snap_date = (datetime.now(JST) - timedelta(hours=9)).date()
                    with db() as conn:
                        conn.execute(
                            "INSERT INTO price_points(code, captured_at, price, prev_close,"
                            " day_high, day_low, volume, source)"
                            " VALUES (%s, now(), %s, %s, %s, %s, %s, 'yahoo-jp')",
                            (
                                code,
                                price,
                                fields.get("前日終値"),
                                fields.get("高値"),
                                fields.get("安値"),
                                int(volume) if volume is not None else None,
                            ),
                        )
                        conn.execute(
                            "INSERT INTO daily_snapshots(code, snap_date, close_price,"
                            " prev_close, open_price, day_high, day_low, volume,"
                            " market_cap_m, dividend_forecast, dividend_yield, per, pbr,"
                            " captured_at, source)"
                            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), 'yahoo-jp')"
                            " ON CONFLICT (code, snap_date) DO UPDATE SET"
                            " close_price=EXCLUDED.close_price,"
                            " prev_close=EXCLUDED.prev_close,"
                            " open_price=EXCLUDED.open_price, day_high=EXCLUDED.day_high,"
                            " day_low=EXCLUDED.day_low, volume=EXCLUDED.volume,"
                            " market_cap_m=EXCLUDED.market_cap_m,"
                            " dividend_forecast=EXCLUDED.dividend_forecast,"
                            " dividend_yield=EXCLUDED.dividend_yield, per=EXCLUDED.per,"
                            " pbr=EXCLUDED.pbr, captured_at=now()",
                            (
                                code,
                                snap_date,
                                price,
                                fields.get("前日終値"),
                                fields.get("始値"),
                                fields.get("高値"),
                                fields.get("安値"),
                                int(volume) if volume is not None else None,
                                fields.get("時価総額"),
                                div,
                                fields.get("配当利回り（会社予想）"),
                                fields.get("PER（会社予想）"),
                                fields.get("PBR（実績）"),
                            ),
                        )
                        if div is not None:
                            old_row = conn.execute(
                                "SELECT dividend_forecast FROM dividend_benefits"
                                " WHERE code = %s",
                                (code,),
                            ).fetchone()
                            if (
                                old_row
                                and old_row["dividend_forecast"] is not None
                                and float(old_row["dividend_forecast"]) != float(div)
                            ):
                                conn.execute(
                                    "INSERT INTO dividend_changes(run_id, code,"
                                    " old_div, new_div) VALUES (%s, %s, %s, %s)",
                                    (
                                        run_id,
                                        code,
                                        old_row["dividend_forecast"],
                                        div,
                                    ),
                                )
                            conn.execute(
                                "INSERT INTO dividend_benefits(code, dividend_forecast,"
                                " dividend_auto) VALUES (%s, %s, %s)"
                                " ON CONFLICT (code) DO UPDATE SET"
                                " dividend_auto=EXCLUDED.dividend_auto,"
                                " dividend_forecast=EXCLUDED.dividend_forecast,"
                                " info_updated_on = CASE WHEN"
                                " dividend_benefits.dividend_forecast IS DISTINCT FROM"
                                " EXCLUDED.dividend_forecast THEN CURRENT_DATE"
                                " ELSE dividend_benefits.info_updated_on END",
                                (code, div, div),
                            )
                        conn.commit()
                    ok += 1
                except Exception as e:
                    ng += 1
                    emit_event(
                        "fetch.code_failed",
                        severity="WARNING",
                        run_id=run_id,
                        code=code,
                        error=f"{type(e).__name__}: {e}"[:200],
                    )
                    if first_error is None:
                        first_error = f"{type(e).__name__}: {e}"[:120]
                with db() as conn:
                    conn.execute(
                        "UPDATE fetch_runs SET done_count = done_count + 1 WHERE id = %s",
                        (run_id,),
                    )
                    conn.commit()
                # Gentle pacing: Yahoo rate-limits bursts from this network.
                # 25-45s with random jitter per request (verified 2026-11-25).
                await asyncio.sleep(random.uniform(25, 45))
        # Nikkei 225 close, from the same source (Yahoo! Finance Japan,
        # quote/998407.O). Stored as a daily snapshot only (code N225);
        # a failure here must not affect the stock counts above.
        try:
            async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25) as ic:
                r = await ic.get("https://finance.yahoo.co.jp/quote/998407.O")
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                nprice, nfields = parse_index_page(r.text)
                if nprice is None or nprice <= 0:
                    raise RuntimeError("index price not found in page")
                snap_date = (datetime.now(JST) - timedelta(hours=9)).date()
                with db() as conn:
                    conn.execute(
                        "INSERT INTO daily_snapshots(code, snap_date, close_price,"
                        " prev_close, open_price, day_high, day_low, volume,"
                        " market_cap_m, dividend_forecast, dividend_yield, per, pbr,"
                        " captured_at, source)"
                        " VALUES ('N225',%s,%s,%s,%s,%s,%s,NULL,NULL,NULL,NULL,NULL,NULL,"
                        " now(), 'yahoo-jp')"
                        " ON CONFLICT (code, snap_date) DO UPDATE SET"
                        " close_price=EXCLUDED.close_price,"
                        " prev_close=EXCLUDED.prev_close,"
                        " open_price=EXCLUDED.open_price, day_high=EXCLUDED.day_high,"
                        " day_low=EXCLUDED.day_low, captured_at=now()",
                        (
                            snap_date,
                            nprice,
                            nfields.get("前日終値"),
                            nfields.get("始値"),
                            nfields.get("高値"),
                            nfields.get("安値"),
                        ),
                    )
                    conn.commit()
        except Exception:
            logger.exception("Nikkei 225 fetch failed (run %s)", run_id)
        await _finish_run(run_id, ok, ng, first_error)
        emit_event("fetch.finished", run_id=run_id, kind=note, ok=ok, ng=ng)


async def _finish_run(run_id, ok, ng, first_error):
    """Record a fetch run's completion. This used to be a single unguarded
    UPDATE at the very end of fetch_all: if it failed, the scheduler's
    blanket `except: pass` swallowed the error and the run stayed
    'running' forever (run 7, 2026-11-26). Retry a few times and log
    every failure so it cannot go unnoticed again."""
    for attempt in range(1, 4):
        try:
            with db() as conn:
                conn.execute(
                    "UPDATE fetch_runs SET finished_at=now(), ok_count=%s, ng_count=%s,"
                    " note = CASE WHEN %s::text IS NULL THEN note"
                    " ELSE note || ' / ' || %s::text END"
                    " WHERE id=%s",
                    (ok, ng, first_error, first_error, run_id),
                )
                conn.commit()
            return
        except Exception:
            logger.exception(
                "fetch run %s: completion update failed (attempt %s/3)",
                run_id,
                attempt,
            )
            await asyncio.sleep(2)
    logger.error("fetch run %s: completion update gave up after 3 attempts", run_id)


async def scheduler():
    while True:
        now = datetime.now(JST)
        target = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await fetch_all("batch")
        except Exception:
            logger.exception("scheduled fetch failed")


@asynccontextmanager
async def lifespan(app):
    with db() as conn:
        conn.execute(SCHEMA)
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
        if cur.rowcount:
            logger.warning("closed %s stale fetch run(s) at startup", cur.rowcount)
        # Operation logs are kept for the configured retention period,
        # and the OTel integration follows the stored settings.
        config = app_config(conn)
        prune_app_logs(conn, config["log_retention_days"])
        conn.commit()
    configure_otlp(config["otel_enabled"], config["otel_endpoint"])
    emit_event("app.started")
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


app = FastAPI(
    lifespan=lifespan,
    title="株式ポートフォリオ API",
    version="1.0.0",
    description=(
        "売買履歴・配当優待マスタ・設定の登録／更新／参照を行う "
        "JSON API（/api/v1）。画面と外部連携で同一の IF を使う。"
        "このドキュメントは OpenAPI 仕様として /openapi.json でも取得できる。"
    ),
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    path = request.url.path
    if path in ("/healthz", "/api/fetch-status"):
        # Health checks and the data page's polling would flood the log.
        return await call_next(request)
    start = time.perf_counter()
    with tracer.start_as_current_span(f"{request.method} {path}"):
        response = await call_next(request)
        emit_event(
            "http.request",
            method=request.method,
            path=path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return response


def round_half_up(v):
    return int(v + 0.5)


def get_settings(conn):
    """Numeric settings as floats (the KPI values). Non-numeric values
    (e.g. the OTel endpoint) are skipped here and read via app_config."""
    out = {}
    for r in conn.execute("SELECT key, value FROM settings").fetchall():
        try:
            out[r["key"]] = float(r["value"])
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db() as conn:
        st = get_settings(conn)
        rows = conn.execute(
            """
            WITH latest AS (
              SELECT DISTINCT ON (code) code, price, captured_at
              FROM price_points ORDER BY code, captured_at DESC
            )
            SELECT h.id, h.code, h.account_type, h.avg_price, h.shares,
                   h.lent_shares, h.ordered_shares,
                   s.name, s.market,
                   d.dividend_forecast, d.benefit_value, d.judgment_note,
                   l.price AS cur_price, l.captured_at AS price_at,
                   (SELECT policy FROM buy_lots b WHERE b.code = h.code
                    ORDER BY (b.avg_price = h.avg_price AND b.shares = h.shares) DESC,
                             b.purchase_date DESC NULLS LAST, b.id DESC LIMIT 1) AS policy,
                   (SELECT purchase_date FROM buy_lots b WHERE b.code = h.code
                    ORDER BY (b.avg_price = h.avg_price AND b.shares = h.shares) DESC,
                             b.purchase_date DESC NULLS LAST, b.id DESC LIMIT 1) AS purchase_date
            FROM holdings h
            JOIN stocks s ON s.code = h.code
            LEFT JOIN dividend_benefits d ON d.code = h.code
            LEFT JOIN latest l ON l.code = h.code
            ORDER BY h.code, h.account_type
            """
        ).fetchall()

    items = []
    price_at = None
    for r in rows:
        price = fnum(r["cur_price"])
        avg = fnum(r["avg_price"])
        shares = r["shares"]
        div = fnum(r["dividend_forecast"]) or 0.0
        benefit = fnum(r["benefit_value"]) or 0.0
        value = price * shares if price is not None else None
        pnl = (price - avg) * shares if price is not None else None
        div_amt = div * shares
        item = dict(r)
        item.update(
            value=value,
            pnl=pnl,
            pnl_ratio=(price - avg) / avg if price is not None and avg else None,
            div_amt=div_amt,
            y_buy=div / avg if avg else None,
            y_now=div / price if price else None,
            yb_now=(div_amt + benefit) / (price * shares) if price else None,
            yb_buy=(div_amt + benefit) / (avg * shares) if avg else None,
        )
        items.append(item)
        if r["price_at"] and (price_at is None or r["price_at"] > price_at):
            price_at = r["price_at"]

    n = len(items)
    total_value = sum(i["value"] for i in items if i["value"] is not None)
    total_div = sum(i["div_amt"] for i in items)
    tax = st.get("tax_rate", 0.20315)
    monthly = total_div * (1 - tax) / 12
    avg_y_buy = sum(i["y_buy"] or 0 for i in items) / n if n else 0
    avg_y_now = sum(i["y_now"] or 0 for i in items) / n if n else 0
    kpi = dict(
        count=n,
        total_value=total_value,
        total_div=total_div,
        monthly=monthly,
        avg_y_buy=avg_y_buy,
        avg_y_now=avg_y_now,
        div_ratio=total_div / total_value if total_value else None,
        target_div=st.get("target_dividend_total"),
        target_monthly=st.get("target_monthly_income"),
        target_y_buy=st.get("target_yield_buy"),
        target_y_now=st.get("target_yield_now"),
        target_y_value=st.get("target_yield_value", 0.04),
    )
    policies = sorted({i["policy"] for i in items if i["policy"]})
    accounts = sorted({i["account_type"] for i in items if i["account_type"]})
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        dict(items=items, kpi=kpi, price_at=price_at, policies=policies, accounts=accounts),
    )


def _prev_trading_date(d, prev_snap_date):
    if prev_snap_date:
        return prev_snap_date
    # Fallback until two snapshot dates have accumulated: step back over
    # weekends only (exchange holidays are not accounted for).
    cur = d - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


@app.get("/summary", response_class=HTMLResponse)
def summary(request: Request):
    with db() as conn:
        run = conn.execute(
            "SELECT * FROM fetch_runs WHERE finished_at IS NOT NULL"
            " AND note LIKE 'batch%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            run = conn.execute(
                "SELECT * FROM fetch_runs WHERE finished_at IS NOT NULL"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()
        dates = conn.execute(
            "SELECT DISTINCT snap_date FROM daily_snapshots WHERE code <> 'N225'"
            " ORDER BY snap_date DESC LIMIT 2"
        ).fetchall()
        latest_date = dates[0]["snap_date"] if dates else None
        prev_date = dates[1]["snap_date"] if len(dates) > 1 else None
        snaps = {}
        if latest_date:
            for r in conn.execute(
                "SELECT * FROM daily_snapshots WHERE snap_date = %s", (latest_date,)
            ).fetchall():
                snaps[r["code"]] = r
        prev_snaps = {}
        if prev_date:
            for r in conn.execute(
                "SELECT * FROM daily_snapshots WHERE snap_date = %s", (prev_date,)
            ).fetchall():
                prev_snaps[r["code"]] = r
        names = {
            r["code"]: r["name"]
            for r in conn.execute("SELECT code, name FROM stocks").fetchall()
        }
        changes = []
        if run:
            for c in conn.execute(
                "SELECT * FROM dividend_changes WHERE run_id = %s ORDER BY code",
                (run["id"],),
            ).fetchall():
                snap = snaps.get(c["code"]) or {}
                prev_snap = prev_snaps.get(c["code"]) or {}
                new_yield = fnum(snap.get("dividend_yield"))
                if prev_snap.get("dividend_yield") is not None:
                    old_yield = fnum(prev_snap["dividend_yield"])
                elif snap.get("close_price") and c["old_div"] is not None:
                    # Only one snapshot date so far: express the old yield
                    # at the same (latest) price to isolate the dividend change.
                    old_yield = float(c["old_div"]) / float(snap["close_price"]) * 100
                else:
                    old_yield = None
                changes.append(
                    dict(
                        code=c["code"],
                        name=names.get(c["code"], ""),
                        old_div=fnum(c["old_div"]),
                        new_div=fnum(c["new_div"]),
                        old_yield=old_yield,
                        new_yield=new_yield,
                    )
                )
    # Nikkei 225: latest snapshot close = previous trading day,
    # its prev_close = the day before that.
    nikkei = None
    nsnap = snaps.get("N225")
    if nsnap and nsnap.get("close_price") and nsnap.get("prev_close"):
        close = float(nsnap["close_price"])
        prev_close = float(nsnap["prev_close"])
        nikkei = dict(
            date=latest_date,
            prev_date=_prev_trading_date(latest_date, prev_date),
            close=close,
            prev_close=prev_close,
            diff=close - prev_close,
            pct=(close - prev_close) / prev_close * 100,
        )
    # Top movers: latest close vs previous close (previous snapshot date
    # when available, otherwise the snapshot's own prev_close).
    movers = []
    for code, s in snaps.items():
        if code == "N225":
            continue
        close = fnum(s.get("close_price"))
        if prev_date and code in prev_snaps:
            prev_price = fnum(prev_snaps[code].get("close_price"))
        else:
            prev_price = fnum(s.get("prev_close"))
        if close and prev_price:
            movers.append(
                dict(
                    code=code,
                    name=names.get(code, ""),
                    prev_price=prev_price,
                    price=close,
                    pct=(close - prev_price) / prev_price * 100,
                )
            )
    movers.sort(key=lambda m: abs(m["pct"]), reverse=True)
    counts = dict(
        up=sum(1 for m in movers if m["pct"] > 0),
        down=sum(1 for m in movers if m["pct"] < 0),
        flat=sum(1 for m in movers if m["pct"] == 0),
    )
    batch = None
    if run:
        mins = None
        if run["started_at"] and run["finished_at"]:
            mins = round(
                (run["finished_at"] - run["started_at"]).total_seconds() / 60
            )
        batch = dict(
            total=run["total_count"] or 0,
            done=run["done_count"] or 0,
            ok=run["ok_count"] or 0,
            ng=run["ng_count"] or 0,
            started=run["started_at"],
            finished=run["finished_at"],
            minutes=mins,
        )
    return templates.TemplateResponse(
        request,
        "summary.html",
        dict(
            updated_at=run["finished_at"] if run else None,
            batch=batch,
            changes=changes,
            nikkei=nikkei,
            movers=movers[:10],
            counts=counts,
            latest_date=latest_date,
            prev_date=_prev_trading_date(latest_date, prev_date) if latest_date else None,
        ),
    )


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    with db() as conn:
        buys = conn.execute(
            "SELECT * FROM buy_lots ORDER BY purchase_date DESC NULLS LAST, id DESC"
        ).fetchall()
        sales = conn.execute(
            "SELECT * FROM sales ORDER BY order_start_date DESC NULLS LAST, id DESC"
        ).fetchall()
    return templates.TemplateResponse(request, "history.html", dict(buys=buys, sales=sales))


def _upsert_stock(conn, code, name):
    if code and name:
        conn.execute(
            "INSERT INTO stocks(code, name) VALUES (%s, %s)"
            " ON CONFLICT(code) DO NOTHING",
            (code, name),
        )


# ---------------------------------------------------------------- services
# The write/read logic for trades, the dividend/benefit master and the
# settings lives here, shared by the JSON API (/api/v1) and anything
# else that needs it. Derivations (trade status, sale profit, monthly
# income target) exist exactly once, in this layer.
TRADE_STATUSES = ("注文中", "一部約定", "約定済")


def create_buy(conn, *, code, name, avg_price, shares, purchase_date,
               policy, trigger_note, note, status):
    final_status = status if status in TRADE_STATUSES else "約定済"
    _upsert_stock(conn, code, name)
    row = conn.execute(
        "INSERT INTO buy_lots(code, name, avg_price, shares, purchase_date,"
        " policy, trigger_note, note, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " RETURNING *",
        (code, name, avg_price, shares, purchase_date, policy,
         trigger_note, note, final_status),
    ).fetchone()
    return row


def delete_buy(conn, lot_id):
    cur = conn.execute("DELETE FROM buy_lots WHERE id = %s", (lot_id,))
    return cur.rowcount > 0


def create_sale(conn, *, code, name, buy_price, shares, purchase_date,
                sell_price, sell_shares, executed_shares, order_start_date,
                executed_date, profit, status, memo):
    ex = executed_shares or 0
    pf = profit
    if pf is None and buy_price is not None and sell_price is not None and ex:
        pf = (sell_price - buy_price) * ex
    if status not in TRADE_STATUSES:
        # "自動" (or anything unspecified): derive from executed shares.
        if sell_shares and ex >= sell_shares:
            status = "約定済"
        elif ex > 0:
            status = "一部約定"
        else:
            status = "注文中"
    _upsert_stock(conn, code, name)
    row = conn.execute(
        "INSERT INTO sales(code, name, buy_price, shares, purchase_date,"
        " sell_price, sell_shares, executed_shares, order_start_date,"
        " executed_date, profit, status, memo)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (code, name, buy_price, shares, purchase_date, sell_price,
         sell_shares, ex, order_start_date, executed_date, pf, status, memo),
    ).fetchone()
    return row


def delete_sale(conn, sale_id):
    cur = conn.execute("DELETE FROM sales WHERE id = %s", (sale_id,))
    return cur.rowcount > 0


def update_benefit(conn, code, *, benefit_value, benefit_desc, judgment_on,
                   judgment_note, funding_target):
    cur = conn.execute(
        "UPDATE dividend_benefits SET benefit_value=%s,"
        " benefit_desc=%s, judgment_on=%s, judgment_note=%s, funding_target=%s,"
        " info_updated_on=%s WHERE code=%s",
        (
            benefit_value,
            benefit_desc,
            judgment_on,
            judgment_note,
            True if funding_target else None,
            datetime.now(JST).date(),
            code,
        ),
    )
    return cur.rowcount > 0


def settings_view(st):
    """The settings as the UI/API present them (percent units, with the
    derived monthly target)."""
    total = st.get("target_dividend_total", 0)
    tax = st.get("tax_rate", 0.20315)
    return dict(
        tax_rate_pct=(tax * 100),
        target_dividend_total=total,
        target_monthly_income=round_half_up(total * (1 - tax) / 12),
        target_yield_buy_pct=(st.get("target_yield_buy", 0) * 100),
        target_yield_now_pct=(st.get("target_yield_now", 0) * 100),
        target_yield_value_pct=(st.get("target_yield_value", 0.04) * 100),
    )


def update_settings(conn, *, tax_rate_pct, target_dividend_total,
                    target_yield_buy_pct, target_yield_now_pct,
                    target_yield_value_pct, log_retention_days=90,
                    otel_enabled=False, otel_endpoint=None):
    # Monthly after-tax income target is derived, not entered directly:
    # annual dividend target x (1 - tax rate) / 12.
    monthly = round_half_up(target_dividend_total * (1 - tax_rate_pct / 100) / 12)
    values = {
        "tax_rate": tax_rate_pct / 100,
        "target_dividend_total": target_dividend_total,
        "target_monthly_income": float(monthly),
        "target_yield_buy": target_yield_buy_pct / 100,
        "target_yield_now": target_yield_now_pct / 100,
        "target_yield_value": target_yield_value_pct / 100,
        "log_retention_days": float(log_retention_days),
    }
    for k, v in values.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (%s, %s)"
            " ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
            (k, repr(v)),
        )
    # Non-numeric settings are stored as plain strings.
    for k, v in (
        ("otel_enabled", "true" if otel_enabled else "false"),
        ("otel_endpoint", otel_endpoint or ""),
    ):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (%s, %s)"
            " ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
            (k, v),
        )
    values["otel_enabled"] = bool(otel_enabled)
    return values


DEFAULT_LOG_RETENTION_DAYS = 90


def _raw_setting(conn, key):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = %s", (key,)
    ).fetchone()
    return row["value"] if row else None


def app_config(conn):
    """The non-KPI app settings: log retention and OTel integration."""
    retention = _raw_setting(conn, "log_retention_days")
    try:
        retention_days = (
            int(float(retention)) if retention else DEFAULT_LOG_RETENTION_DAYS
        )
    except ValueError:
        retention_days = DEFAULT_LOG_RETENTION_DAYS
    return dict(
        log_retention_days=retention_days,
        otel_enabled=_raw_setting(conn, "otel_enabled") == "true",
        otel_endpoint=_raw_setting(conn, "otel_endpoint") or "",
    )


def settings_full_view(conn):
    """Everything the settings page / API exposes: KPI view + app config."""
    view = settings_view(get_settings(conn))
    view.update(app_config(conn))
    return view


def prune_app_logs(conn, retention_days):
    cur = conn.execute(
        "DELETE FROM app_logs WHERE ts < now() - make_interval(days => %s)",
        (retention_days,),
    )
    return cur.rowcount


@app.get("/master", response_class=HTMLResponse)
def master(request: Request):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT d.*, s.name FROM dividend_benefits d
            LEFT JOIN stocks s ON s.code = d.code ORDER BY d.code
            """
        ).fetchall()
    return templates.TemplateResponse(request, "master.html", dict(rows=rows))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with db() as conn:
        view = settings_full_view(conn)
    return templates.TemplateResponse(request, "settings.html", view)


# ---------------------------------------------------------------- JSON API
# /api/v1 is the single interface for trades, the dividend/benefit
# master and the settings: the web UI uses it, and future external
# integrations use the same paths and shapes. Writes go through the
# service layer above, so derivations and log events are identical
# no matter which client calls.


def _date_out(v):
    return v.isoformat() if v is not None else None


def _buy_out(r):
    return dict(
        id=r["id"], code=r["code"], name=r["name"],
        avg_price=fnum(r["avg_price"]), shares=r["shares"],
        purchase_date=_date_out(r["purchase_date"]), policy=r["policy"],
        trigger_note=r["trigger_note"], note=r["note"], status=r["status"],
    )


def _sale_out(r):
    return dict(
        id=r["id"], code=r["code"], name=r["name"],
        buy_price=fnum(r["buy_price"]), shares=r["shares"],
        purchase_date=_date_out(r["purchase_date"]),
        sell_price=fnum(r["sell_price"]), sell_shares=r["sell_shares"],
        executed_shares=r["executed_shares"],
        order_start_date=_date_out(r["order_start_date"]),
        executed_date=_date_out(r["executed_date"]),
        profit=fnum(r["profit"]), status=r["status"], memo=r["memo"],
    )


def _benefit_out(r):
    return dict(
        code=r["code"], name=r.get("name"),
        dividend_forecast=fnum(r["dividend_forecast"]),
        dividend_auto=fnum(r["dividend_auto"]),
        benefit_value=fnum(r["benefit_value"]),
        benefit_desc=r["benefit_desc"],
        info_updated_on=_date_out(r["info_updated_on"]),
        funding_target=r["funding_target"],
        judgment_on=_date_out(r["judgment_on"]),
        judgment_note=r["judgment_note"],
    )


class BuyIn(BaseModel):
    code: str | None = None
    name: str | None = None
    avg_price: float | None = None
    shares: int | None = None
    purchase_date: date | None = None
    policy: str | None = None
    trigger_note: str | None = None
    note: str | None = None
    status: str | None = None


class SaleIn(BaseModel):
    code: str | None = None
    name: str | None = None
    buy_price: float | None = None
    shares: int | None = None
    purchase_date: date | None = None
    sell_price: float | None = None
    sell_shares: int | None = None
    executed_shares: int | None = None
    order_start_date: date | None = None
    executed_date: date | None = None
    profit: float | None = None
    status: str | None = None
    memo: str | None = None


class BenefitIn(BaseModel):
    benefit_value: float | None = None
    benefit_desc: str | None = None
    judgment_on: date | None = None
    judgment_note: str | None = None
    funding_target: bool | None = None


class SettingsIn(BaseModel):
    tax_rate_pct: float
    target_dividend_total: float
    target_yield_buy_pct: float
    target_yield_now_pct: float
    target_yield_value_pct: float
    log_retention_days: int = Field(default=90, ge=1, le=3650)
    otel_enabled: bool = False
    otel_endpoint: str | None = None


class BuyOut(BaseModel):
    id: int
    code: str | None
    name: str | None
    avg_price: float | None
    shares: int | None
    purchase_date: date | None
    policy: str | None
    trigger_note: str | None
    note: str | None
    status: str


class SaleOut(BaseModel):
    id: int
    code: str | None
    name: str | None
    buy_price: float | None
    shares: int | None
    purchase_date: date | None
    sell_price: float | None
    sell_shares: int | None
    executed_shares: int | None
    order_start_date: date | None
    executed_date: date | None
    profit: float | None
    status: str
    memo: str | None


class BenefitOut(BaseModel):
    code: str
    name: str | None
    dividend_forecast: float | None
    dividend_auto: float | None
    benefit_value: float | None
    benefit_desc: str | None
    info_updated_on: date | None
    funding_target: bool | None
    judgment_on: date | None
    judgment_note: str | None


class SettingsOut(BaseModel):
    tax_rate_pct: float
    target_dividend_total: float
    target_monthly_income: int
    target_yield_buy_pct: float
    target_yield_now_pct: float
    target_yield_value_pct: float
    log_retention_days: int
    otel_enabled: bool
    otel_endpoint: str


@app.get("/api/v1/buys", response_model=list[BuyOut], tags=["buys"])
def api_buys_list():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM buy_lots ORDER BY purchase_date DESC NULLS LAST, id DESC"
        ).fetchall()
    return [_buy_out(r) for r in rows]


@app.post("/api/v1/buys", status_code=201, response_model=BuyOut, tags=["buys"])
def api_buys_create(payload: BuyIn):
    with db() as conn:
        row = create_buy(conn, **payload.model_dump())
        conn.commit()
    emit_event("history.buy_created", id=row["id"], code=row["code"],
               status=row["status"])
    return _buy_out(row)


@app.delete("/api/v1/buys/{lot_id}", status_code=204, tags=["buys"])
def api_buys_delete(lot_id: int):
    with db() as conn:
        deleted = delete_buy(conn, lot_id)
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="buy lot not found")
    emit_event("history.buy_deleted", id=lot_id)
    return None


@app.get("/api/v1/sales", response_model=list[SaleOut], tags=["sales"])
def api_sales_list():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sales ORDER BY order_start_date DESC NULLS LAST, id DESC"
        ).fetchall()
    return [_sale_out(r) for r in rows]


@app.post("/api/v1/sales", status_code=201, response_model=SaleOut, tags=["sales"])
def api_sales_create(payload: SaleIn):
    with db() as conn:
        row = create_sale(conn, **payload.model_dump())
        conn.commit()
    attrs = dict(id=row["id"], code=row["code"], status=row["status"])
    if row["profit"] is not None:
        attrs["profit"] = float(row["profit"])
    emit_event("history.sell_created", **attrs)
    return _sale_out(row)


@app.delete("/api/v1/sales/{sale_id}", status_code=204, tags=["sales"])
def api_sales_delete(sale_id: int):
    with db() as conn:
        deleted = delete_sale(conn, sale_id)
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="sale not found")
    emit_event("history.sell_deleted", id=sale_id)
    return None


@app.get("/api/v1/dividend-benefits", response_model=list[BenefitOut], tags=["dividend-benefits"])
def api_benefits_list():
    with db() as conn:
        rows = conn.execute(
            "SELECT d.*, s.name FROM dividend_benefits d"
            " LEFT JOIN stocks s ON s.code = d.code ORDER BY d.code"
        ).fetchall()
    return [_benefit_out(r) for r in rows]


@app.get("/api/v1/dividend-benefits/{code}", response_model=BenefitOut, tags=["dividend-benefits"])
def api_benefits_get(code: str):
    with db() as conn:
        row = conn.execute(
            "SELECT d.*, s.name FROM dividend_benefits d"
            " LEFT JOIN stocks s ON s.code = d.code WHERE d.code = %s",
            (code,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="dividend benefit not found")
    return _benefit_out(row)


@app.put("/api/v1/dividend-benefits/{code}", response_model=BenefitOut, tags=["dividend-benefits"])
def api_benefits_update(code: str, payload: BenefitIn):
    with db() as conn:
        updated = update_benefit(conn, code, **payload.model_dump())
        if not updated:
            raise HTTPException(
                status_code=404, detail="dividend benefit not found"
            )
        conn.commit()
        row = conn.execute(
            "SELECT d.*, s.name FROM dividend_benefits d"
            " LEFT JOIN stocks s ON s.code = d.code WHERE d.code = %s",
            (code,),
        ).fetchone()
    emit_event("master.updated", code=code)
    return _benefit_out(row)


@app.get("/api/v1/settings", response_model=SettingsOut, tags=["settings"])
def api_settings_get():
    with db() as conn:
        view = settings_full_view(conn)
    return view


@app.put("/api/v1/settings", response_model=SettingsOut, tags=["settings"])
def api_settings_update(payload: SettingsIn):
    with db() as conn:
        values = update_settings(conn, **payload.model_dump())
        # A shorter retention applies immediately, not at next startup.
        prune_app_logs(conn, payload.log_retention_days)
        conn.commit()
        view = settings_full_view(conn)
    emit_event("settings.updated", **values)
    # Toggling the OTel integration takes effect without a restart.
    configure_otlp(payload.otel_enabled, payload.otel_endpoint)
    return view


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request):
    with db() as conn:
        runs = conn.execute(
            "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT 3"
        ).fetchall()
        prices = conn.execute(
            """
            SELECT DISTINCT ON (p.code) p.code, s.name, p.price, p.captured_at, p.source
            FROM price_points p LEFT JOIN stocks s ON s.code = p.code
            ORDER BY p.code, p.captured_at DESC
            """
        ).fetchall()
        daily = conn.execute(
            """
            SELECT snap_date, count(*) AS codes FROM daily_snapshots
            GROUP BY snap_date ORDER BY snap_date DESC LIMIT 7
            """
        ).fetchall()
    runs_out = []
    for r in runs:
        d = dict(r)
        note = d.get("note") or ""
        d["kind"] = "自動取得" if note.startswith("batch") else "手動取得"
        if d.get("ok_count") is not None:
            d["result"] = f"成功 {d['ok_count']}件 ／ 失敗 {d['ng_count'] or 0}件"
        elif d.get("finished_at") is not None:
            d["result"] = "中断"
        else:
            d["result"] = "―"
        runs_out.append(d)
    return templates.TemplateResponse(
        request,
        "data.html",
        dict(runs=runs_out, prices=prices, daily=daily, fetching=_fetch_lock.locked()),
    )


@app.get("/api/fetch-status")
def fetch_status():
    with db() as conn:
        run = conn.execute(
            "SELECT id, done_count, total_count, ok_count, ng_count, finished_at"
            " FROM fetch_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if run is None:
        return {"running": False}
    return {
        "running": _fetch_lock.locked(),
        "done": run["done_count"] or 0,
        "total": run["total_count"] or 0,
        "ok": run["ok_count"],
        "ng": run["ng_count"],
        "finished": run["finished_at"] is not None,
    }


@app.post("/fetch")
def fetch_now(background: BackgroundTasks):
    background.add_task(fetch_all, "manual")
    return RedirectResponse("/data", status_code=303)


_SEVERITY_NUMBERS = {"DEBUG": 5, "INFO": 9, "WARNING": 13, "ERROR": 17}


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, severity: str = "", q: str = ""):
    clauses, params = [], []
    if severity in _SEVERITY_NUMBERS:
        clauses.append("severity_number >= %s")
        params.append(_SEVERITY_NUMBERS[severity])
    if q:
        clauses.append("(body ILIKE %s OR attributes::text ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM app_logs{where} ORDER BY id DESC LIMIT 200",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        attrs = d.get("attributes") or {}
        d["attrs_text"] = ", ".join(f"{k}={v}" for k, v in attrs.items())
        d["ts_text"] = r["ts"].strftime("%m-%d %H:%M:%S") if r["ts"] else ""
        out.append(d)
    return templates.TemplateResponse(
        request, "logs.html", dict(rows=out, severity=severity, q=q)
    )


@app.get("/healthz")
def healthz():
    with db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}
