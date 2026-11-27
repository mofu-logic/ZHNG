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
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, StringConstraints

from app import fetch_service
from app.config import DATABASE_URL, JST
from app.db import SCHEMA, db, initialize_database
from app.formatting import (
    fnum,
    fmt_pct,
    fmt_pct_signed,
    fmt_pct_value,
    fmt_price,
    fmt_signed,
    fmt_yen,
    register_jinja_filters,
    round_half_up,
)
from app.observability import (
    OtelBridgeHandler,
    configure_otlp,
    emit_event,
    setup_observability,
)
from app.yahoo import (
    fetch_one_quote,
    parse_index_page,
    parse_jp_page,
    parse_profile_page,
    suffix_for_market,
)
from app.services import (
    BENEFIT_MEMO_FIELDS,
    LinkageError,
    buy_linkage_applies,
    cancel_buy,
    cancel_sale,
    candidates_at_target,
    candidate_exists,
    create_buy,
    create_sale,
    delete_buy,
    delete_candidate,
    delete_sale,
    get_benefit_memo_row,
    list_benefit_memo_rows,
    list_candidate_rows,
    register_candidate,
    update_benefit,
    update_candidate,
    upsert_benefit_memo,
    upsert_stock_info,
)
from app.settings import (
    DEFAULT_FETCH_START_TIME,
    FETCH_START_TIME_PATTERN,
    app_config,
    get_settings,
    prune_app_logs,
    settings_full_view,
    update_settings,
)

# formatting.py / yahoo.py / config.py / db.py / settings.py /
# fetch_service.py へ移したもののうち、main.py 内のコードでは直接
# 使わないものもテストが app.main 経由で import しているため、
# ここで再エクスポートする。__all__ に載せることで未使用 import
# として lint に指摘されないようにする。httpx はテストが
# main.httpx.AsyncClient をフェイクに差し替えるための接点で、
# suffix_for_market はテストが市場→サフィックスを直接検証する
# ための接点で、parse_* は Yahoo ページのパースをテストが直接
# 検証するための接点である。
__all__ = [
    "SCHEMA",
    "fmt_pct",
    "fmt_pct_signed",
    "fmt_pct_value",
    "fmt_price",
    "fmt_signed",
    "fmt_yen",
    "httpx",
    "parse_index_page",
    "parse_jp_page",
    "parse_profile_page",
    "round_half_up",
    "suffix_for_market",
]

tracer = setup_observability(DATABASE_URL)

logger = logging.getLogger("stock-app")
if not logger.handlers:
    # Records are bridged into the OpenTelemetry logs pipeline
    # (structured JSON on stdout + the app_logs table). See
    # app/observability.py.
    logger.addHandler(OtelBridgeHandler())
    logger.setLevel(logging.INFO)
    logger.propagate = False


templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
register_jinja_filters(templates.env)


# ---------------------------------------------------------------- price fetch
# 取得パイプラインの実装は app/fetch_service.py にある。main.py には
# 画面・テストが app.main 経由で触る接点だけを残す。
# db() と _fetch_lock は main.py の名前空間で差し替えられる
# (tests/test_fetch.py が main.db / main._fetch_lock を差し替えて
# 取得の失敗・ロックを再現する) ため、fetch_all / _finish_run は
# ここで薄く包み、呼び出し時点の db とロックを fetch_service へ
# 渡す。手動取得 (/fetch) と夜間バッチ (scheduler) が同じロックを
# 共有する、という振る舞いは変わらない。
_fetch_lock = asyncio.Lock()


async def fetch_all(note="manual"):
    """手動・夜間を問わず全銘柄を取得する。実装は fetch_service 側。"""
    return await fetch_service.fetch_all(note, db=db, lock=_fetch_lock)


async def _finish_run(run_id, ok, ng, first_error):
    """取得 run の完了を記録する。実装は fetch_service 側。"""
    return await fetch_service._finish_run(
        run_id, ok, ng, first_error, db=db
    )


