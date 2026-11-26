"""The previous-trading-date fallback.

(Form-string parsing helpers used to live here; input parsing is now
Pydantic's job in the /api/v1 models, covered by tests/test_api.py.)
"""
from datetime import date

from app.main import _prev_trading_date


def test_prev_trading_date_prefers_snapshot_date():
    d = date(2026, 11, 25)
    assert _prev_trading_date(d, date(2026, 11, 24)) == date(2026, 11, 24)


def test_prev_trading_date_weekday():
    # Wed 2026-11-25 -> Tue 2026-11-24
    assert _prev_trading_date(date(2026, 11, 25), None) == date(2026, 11, 24)


def test_prev_trading_date_skips_weekend():
    # Mon 2026-11-30 -> Fri 2026-11-27
    assert _prev_trading_date(date(2026, 11, 30), None) == date(2026, 11, 27)
    # Sun 2026-11-29 -> Fri 2026-11-27
    assert _prev_trading_date(date(2026, 11, 29), None) == date(2026, 11, 27)
    # Sat 2026-11-28 -> Fri 2026-11-27
    assert _prev_trading_date(date(2026, 11, 28), None) == date(2026, 11, 27)
