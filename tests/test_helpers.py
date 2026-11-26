"""Form-parsing helpers and the previous-trading-date fallback."""
from datetime import date

from app.main import _f, _i, _prev_trading_date, _s


def test_s():
    assert _s("  abc ") == "abc"
    assert _s("") is None
    assert _s("   ") is None
    assert _s(None) is None


def test_f():
    assert _f("12.5") == 12.5
    assert _f("") is None
    assert _f(None) is None
    assert _f(" 100 ") == 100.0


def test_i():
    assert _i("100") == 100
    assert _i("100.9") == 100
    assert _i("") is None
    assert _i(None) is None


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