@asynccontextmanager
async def lifespan(app):
    with db() as conn:
        stale_runs = initialize_database(conn)
        if stale_runs:
            logger.warning("closed %s stale fetch run(s) at startup", stale_runs)
        # Operation logs are kept for the configured retention period,
        # and the OTel integration follows the stored settings.
        config = app_config(conn)
        prune_app_logs(conn, config["log_retention_days"])
        conn.commit()
    configure_otlp(config["otel_enabled"], config["otel_endpoint"])
    emit_event("app.started")
    task = asyncio.create_task(fetch_service.scheduler(db=db, lock=_fetch_lock))
    yield
    task.cancel()


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,  # served by the gated routes below
    redoc_url=None,
    title="株式ポートフォリオ API",
    version="1.0.0",
    description=(
        "売買履歴・配当優待マスタ・設定の登録／更新／参照を行う "
        "JSON API（/api/v1）。画面と外部連携で同一の IF を使う。"
        "このドキュメントは OpenAPI 仕様として /openapi.json でも取得できる。"
    ),
)

# Shared stylesheet (web/app/static/app.css, DESIGN.md section 9):
# the common CSS lives in one static file instead of an inline
# <style> block in base.html, and pages link it at /static/app.css.
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


def _swagger_enabled():
    with db() as conn:
        return app_config(conn)["swagger_enabled"]


@app.get("/docs", include_in_schema=False)
def swagger_ui():
    """Swagger UI, served only while the developer switch
    (settings: swagger_enabled) is on. /openapi.json itself stays
    available: it is the machine-readable spec for integrations."""
    if not _swagger_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    from fastapi.openapi.docs import get_swagger_ui_html

    return get_swagger_ui_html(
        openapi_url="/openapi.json", title=f"{app.title} - Swagger UI"
    )


