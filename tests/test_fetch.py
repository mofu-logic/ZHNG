"""The nightly fetch pipeline, with Yahoo replaced by a fake client.

fetch_all is the app's most consequential code path (it rewrites
prices, snapshots and the dividend master). These tests run it end to
end against fixture pages: success for all codes, a per-code failure,
the dividend-change recording, the Nikkei snapshot, and the lock that
prevents overlapping runs.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from test_parse import INDEX_PAGE, JP_PAGE, JP_PAGE_DECIMAL


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def make_fake_client(fail_codes=(), empty_codes=(), index_mode="ok"):
    class FakeAsyncClient:
        def __init__(self, headers=None, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if "998407" in url:
                if index_mode == "http404":
                    return FakeResponse(404, "not found")
                if index_mode == "noprice":
                    return FakeResponse(200, "<html><body></body></html>")
                return FakeResponse(200, INDEX_PAGE)
            for code in fail_codes:
                if f"/{code}." in url:
                    return FakeResponse(404, "not found")
            for code in empty_codes:
                if f"/{code}." in url:
                    # 200 but no price board: the parse yields no price.
                    return FakeResponse(200, "<html><body></body></html>")
            return FakeResponse(200, JP_PAGE)

    return FakeAsyncClient


@pytest.fixture()
def fake_fetch(monkeypatch):
    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    main._fetch_lock = asyncio.Lock()

    def install(**kwargs):
        monkeypatch.setattr(
            main.httpx, "AsyncClient", make_fake_client(**kwargs)
        )

    return install


def _seed_two_stocks(db):
    for code, market in [("9999", "東証"), ("8888", "名証")]:
        db.execute(
            "INSERT INTO stocks(code, name, market) VALUES (%s, %s, %s)",
            (code, f"銘柄{code}", market),
        )
        db.execute(
            "INSERT INTO holdings(code, account_type, avg_price, shares)"
            " VALUES (%s, '特定', 800, 100)",
            (code,),
        )
    db.execute(
        "INSERT INTO dividend_benefits(code, dividend_forecast, dividend_auto)"
        " VALUES ('9999', 40, 40)"
    )
    db.commit()


def _snap_date_today():
    return (datetime.now(main.JST) - timedelta(hours=9)).date()


def test_fetch_all_success(db, fake_fetch):
    _seed_two_stocks(db)
    fake_fetch()
    asyncio.run(main.fetch_all("batch"))

    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["finished_at"] is not None
    assert run["ok_count"] == 2
    assert run["ng_count"] == 0
    assert run["done_count"] == 2

    prices = db.execute(
        "SELECT code, price FROM price_points ORDER BY code"
    ).fetchall()
    assert [(p["code"], float(p["price"])) for p in prices] == [
        ("8888", 1000.0),
        ("9999", 1000.0),
    ]

    snaps = db.execute(
        "SELECT code FROM daily_snapshots WHERE snap_date = %s ORDER BY code",
        (_snap_date_today(),),
    ).fetchall()
    assert [s["code"] for s in snaps] == ["8888", "9999", "N225"]

    # The page's company-forecast dividend (50) replaced the stored 40,
    # and the change was recorded against this run.
    row = db.execute(
        "SELECT * FROM dividend_benefits WHERE code = '9999'"
    ).fetchone()
    assert float(row["dividend_forecast"]) == 50.0
    changes = db.execute("SELECT * FROM dividend_changes").fetchall()
    assert len(changes) == 1
    assert changes[0]["code"] == "9999"
    assert float(changes[0]["old_div"]) == 40.0
    assert float(changes[0]["new_div"]) == 50.0


def test_fetch_all_partial_failure(db, fake_fetch):
    _seed_two_stocks(db)
    fake_fetch(fail_codes=("8888",))
    asyncio.run(main.fetch_all("batch"))

    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["finished_at"] is not None
    assert run["ok_count"] == 1
    assert run["ng_count"] == 1
    assert "HTTP 404" in run["note"]  # first error is kept on the run


def test_fetch_all_price_not_found_in_page(db, fake_fetch):
    _seed_two_stocks(db)
    fake_fetch(empty_codes=("8888",))
    asyncio.run(main.fetch_all("batch"))

    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["ok_count"] == 1
    assert run["ng_count"] == 1
    assert "price not found in page" in run["note"]


@pytest.mark.parametrize("index_mode", ["http404", "noprice"])
def test_fetch_all_nikkei_failure_does_not_affect_stocks(
    db, fake_fetch, index_mode
):
    _seed_two_stocks(db)
    fake_fetch(index_mode=index_mode)
    asyncio.run(main.fetch_all("batch"))

    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["finished_at"] is not None
    assert run["ok_count"] == 2
    assert run["ng_count"] == 0
    codes = db.execute(
        "SELECT code FROM daily_snapshots ORDER BY code"
    ).fetchall()
    assert [c["code"] for c in codes] == ["8888", "9999"]  # no N225 row


def test_fetch_now_endpoint_schedules_manual_fetch(client, monkeypatch):
    calls = []

    async def fake_fetch_all(note="manual"):
        calls.append(note)

    monkeypatch.setattr(main, "fetch_all", fake_fetch_all)
    r = client.post("/fetch", follow_redirects=False)
    assert r.status_code == 303
    assert calls == ["manual"]


def test_fetch_all_skips_when_locked(db, fake_fetch):
    _seed_two_stocks(db)
    fake_fetch()

    async def held():
        lock = asyncio.Lock()
        await lock.acquire()
        main._fetch_lock = lock
        await main.fetch_all("manual")

    asyncio.run(held())
    assert db.execute("SELECT count(*) c FROM fetch_runs").fetchone()["c"] == 0


def test_finish_run_records_error_note(db):
    run_id = db.execute(
        "INSERT INTO fetch_runs(started_at, note) VALUES (now(), 'batch')"
        " RETURNING id"
    ).fetchone()["id"]
    db.commit()
    asyncio.run(main._finish_run(run_id, 3, 1, "Boom"))
    run = db.execute(
        "SELECT * FROM fetch_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert run["finished_at"] is not None
    assert run["ok_count"] == 3
    assert run["ng_count"] == 1
    assert run["note"] == "batch / Boom"


def test_finish_run_without_error_keeps_note(db):
    # Regression: a clean run (first_error=None) must still record its
    # completion. The un-cast `CASE WHEN %s IS NULL` made PostgreSQL
    # reject the parameter type and the run stayed "running" forever.
    run_id = db.execute(
        "INSERT INTO fetch_runs(started_at, note) VALUES (now(), 'batch')"
        " RETURNING id"
    ).fetchone()["id"]
    db.commit()
    asyncio.run(main._finish_run(run_id, 63, 0, None))
    run = db.execute(
        "SELECT * FROM fetch_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert run["finished_at"] is not None
    assert run["ok_count"] == 63
    assert run["note"] == "batch"


def test_finish_run_gives_up_after_retries(db, monkeypatch):
    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    def broken_db():
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "db", broken_db)
    # Must not raise: it logs and gives up after 3 attempts.
    asyncio.run(main._finish_run(999, 1, 0, None))


def test_lifespan_closes_stale_runs(db):
    db.execute(
        "INSERT INTO fetch_runs(started_at, note) VALUES (now(), 'batch')"
    )
    db.commit()
    with TestClient(main.app) as client:
        assert client.get("/healthz").status_code == 200
    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["finished_at"] is not None
    assert "interrupted (closed at startup)" in run["note"]


class SuffixFakeClient:
    """Serves the JP page only for URLs ending in one given suffix."""

    def __init__(self, ok_suffix, headers=None, timeout=None):
        self.ok_suffix = ok_suffix

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if url.endswith("." + self.ok_suffix):
            return FakeResponse(200, JP_PAGE)
        return FakeResponse(404, "not found")


def test_suffix_for_market():
    assert main.suffix_for_market("東証") == "T"
    assert main.suffix_for_market("名証") == "N"
    assert main.suffix_for_market("札証") == "S"
    assert main.suffix_for_market("福証") == "F"
    assert main.suffix_for_market(None) == "T"
    assert main.suffix_for_market("不明") == "T"


def test_fetch_one_quote_falls_back_to_sapporo(monkeypatch):
    monkeypatch.setattr(
        main.httpx, "AsyncClient",
        lambda **kw: SuffixFakeClient("S", **kw),
    )
    market, name, price, fields = asyncio.run(main.fetch_one_quote("2928"))
    assert market == "札証"
    assert price == 1000.0


def test_fetch_one_quote_falls_back_to_fukuoka(monkeypatch):
    monkeypatch.setattr(
        main.httpx, "AsyncClient",
        lambda **kw: SuffixFakeClient("F", **kw),
    )
    market, name, price, fields = asyncio.run(main.fetch_one_quote("1771"))
    assert market == "福証"
    assert price == 1000.0


def test_fetch_stores_industry_once(fake_fetch, db, monkeypatch):
    from test_parse import PROFILE_PAGE

    hits = {"profile": 0}

    class ProfileClient:
        def __init__(self, headers=None, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if "998407" in url:
                from test_parse import INDEX_PAGE

                return FakeResponse(200, INDEX_PAGE)
            if url.endswith("/profile"):
                hits["profile"] += 1
                return FakeResponse(200, PROFILE_PAGE)
            return FakeResponse(200, JP_PAGE)

    monkeypatch.setattr(main.httpx, "AsyncClient", ProfileClient)
    _seed_two_stocks(db)
    db.commit()
    asyncio.run(main.fetch_all())
    rows = db.execute(
        "SELECT code, industry, industry_fetched_on FROM stocks ORDER BY code"
    ).fetchall()
    assert {r["industry"] for r in rows} == {"銀行業"}
    assert all(r["industry_fetched_on"] is not None for r in rows)
    assert hits["profile"] == 2  # one per code
    # Fresh industries are not refetched on the next run.
    asyncio.run(main.fetch_all())
    assert hits["profile"] == 2


class _ProgressFailingConnection:
    """Wraps a real connection; only the done_count progress UPDATE fails.

    The per-code quote store, the industry update and _finish_run all go
    through main.db too, so the failure is keyed on the progress UPDATE
    text itself. This simulates a transient DB failure hitting exactly
    the progress update that lives outside the per-code try in fetch_all.
    """

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def execute(self, query, params=None, **kwargs):
        if isinstance(query, str) and "done_count = done_count + 1" in query:
            raise RuntimeError("progress update failed (simulated DB error)")
        return self._conn.execute(query, params, **kwargs)

    def commit(self):
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _progress_failing_db(real_db):
    def failing_db():
        return _ProgressFailingConnection(real_db())

    return failing_db


def test_fetch_all_progress_update_failure_does_not_stop_batch(
    db, fake_fetch, monkeypatch
):
    # R1: the done_count progress UPDATE sits outside the per-code try,
    # so a DB failure there used to escape fetch_all, skipping the
    # remaining codes and _finish_run (the run stayed "running").
    # A progress-update failure is not a per-code failure: both quotes
    # were fetched and stored, so ok must stay 2 and ng 0.
    _seed_two_stocks(db)
    fake_fetch()
    monkeypatch.setattr(main, "db", _progress_failing_db(main.db))
    try:
        asyncio.run(main.fetch_all("batch"))
    except RuntimeError:
        pass  # the error itself may escape; the run must still finish
    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["finished_at"] is not None
    assert run["ok_count"] == 2
    assert run["ng_count"] == 0
    prices = db.execute(
        "SELECT code FROM price_points ORDER BY code"
    ).fetchall()
    assert [p["code"] for p in prices] == ["8888", "9999"]


def test_fetch_all_unexpected_loop_error_still_finishes_run(
    db, fake_fetch, monkeypatch
):
    # R1: even an unexpected error escaping the per-code try (here the
    # per-code pacing sleep, 25-45s, which sits outside the try) must
    # not leave the run "running": _finish_run belongs in a finally.
    # The industry sleep (3-8s) and _finish_run retries (2s) are left
    # alone so only the pacing sleep fails.
    _seed_two_stocks(db)
    fake_fetch()

    async def pacing_sleep(delay, *a, **k):
        if delay > 20:
            raise RuntimeError("pacing sleep failed (simulated)")
        return None

    monkeypatch.setattr(asyncio, "sleep", pacing_sleep)
    try:
        asyncio.run(main.fetch_all("batch"))
    except RuntimeError:
        pass  # the error itself may still propagate; the run must finish
    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["finished_at"] is not None
    assert run["ok_count"] == 1  # 8888 stored before the pacing sleep
    assert run["ng_count"] == 0


# --- Plausibility guard (2026-11-27) -----------------------------------
# The parser once stored the volume (or the previous close) as the
# close for prices with a fractional part. Independently of the parse
# fix, fetch_all must refuse to store an implausible quote and count
# the code as failed instead. The pages below are built so that only
# one guard condition trips in each test.


def _quote_page(price, prev_close, open_price, day_high, day_low, volume):
    """A quote page in the real structure (_CommonPriceBoard__price_
    board plus a data list). Values are the literal page texts;
    None renders as "---" (the field is then absent from the parse)."""

    def item(name, value):
        body = (
            f'<span class="_StyledNumber__value_2">{value}</span>'
            if value is not None
            else "---"
        )
        return (
            '<dt class="_DataListItem__term_a">'
            f'<span class="_DataListItem__name_b">{name}</span></dt>'
            f"<dd>{body}</dd>"
        )

    return (
        "<html><body>"
        '<div class="_PriceBoardMenu__menu_a">メニュー</div>'
        '<div class="_CommonPriceBoard__price_b">'
        f'<span class="_StyledNumber__value_1">{price}</span></div>'
        "<dl>"
        + item("前日終値", prev_close)
        + item("始値", open_price)
        + item("高値", day_high)
        + item("安値", day_low)
        + item("出来高", volume)
        + "</dl></body></html>"
    )


class _PageMapClient:
    """Serves a fixed page per stock code (INDEX_PAGE for the Nikkei)."""

    def __init__(self, pages, headers=None, timeout=None):
        self.pages = pages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if "998407" in url:
            return FakeResponse(200, INDEX_PAGE)
        for code, page in self.pages.items():
            if f"/{code}." in url:
                return FakeResponse(200, page)
        return FakeResponse(200, JP_PAGE)


def _install_pages(monkeypatch, pages):
    monkeypatch.setattr(
        main.httpx, "AsyncClient", lambda **kw: _PageMapClient(pages, **kw)
    )


def _assert_code_rejected(db):
    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["finished_at"] is not None
    assert run["ok_count"] == 1
    assert run["ng_count"] == 1
    prices = db.execute(
        "SELECT code FROM price_points ORDER BY code"
    ).fetchall()
    assert [p["code"] for p in prices] == ["9999"]
    snaps = db.execute(
        "SELECT code FROM daily_snapshots WHERE snap_date = %s ORDER BY code",
        (_snap_date_today(),),
    ).fetchall()
    assert [s["code"] for s in snaps] == ["9999", "N225"]


def test_fetch_all_rejects_price_equal_to_volume(db, fake_fetch, monkeypatch):
    _seed_two_stocks(db)
    _install_pages(
        monkeypatch,
        {
            "8888": _quote_page(
                "123,456", "120,000", "121,000", "125,000", "119,000", "123,456"
            )
        },
    )
    asyncio.run(main.fetch_all("batch"))
    _assert_code_rejected(db)


def test_fetch_all_rejects_price_outside_day_range(
    db, fake_fetch, monkeypatch
):
    # The stored 3941 case: the "close" (1,477) sat below the day low
    # (1,480) because it was really the previous close.
    _seed_two_stocks(db)
    _install_pages(
        monkeypatch,
        {"8888": _quote_page("1,477", "1,477", "1,500", "1,513", "1,480", "957,600")},
    )
    asyncio.run(main.fetch_all("batch"))
    _assert_code_rejected(db)


def test_fetch_all_rejects_quote_without_ohlc_and_volume(
    db, fake_fetch, monkeypatch
):
    # No trades today: a price-like number is on the page but open /
    # high / low / volume are all "---". Nothing may be stored.
    _seed_two_stocks(db)
    _install_pages(
        monkeypatch,
        {"8888": _quote_page("3,029", "3,029", None, None, None, None)},
    )
    asyncio.run(main.fetch_all("batch"))
    _assert_code_rejected(db)


def test_fetch_all_rejects_implausible_daily_move(
    db, fake_fetch, monkeypatch
):
    # +102% against the previous close is beyond any daily price
    # limit; such a jump means the parsed "price" is not the price.
    _seed_two_stocks(db)
    _install_pages(
        monkeypatch,
        {"8888": _quote_page("2,000", "990", "1,995", "2,010", "1,985", "123,456")},
    )
    asyncio.run(main.fetch_all("batch"))
    _assert_code_rejected(db)


def test_fetch_all_stores_decimal_close_price(db, fake_fetch, monkeypatch):
    # The guard must not misfire on a genuine decimal close: 830.4 is
    # stored as 830.4 (not as the volume, not rejected).
    _seed_two_stocks(db)
    _install_pages(monkeypatch, {"8888": JP_PAGE_DECIMAL})
    asyncio.run(main.fetch_all("batch"))

    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["ok_count"] == 2
    assert run["ng_count"] == 0

    price = db.execute(
        "SELECT price FROM price_points WHERE code = '8888'"
    ).fetchone()
    assert float(price["price"]) == 830.4

    snap = db.execute(
        "SELECT * FROM daily_snapshots"
        " WHERE code = '8888' AND snap_date = %s",
        (_snap_date_today(),),
    ).fetchone()
    assert float(snap["close_price"]) == 830.4
    assert float(snap["prev_close"]) == 825.3
    assert snap["volume"] == 1234500


# --- Daily snapshot upsert: both write paths ----------------------------
# daily_snapshots is written from two places: _store_quote for regular
# stocks (fetch_all and candidate registration) and an inline upsert
# for the Nikkei (code N225). The tests below pin, column by column,
# what each path stores today, plus the last-write-wins overwrite on a
# same-day refetch, so consolidating the two upserts cannot drift
# either path's output.

_JP_SNAPSHOT = {
    "close_price": 1000.0,
    "prev_close": 990.0,
    "open_price": 995.0,
    "day_high": 1010.0,
    "day_low": 985.0,
    "volume": 123456,
    "market_cap_m": 1234567.0,
    "dividend_forecast": 50.0,
    "dividend_yield": 5.0,
    "per": 11.17,
    "pbr": 1.23,
}

# JP_PAGE with every field changed; the anchors are the fixture's
# literal values, each unique in the page.
JP_PAGE_V2 = (
    JP_PAGE.replace(">1,000<", ">1,100<")
    .replace(">990<", ">1,050<")
    .replace(">995<", ">1,060<")
    .replace(">1,010<", ">1,120<")
    .replace(">985<", ">1,040<")
    .replace(">123,456<", ">234,567<")
    .replace(">1,234,567<", ">2,345,678<")
    .replace(">50<", ">60<")
    .replace(">5.00<", ">5.45<")
    .replace("11.17", "12.50")
    .replace(">1.23<", ">1.45<")
)

_JP_SNAPSHOT_V2 = {
    "close_price": 1100.0,
    "prev_close": 1050.0,
    "open_price": 1060.0,
    "day_high": 1120.0,
    "day_low": 1040.0,
    "volume": 234567,
    "market_cap_m": 2345678.0,
    "dividend_forecast": 60.0,
    "dividend_yield": 5.45,
    "per": 12.5,
    "pbr": 1.45,
}

# The INDEX_PAGE fixture carries only the previous close; every other
# value column of the N225 row is NULL.
_N225_SNAPSHOT = {
    "close_price": 58644.63,
    "prev_close": 57011.62,
    "open_price": None,
    "day_high": None,
    "day_low": None,
    "volume": None,
    "market_cap_m": None,
    "dividend_forecast": None,
    "dividend_yield": None,
    "per": None,
    "pbr": None,
}


def _assert_snapshot(row, expected):
    assert row is not None
    for column, want in expected.items():
        got = row[column]
        if want is None:
            assert got is None, column
        elif column == "volume":
            assert got == want, column
        else:
            assert float(got) == want, column


def _snapshot_of(db, code):
    return db.execute(
        "SELECT * FROM daily_snapshots WHERE code = %s AND snap_date = %s",
        (code, _snap_date_today()),
    ).fetchone()


def test_fetch_all_snapshot_stores_all_columns(db, fake_fetch):
    _seed_two_stocks(db)
    fake_fetch()
    asyncio.run(main.fetch_all("batch"))

    for code in ("8888", "9999"):
        _assert_snapshot(_snapshot_of(db, code), _JP_SNAPSHOT)
    _assert_snapshot(_snapshot_of(db, "N225"), _N225_SNAPSHOT)


class _SnapshotClient:
    """Serves one fixed page for every stock and one for the Nikkei."""

    def __init__(self, stock_page, index_page, headers=None, timeout=None):
        self.stock_page = stock_page
        self.index_page = index_page

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if "998407" in url:
            return FakeResponse(200, self.index_page)
        return FakeResponse(200, self.stock_page)


def _install_snapshot_client(monkeypatch, stock_page, index_page):
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **kw: _SnapshotClient(stock_page, index_page, **kw),
    )


def test_fetch_all_snapshot_last_write_wins_for_stocks(
    db, fake_fetch, monkeypatch
):
    _seed_two_stocks(db)
    fake_fetch()
    asyncio.run(main.fetch_all("batch"))
    _assert_snapshot(_snapshot_of(db, "9999"), _JP_SNAPSHOT)

    # A same-day refetch overwrites every value column of the existing
    # snapshot row instead of adding a second one; the raw price
    # points, by contrast, are append-only.
    _install_snapshot_client(monkeypatch, JP_PAGE_V2, INDEX_PAGE)
    asyncio.run(main.fetch_all("batch"))

    for code in ("8888", "9999"):
        rows = db.execute(
            "SELECT * FROM daily_snapshots WHERE code = %s", (code,)
        ).fetchall()
        assert len(rows) == 1
        _assert_snapshot(rows[0], _JP_SNAPSHOT_V2)
    points = db.execute(
        "SELECT count(*) c FROM price_points WHERE code = '9999'"
    ).fetchone()
    assert points["c"] == 2
    # The index row kept its own values.
    _assert_snapshot(_snapshot_of(db, "N225"), _N225_SNAPSHOT)


def _index_page(price, prev_close, open_price, day_high, day_low, volume):
    """A Nikkei page in the INDEX_PAGE structure (common price board
    plus a data list) with a full field set. Values are the literal
    page texts; None renders as "---" (absent from the parse)."""

    def item(name, value):
        body = (
            f'<span class="_StyledNumber__value_2">{value}</span>'
            if value is not None
            else "---"
        )
        return (
            '<dt class="_DataListItem__term_a">'
            f'<span class="_DataListItem__name_b">{name}</span></dt>'
            f"<dd>{body}</dd>"
        )

    return (
        "<html><body>"
        '<div class="_CommonPriceBoard__price_y">'
        f'<span class="_StyledNumber__value_9">{price}</span></div>'
        "<dl>"
        + item("前日終値", prev_close)
        + item("始値", open_price)
        + item("高値", day_high)
        + item("安値", day_low)
        + item("出来高", volume)
        + "</dl></body></html>"
    )


def test_fetch_all_snapshot_nikkei_columns_and_last_write_wins(
    db, fake_fetch, monkeypatch
):
    _seed_two_stocks(db)
    fake_fetch()  # patches sleep and the lock; client replaced below

    # The index path stores the OHLC values it parsed, but the other
    # value columns stay NULL even though the page carries a volume:
    # the N225 upsert writes NULL for them literally.
    _install_snapshot_client(
        monkeypatch,
        JP_PAGE,
        _index_page(
            "58,644.63", "57,011.62", "57,500", "58,900", "57,200",
            "1,234,567",
        ),
    )
    asyncio.run(main.fetch_all("batch"))
    _assert_snapshot(
        _snapshot_of(db, "N225"),
        {
            **_N225_SNAPSHOT,
            "open_price": 57500.0,
            "day_high": 58900.0,
            "day_low": 57200.0,
        },
    )

    _install_snapshot_client(
        monkeypatch,
        JP_PAGE,
        _index_page(
            "59,100.25", "58,644.63", "58,700", "59,300", "58,500",
            "2,345,678",
        ),
    )
    asyncio.run(main.fetch_all("batch"))

    rows = db.execute(
        "SELECT * FROM daily_snapshots WHERE code = 'N225'"
    ).fetchall()
    assert len(rows) == 1
    _assert_snapshot(
        rows[0],
        {
            **_N225_SNAPSHOT,
            "close_price": 59100.25,
            "prev_close": 58644.63,
            "open_price": 58700.0,
            "day_high": 59300.0,
            "day_low": 58500.0,
        },
    )
    # The stock rows kept their own values.
    _assert_snapshot(_snapshot_of(db, "9999"), _JP_SNAPSHOT)
