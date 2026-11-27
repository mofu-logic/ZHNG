"""Configurable nightly-batch start time (fetch_start_time).

The batch start time is a setting, not a hard-coded 01:00: it is
exposed through /api/v1/settings as "HH:MM" (default "01:00"),
validated against ^([01][0-9]|2[0-3]):[0-5][0-9]$, used by
app.fetch_service.next_run_at to compute the next run, shown in the
/data page tooltip, and edited on /settings with a time input.
"""
import asyncio
import re
from datetime import datetime

import pytest

from app.config import JST
from test_api import KPI_PAYLOAD


def _put_fetch_start_time(client, value):
    return client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "fetch_start_time": value},
    )


def _fetch_start_time_input(html):
    """The <input> tag carrying name="fetch_start_time", or None."""
    for tag in re.findall(r"<input\b[^>]*>", html):
        if 'name="fetch_start_time"' in tag:
            return tag
    return None


# --- 1. Settings API: default and round-trip -------------------------


def test_settings_fetch_start_time_default(client, db):
    settings = client.get("/api/v1/settings").json()
    assert settings["fetch_start_time"] == "01:00"


def test_settings_fetch_start_time_roundtrip(client, db):
    r = _put_fetch_start_time(client, "01:30")
    assert r.status_code == 200
    assert r.json()["fetch_start_time"] == "01:30"
    got = client.get("/api/v1/settings").json()
    assert got["fetch_start_time"] == "01:30"


# --- 2. Settings API: validation -------------------------------------


@pytest.mark.parametrize(
    "value", ["25:00", "24:00", "12:60", "1時", "abc", ""]
)
def test_settings_fetch_start_time_validation(client, db, value):
    r = _put_fetch_start_time(client, value)
    assert r.status_code == 422


# --- 3. next_run_at ---------------------------------------------------


@pytest.mark.parametrize(
    "now,start_time,expected",
    [
        # Before today's start time: today.
        (
            datetime(2026, 11, 27, 0, 30, tzinfo=JST),
            "01:00",
            datetime(2026, 11, 27, 1, 0, tzinfo=JST),
        ),
        # Exactly at the start time: already passed, so tomorrow.
        (
            datetime(2026, 11, 27, 1, 0, tzinfo=JST),
            "01:00",
            datetime(2026, 11, 28, 1, 0, tzinfo=JST),
        ),
        # Late at night: tomorrow.
        (
            datetime(2026, 11, 27, 23, 59, tzinfo=JST),
            "01:00",
            datetime(2026, 11, 28, 1, 0, tzinfo=JST),
        ),
        # A start time earlier today: tomorrow.
        (
            datetime(2026, 11, 27, 10, 0, tzinfo=JST),
            "09:00",
            datetime(2026, 11, 28, 9, 0, tzinfo=JST),
        ),
        # A start time later today: today.
        (
            datetime(2026, 11, 27, 10, 0, tzinfo=JST),
            "23:45",
            datetime(2026, 11, 27, 23, 45, tzinfo=JST),
        ),
    ],
)
def test_next_run_at(now, start_time, expected):
    from app.fetch_service import next_run_at

    assert next_run_at(now, start_time) == expected


def test_next_run_at_result_has_no_seconds():
    from app.fetch_service import next_run_at

    now = datetime(2026, 11, 27, 0, 30, 45, 123456, tzinfo=JST)
    result = next_run_at(now, "01:00")
    assert result == datetime(2026, 11, 27, 1, 0, tzinfo=JST)
    assert result.second == 0
    assert result.microsecond == 0


# --- 4. /data page tooltip --------------------------------------------


def test_data_page_shows_fetch_start_time(client, db):
    page = client.get("/data").text
    assert "毎日 01:00 JST" in page
    assert (
        "株価と配当の更新は夜間バッチで毎日 01:00 JST に自動実行されます。"
        in page
    )

    r = _put_fetch_start_time(client, "01:30")
    assert r.status_code == 200

    page = client.get("/data").text
    assert "毎日 01:30 JST" in page
    assert (
        "株価と配当の更新は夜間バッチで毎日 01:30 JST に自動実行されます。"
        in page
    )


# --- 5. /settings page time input --------------------------------------


def test_settings_page_has_fetch_start_time_input(client, db):
    page = client.get("/settings").text
    tag = _fetch_start_time_input(page)
    assert tag is not None
    assert 'type="time"' in tag
    assert 'value="01:00"' in tag

    r = _put_fetch_start_time(client, "01:30")
    assert r.status_code == 200

    page = client.get("/settings").text
    tag = _fetch_start_time_input(page)
    assert tag is not None
    assert 'type="time"' in tag
    assert 'value="01:30"' in tag


# --- 6. scheduler: transient DB failure must not kill the loop -------


class _FakeCursor:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return {"value": self._value} if self._value is not None else None


class _FakeConn:
    """Minimal connection stand-in: app_config only reads settings."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        key = params[0] if params else None
        return _FakeCursor("01:00" if key == "fetch_start_time" else None)


def test_scheduler_survives_transient_config_read_failure(monkeypatch, caplog):
    """Regression (review R1): the scheduler re-reads the settings from
    the DB on every wait iteration. A transient DB failure there used to
    escape the while loop (only fetch_all was guarded), silently killing
    the scheduler task until a process restart. The loop must log the
    failure, back off, and still reach fetch_all once the DB recovers.
    """
    from app import fetch_service

    # First config read fails; subsequent reads succeed.
    db_calls = {"n": 0}

    def fake_db():
        db_calls["n"] += 1
        if db_calls["n"] == 1:
            raise RuntimeError("temporary DB failure")
        return _FakeConn()

    fetched = []

    async def fake_fetch_all(note, *, db=None, lock=None):
        fetched.append(note)

    monkeypatch.setattr(fetch_service, "fetch_all", fake_fetch_all)
    # The app logger sets propagate=False (main.py); let caplog see it.
    monkeypatch.setattr(fetch_service.logger, "propagate", True)

    # Controlled clock: just before 01:00, then just after it, so the
    # recovered iteration fires instead of waiting for the next day.
    times = [
        datetime(2026, 11, 27, 0, 59, 59, tzinfo=JST),
        datetime(2026, 11, 27, 1, 0, 1, tzinfo=JST),
    ]

    class FakeDateTime(datetime):
        _idx = 0

        @classmethod
        def now(cls, tz=None):
            idx = min(cls._idx, len(times) - 1)
            cls._idx += 1
            return times[idx]

    monkeypatch.setattr(fetch_service, "datetime", FakeDateTime)

    real_sleep = asyncio.sleep

    async def instant_sleep(*a, **k):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    async def scenario():
        task = asyncio.create_task(
            fetch_service.scheduler(db=fake_db, lock=asyncio.Lock())
        )
        try:
            for _ in range(200):
                if fetched:
                    break
                await real_sleep(0.005)
            # On the buggy code the task is finished (dead) here and
            # fetched stays empty.
            assert not task.done()
            assert fetched == ["batch"]
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    with caplog.at_level("ERROR", logger="stock-app"):
        asyncio.run(scenario())

    # The transient failure must be logged (with a traceback), not
    # silently swallowed.
    assert any(record.exc_info for record in caplog.records)
