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
from test_parse import INDEX_PAGE, JP_PAGE


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