@app.get("/redoc", include_in_schema=False)
def redoc_ui():
    if not _swagger_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    from fastapi.openapi.docs import get_redoc_html

    return get_redoc_html(
        openapi_url="/openapi.json", title=f"{app.title} - ReDoc"
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


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db() as conn:
        st = get_settings(conn)
        fetch_start_time = app_config(conn)["fetch_start_time"]
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
                   COALESCE(d.policy,
                     (SELECT policy FROM buy_lots b WHERE b.code = h.code
                      ORDER BY (b.avg_price = h.avg_price AND b.shares = h.shares) DESC,
                               b.purchase_date DESC NULLS LAST, b.id DESC LIMIT 1)) AS policy,
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
        dict(items=items, kpi=kpi, price_at=price_at, policies=policies, accounts=accounts,
             fetch_start_time=fetch_start_time),
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
    # Held stocks only: purchase candidates share the snapshot table
    # but are not part of the portfolio summary.
    with db() as conn:
        holding_codes = {
            r["code"]
            for r in conn.execute("SELECT DISTINCT code FROM holdings").fetchall()
        }
        target_hits = [
            dict(code=r["code"], name=r["name"],
                 target_price=fnum(r["target_price"]),
                 close_price=fnum(r["close_price"]))
            for r in candidates_at_target(conn)
        ]

    movers = []
    for code, s in snaps.items():
        if code == "N225" or code not in holding_codes:
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
            target_hits=target_hits,
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
        linked = {
            (e["source_type"], e["source_id"])
            for e in conn.execute(
                "SELECT source_type, source_id FROM holding_events"
                " WHERE kind IN ('buy', 'sell')"
            ).fetchall()
        }
    # Row state for the history screen: a cancelled trade stays
    # visible as 取消済み, a trade with a linkage event is 反映済み,
    # and anything else never touched holdings （対象外）.
    for rows, source_type in ((buys, "buy_lot"), (sales, "sale")):
        for r in rows:
            if r["cancelled_at"] is not None:
                r["link_state"] = "取消済み"
            elif (source_type, r["id"]) in linked:
                r["link_state"] = "反映済み"
            else:
                r["link_state"] = "対象外"
    return templates.TemplateResponse(request, "history.html", dict(buys=buys, sales=sales))


@app.get("/master", response_class=HTMLResponse)
def master(request: Request):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT d.*, s.name FROM dividend_benefits d
            LEFT JOIN stocks s ON s.code = d.code ORDER BY d.code
            """
        ).fetchall()
    editor = {
        r["code"]: dict(
            benefit_value=fnum(r["benefit_value"]),
            benefit_desc=r["benefit_desc"],
            judgment_on=_date_out(r["judgment_on"]),
            judgment_note=r["judgment_note"],
            funding_target=bool(r["funding_target"]),
            policy=r["policy"],
        )
        for r in rows
    }
    return templates.TemplateResponse(
        request, "master.html", dict(rows=rows, editor=editor)
    )


@app.get("/benefit-memos", response_class=HTMLResponse)
def benefit_memos_page(request: Request):
    with db() as conn:
        rows = [_benefit_memo_out(r) for r in list_benefit_memo_rows(conn)]
    editor = {
        r["code"]: {field: r[field] for field in BENEFIT_MEMO_FIELDS}
        for r in rows
    }
    return templates.TemplateResponse(
        request, "benefit_memos.html", dict(rows=rows, editor=editor)
    )


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
        account_type=r["account_type"], cancelled_at=r["cancelled_at"],
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
        account_type=r["account_type"], cancelled_at=r["cancelled_at"],
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
        policy=r["policy"],
        created_on=_date_out(r["created_on"]),
    )


class BuyIn(BaseModel):
    code: Annotated[str, StringConstraints(strip_whitespace=True,
                                           min_length=1)]
    name: str | None = None
    avg_price: float | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    purchase_date: date | None = None
    policy: str | None = None
    trigger_note: str | None = None
    note: str | None = None
    status: str | None = None
    account_type: str | None = None


class SaleIn(BaseModel):
    code: Annotated[str, StringConstraints(strip_whitespace=True,
                                           min_length=1)]
    name: str | None = None
    buy_price: float | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    purchase_date: date | None = None
    sell_price: float | None = Field(default=None, ge=0)
    sell_shares: int | None = Field(default=None, ge=0)
    executed_shares: int | None = Field(default=None, ge=0)
    order_start_date: date | None = None
    executed_date: date | None = None
    profit: float | None = None
    status: str | None = None
    memo: str | None = None
    account_type: str | None = None


class BenefitIn(BaseModel):
    benefit_value: float | None = None
    benefit_desc: str | None = None
    judgment_on: date | None = None
    judgment_note: str | None = None
    funding_target: bool | None = None
    policy: str | None = None


class SettingsIn(BaseModel):
    tax_rate_pct: float
    target_dividend_total: float
    target_yield_buy_pct: float
    target_yield_now_pct: float
    target_yield_value_pct: float
    log_retention_days: int = Field(default=90, ge=1, le=3650)
    otel_enabled: bool = False
    otel_endpoint: str | None = None
    swagger_enabled: bool = False
    fetch_start_time: str = Field(
        default=DEFAULT_FETCH_START_TIME, pattern=FETCH_START_TIME_PATTERN
    )


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
    account_type: str | None
    cancelled_at: datetime | None


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
    account_type: str | None
    cancelled_at: datetime | None


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
    policy: str | None
    created_on: date | None


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
    swagger_enabled: bool
    fetch_start_time: str


@app.get("/api/v1/buys", response_model=list[BuyOut], tags=["buys"])
def api_buys_list():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM buy_lots ORDER BY purchase_date DESC NULLS LAST, id DESC"
        ).fetchall()
    return [_buy_out(r) for r in rows]


@app.post("/api/v1/buys", status_code=201, response_model=BuyOut, tags=["buys"])
def api_buys_create(payload: BuyIn):
    fetched = None
    if payload.name is None and buy_linkage_applies(
        account_type=payload.account_type, status=payload.status,
        shares=payload.shares, avg_price=payload.avg_price,
    ):
        # A linked buy for a code the stock master does not know yet
        # needs the master row first: fetch it from Yahoo, like
        # purchase-candidate registration, and refuse the buy itself
        # when the fetch fails (nothing is registered then). When a
        # name is supplied, the existing behavior stands (the master
        # row is created from the supplied name, without a market).
        with db() as conn:
            known = conn.execute(
                "SELECT 1 FROM stocks WHERE code = %s", (payload.code,)
            ).fetchone()
        if known is None:
            try:
                market, name, price, fields = asyncio.run(
                    fetch_one_quote(payload.code)
                )
            except RuntimeError as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"銘柄情報を取得できませんでした: {e}",
                )
            fetched = (name or payload.code, market)
    with db() as conn:
        if fetched is not None:
            upsert_stock_info(conn, payload.code, fetched[0], fetched[1])
        row = create_buy(conn, **payload.model_dump())
        conn.commit()
    emit_event("history.buy_created", id=row["id"], code=row["code"],
               status=row["status"])
    return _buy_out(row)


@app.post("/api/v1/buys/{lot_id}/cancel", response_model=BuyOut, tags=["buys"])
def api_buys_cancel(lot_id: int):
    with db() as conn:
        outcome, row = cancel_buy(conn, lot_id)
        if outcome == "ok":
            conn.commit()
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="buy lot not found")
    if outcome == "already":
        raise HTTPException(status_code=409, detail="すでに取り消されています")
    if outcome == "blocked":
        raise HTTPException(
            status_code=409,
            detail="この履歴より後の保有変更があるため、取り消せません",
        )
    emit_event("history.buy_cancelled", id=lot_id, code=row["code"])
    return _buy_out(row)


@app.delete("/api/v1/buys/{lot_id}", status_code=204, tags=["buys"])
def api_buys_delete(lot_id: int):
    with db() as conn:
        outcome = delete_buy(conn, lot_id)
        if outcome == "deleted":
            conn.commit()
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="buy lot not found")
    if outcome == "blocked":
        raise HTTPException(
            status_code=409,
            detail="この履歴より後の保有変更があるため、削除できません",
        )
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
    try:
        with db() as conn:
            row = create_sale(conn, **payload.model_dump())
            conn.commit()
    except LinkageError as e:
        # The executed shares exceed the holding for the named
        # account: the sale itself is not registered either.
        raise HTTPException(status_code=422, detail=str(e))
    attrs = dict(id=row["id"], code=row["code"], status=row["status"])
    if row["profit"] is not None:
        attrs["profit"] = float(row["profit"])
    emit_event("history.sell_created", **attrs)
    return _sale_out(row)


@app.post("/api/v1/sales/{sale_id}/cancel", response_model=SaleOut, tags=["sales"])
def api_sales_cancel(sale_id: int):
    with db() as conn:
        outcome, row = cancel_sale(conn, sale_id)
        if outcome == "ok":
            conn.commit()
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="sale not found")
    if outcome == "already":
        raise HTTPException(status_code=409, detail="すでに取り消されています")
    if outcome == "blocked":
        raise HTTPException(
            status_code=409,
            detail="この履歴より後の保有変更があるため、取り消せません",
        )
    emit_event("history.sell_cancelled", id=sale_id, code=row["code"])
    return _sale_out(row)


@app.delete("/api/v1/sales/{sale_id}", status_code=204, tags=["sales"])
def api_sales_delete(sale_id: int):
    with db() as conn:
        outcome = delete_sale(conn, sale_id)
        if outcome == "deleted":
            conn.commit()
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="sale not found")
    if outcome == "blocked":
        raise HTTPException(
            status_code=409,
            detail="この履歴より後の保有変更があるため、削除できません",
        )
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
        updated = update_benefit(conn, code, **payload.model_dump(exclude_unset=True))
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


def _benefit_memo_out(r):
    return dict(
        code=r["code"], name=r.get("name"),
        shareholder_no=r["shareholder_no"],
        deadline_1=_date_out(r["deadline_1"]),
        deadline_2=_date_out(r["deadline_2"]),
        delivery_waiting=bool(r["delivery_waiting"]),
        status=r["status"],
        form_arrival_1=_date_out(r["form_arrival_1"]),
        form_arrival_2=_date_out(r["form_arrival_2"]),
        item_arrival_1=_date_out(r["item_arrival_1"]),
        item_arrival_2=_date_out(r["item_arrival_2"]),
    )


class BenefitMemoIn(BaseModel):
    shareholder_no: str | None = None
    deadline_1: date | None = None
    deadline_2: date | None = None
    delivery_waiting: bool | None = None
    status: str | None = None
    form_arrival_1: date | None = None
    form_arrival_2: date | None = None
    item_arrival_1: date | None = None
    item_arrival_2: date | None = None


class BenefitMemoOut(BaseModel):
    code: str
    name: str | None
    shareholder_no: str | None
    deadline_1: date | None
    deadline_2: date | None
    delivery_waiting: bool
    status: str | None
    form_arrival_1: date | None
    form_arrival_2: date | None
    item_arrival_1: date | None
    item_arrival_2: date | None


@app.get("/api/v1/benefit-memos", response_model=list[BenefitMemoOut], tags=["benefit-memos"])
def api_benefit_memos_list():
    with db() as conn:
        rows = list_benefit_memo_rows(conn)
    return [_benefit_memo_out(r) for r in rows]


@app.put("/api/v1/benefit-memos/{code}", response_model=BenefitMemoOut, tags=["benefit-memos"])
def api_benefit_memos_update(code: str, payload: BenefitMemoIn):
    code = code.strip().upper()
    with db() as conn:
        updated = upsert_benefit_memo(
            conn, code, **payload.model_dump(exclude_unset=True)
        )
        if not updated:
            raise HTTPException(
                status_code=404, detail="銘柄が見つかりません"
            )
        conn.commit()
        row = get_benefit_memo_row(conn, code)
    emit_event("benefit_memo.updated", code=code)
    return _benefit_memo_out(row)


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


def _candidate_out(r):
    close = fnum(r["close_price"])
    prev = fnum(r["prev_close"])
    change = change_pct = None
    if close is not None and prev:
        change = close - prev
        change_pct = change / prev * 100
    target = fnum(r["target_price"])
    return dict(
        code=r["code"], name=r["name"],
        added_at=r["added_at"].isoformat() if r["added_at"] else None,
        snap_date=_date_out(r["snap_date"]),
        close_price=close, prev_close=prev,
        change=change, change_pct=change_pct,
        dividend_forecast=fnum(r["dividend_forecast"]),
        dividend_yield=fnum(r["dividend_yield"]),
        per=fnum(r["per"]), pbr=fnum(r["pbr"]),
        policy=r["policy"], memo=r["memo"], target_price=target,
        reached=(target is not None and close is not None
                 and close <= target),
    )


class CandidateIn(BaseModel):
    code: str = Field(min_length=4, max_length=5, pattern=r"^[0-9A-Za-z]+$")


class CandidateOut(BaseModel):
    code: str
    name: str | None
    added_at: str | None
    snap_date: date | None
    close_price: float | None
    prev_close: float | None
    change: float | None
    change_pct: float | None
    dividend_forecast: float | None
    dividend_yield: float | None
    per: float | None
    pbr: float | None
    policy: str | None
    memo: str | None
    target_price: float | None
    reached: bool


class CandidateUpdateIn(BaseModel):
    policy: str | None = None
    memo: str | None = None
    target_price: float | None = Field(default=None, ge=0)


@app.get("/candidates", response_class=HTMLResponse)
def candidates_page(request: Request):
    with db() as conn:
        rows = [_candidate_out(r) for r in list_candidate_rows(conn)]
    editor = {
        r["code"]: dict(policy=r["policy"], memo=r["memo"],
                        target_price=r["target_price"])
        for r in rows
    }
    return templates.TemplateResponse(
        request, "candidates.html",
        dict(rows=rows, editor=editor),
    )


@app.put("/api/v1/candidates/{code}", response_model=CandidateOut, tags=["candidates"])
def api_candidates_update(code: str, payload: CandidateUpdateIn):
    code = code.strip().upper()
    with db() as conn:
        updated = update_candidate(
            conn, code, **payload.model_dump(exclude_unset=True)
        )
        conn.commit()
        if not updated:
            raise HTTPException(
                status_code=404, detail="購入候補に登録されていません"
            )
        row = [r for r in list_candidate_rows(conn) if r["code"] == code][0]
    emit_event("candidate.updated", code=code)
    return _candidate_out(row)


@app.get("/api/v1/candidates", response_model=list[CandidateOut], tags=["candidates"])
def api_candidates_list():
    with db() as conn:
        rows = list_candidate_rows(conn)
    return [_candidate_out(r) for r in rows]


class CandidatePreviewOut(BaseModel):
    code: str
    name: str | None
    close_price: float | None


@app.get("/api/v1/candidates/preview/{code}", response_model=CandidatePreviewOut, tags=["candidates"])
def api_candidates_preview(code: str):
    """Fetch a quote without registering it (the UI shows the stock
    name next to the register button so a mistyped code is caught
    before registration)."""
    code = code.strip().upper()
    if not re.fullmatch(r"[0-9A-Za-z]{4,5}", code):
        raise HTTPException(
            status_code=422, detail="銘柄コードの形式が正しくありません"
        )
    try:
        market, name, price, fields = asyncio.run(fetch_one_quote(code))
    except RuntimeError:
        raise HTTPException(status_code=404, detail="銘柄が見つかりませんでした")
    return dict(code=code, name=name, close_price=price)


@app.post("/api/v1/candidates", status_code=201, response_model=CandidateOut, tags=["candidates"])
def api_candidates_create(payload: CandidateIn):
    code = payload.code.strip().upper()
    with db() as conn:
        if candidate_exists(conn, code):
            raise HTTPException(
                status_code=409,
                detail="この銘柄はすでに購入候補に登録されています",
            )
    # Registration fetches the quote right away; a code that cannot be
    # fetched at all is not registered.
    try:
        market, name, price, fields = asyncio.run(fetch_one_quote(code))
    except RuntimeError as e:
        raise HTTPException(
            status_code=502, detail=f"銘柄情報を取得できませんでした: {e}"
        )
    if not name:
        name = code  # title parse missed; stocks.name is NOT NULL
    snap_date = (datetime.now(JST) - timedelta(hours=9)).date()
    with db() as conn:
        register_candidate(conn, code, market, name)
        fetch_service._store_quote(conn, code, price, fields, snap_date, update_dividend=False)
        conn.commit()
        row = [r for r in list_candidate_rows(conn) if r["code"] == code][0]
    emit_event("candidate.added", code=code, name=name)
    return _candidate_out(row)


@app.delete("/api/v1/candidates/{code}", status_code=204, tags=["candidates"])
def api_candidates_delete(code: str):
    code = code.strip().upper()
    with db() as conn:
        deleted = delete_candidate(conn, code)
        conn.commit()
    if not deleted:
        raise HTTPException(
            status_code=404, detail="購入候補に登録されていません"
        )
    emit_event("candidate.deleted", code=code)
    return None


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
            GROUP BY snap_date ORDER BY snap_date DESC LIMIT 3
            """
        ).fetchall()
        fetch_start_time = app_config(conn)["fetch_start_time"]
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
        dict(
            runs=runs_out, prices=prices, daily=daily,
            fetching=_fetch_lock.locked(), fetch_start_time=fetch_start_time,
        ),
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
