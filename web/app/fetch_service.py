"""夜間バッチと手動取得のパイプライン。

main.py から切り出した取得まわりの実装。fetch_all / _store_quote /
_finish_run / scheduler を置く。振る舞いは変えない（純粋な移動）。

db() と取得ロックは呼び出し側から注入する。手動取得 (/fetch) と
夜間バッチ (scheduler) が同じロックを共有するためで、ロックの
実体は main.py が持つ (_fetch_lock)。db も同じく main.py の
ものを使う。引数を省略した場合はこのモジュールの既定
(db.py の db) を使う。
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta

import httpx

from app.config import JST
from app.db import db as _default_db
from app.observability import emit_event
from app.settings import app_config
from app.yahoo import (
    UA,
    parse_index_page,
    parse_jp_page,
    parse_profile_page,
    suffix_for_market,
)

logger = logging.getLogger("stock-app")


def _store_quote(conn, code, price, fields, snap_date, run_id=None,
                 update_dividend=True):
    """Save one fetched quote: the raw price point, the daily
    snapshot, and (for held stocks only) the dividend master
    auto-update with its change record. Candidates pass
    update_dividend=False so research stocks never enter the
    dividend/benefit master. The caller commits."""
    div = fields.get("1株配当（会社予想）")
    volume = fields.get("出来高")
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
    if div is not None and update_dividend:
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
                (run_id, code, old_row["dividend_forecast"], div),
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


async def fetch_all(note="manual", *, db=_default_db, lock):
    """全銘柄を取得する。lock は必須: 手動取得と夜間バッチが同じ
    ロックを共有するため、呼び出し側 (main.py の _fetch_lock) を
    渡すこと。"""
    if lock.locked():
        return
    async with lock:
        with db() as conn:
            # Targets: held stocks plus purchase candidates (candidates
            # are refreshed too, but never enter the dividend master).
            targets = conn.execute(
                "SELECT code, market FROM ("
                " SELECT DISTINCT h.code, s.market FROM holdings h"
                " JOIN stocks s ON s.code = h.code"
                " UNION"
                " SELECT c.code, s.market FROM candidates c"
                " JOIN stocks s ON s.code = c.code"
                ") ORDER BY code"
            ).fetchall()
            holding_codes = {
                r["code"]
                for r in conn.execute(
                    "SELECT DISTINCT code FROM holdings"
                ).fetchall()
            }
            # Industry changes rarely: fetch the profile page only
            # when the industry is unknown or ~6 months stale, so the
            # extra request does not double every night's batch.
            industry_due = {
                r["code"]
                for r in conn.execute(
                    "SELECT code FROM stocks WHERE industry IS NULL"
                    " OR industry_fetched_on IS NULL"
                    " OR industry_fetched_on < CURRENT_DATE - 180"
                ).fetchall()
            }
            run_id = conn.execute(
                "INSERT INTO fetch_runs(started_at, note, total_count, done_count)"
                " VALUES (now(), %s, %s, 0) RETURNING id",
                (note, len(targets)),
            ).fetchone()["id"]
            conn.commit()
        emit_event("fetch.started", run_id=run_id, kind=note, total=len(targets))
        ok = ng = 0
        first_error = None
        try:
            async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25) as client:
                for t in targets:
                    code = t["code"]
                    # All quotes come from the Yahoo! Finance Japan quote pages
                    # (suffix per exchange: .T/.N/.S/.F). The pages also carry
                    # the company-forecast dividend, which auto-updates the
                    # master.
                    suffix = suffix_for_market(t["market"])
                    try:
                        r = await client.get(
                            f"https://finance.yahoo.co.jp/quote/{code}.{suffix}"
                        )
                        if r.status_code != 200:
                            raise RuntimeError(f"HTTP {r.status_code}")
                        price, fields = parse_jp_page(r.text)
                        if price is None or price <= 0:
                            raise RuntimeError("price not found in page")
                        industry = None
                        if code in industry_due:
                            # A failed profile fetch must not fail the quote.
                            try:
                                pr = await client.get(
                                    f"https://finance.yahoo.co.jp/quote/{code}.{suffix}/profile"
                                )
                                if pr.status_code == 200:
                                    industry = parse_profile_page(pr.text)
                            except Exception:
                                industry = None
                            await asyncio.sleep(random.uniform(3, 8))
                        # The nightly batch reads the previous trading day's close;
                        # JST-9h maps the fetch moment to that trading day.
                        snap_date = (datetime.now(JST) - timedelta(hours=9)).date()
                        with db() as conn:
                            _store_quote(
                                conn, code, price, fields, snap_date,
                                run_id=run_id,
                                update_dividend=code in holding_codes,
                            )
                            if industry:
                                conn.execute(
                                    "UPDATE stocks SET industry = %s,"
                                    " industry_fetched_on = CURRENT_DATE"
                                    " WHERE code = %s",
                                    (industry, code),
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
                    # A progress-update failure is not a per-code failure:
                    # log it and keep going, so a transient DB hiccup
                    # cannot stall the whole batch (same class of bug as
                    # the _finish_run one fixed for run 7).
                    try:
                        with db() as conn:
                            conn.execute(
                                "UPDATE fetch_runs SET done_count = done_count + 1 WHERE id = %s",
                                (run_id,),
                            )
                            conn.commit()
                    except Exception:
                        logger.exception(
                            "fetch run %s: progress update failed for %s",
                            run_id,
                            code,
                        )
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
        finally:
            await _finish_run(run_id, ok, ng, first_error, db=db)
        emit_event("fetch.finished", run_id=run_id, kind=note, ok=ok, ng=ng)


async def _finish_run(run_id, ok, ng, first_error, *, db=_default_db):
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


def next_run_at(now, start_time):
    """start_time ("HH:MM") の次回発生時刻を返す。今日のその時刻
    （秒・マイクロ秒は0）が now より後なら今日、そうでなければ翌日。"""
    hour, minute = (int(part) for part in start_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def scheduler(*, db=_default_db, lock):
    """設定された開始時刻 (fetch_start_time、デフォルト01:00 JST) に
    fetch_all("batch") を実行する。lock は必須 (fetch_all の docstring
    を参照)。待機中は最大60秒ごとに設定を読み直して次回を再計算する
    ので、開始時刻の変更は再起動なしで反映される。設定時刻を過ぎた
    時刻への変更では即時実行せず、次回発生時刻に実行する。"""
    while True:
        try:
            with db() as conn:
                start_time = app_config(conn)["fetch_start_time"]
            now = datetime.now(JST)
            target = next_run_at(now, start_time)
            wait_seconds = min((target - now).total_seconds(), 60)
            await asyncio.sleep(wait_seconds)
            if datetime.now(JST) < target:
                continue
            try:
                await fetch_all("batch", db=db, lock=lock)
            except Exception:
                logger.exception("scheduled fetch failed")
        except Exception:
            # 設定の読み直しは最大60秒ごとに行うため、一時的なDB障害で
            # 例外がループを抜けるとスケジューラが恒常停止してしまう。
            # 記録してバックオフを挟み、次の反復で回復を待つ。
            logger.exception(
                "scheduler iteration failed; retrying in 60 seconds"
            )
            await asyncio.sleep(60)
