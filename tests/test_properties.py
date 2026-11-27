"""Property-based tests (Hypothesis) for the pure functions.

Example-based tests check the cases someone thought of; these check
invariants over generated inputs. The 2026-11-27 incident — a close
with a fractional part (830.4) that the price regex missed, so the
volume was stored as the close — is exactly the class of bug that
lives in input variety nobody enumerates by hand.

Profiles are registered in conftest.py: "light" (default, CI) keeps
the suite fast and reproducible, "heavy" digs deeper before
releases. Run only these with: pytest -m property
"""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from app.fetch_service import next_run_at
from app.formatting import (
    fmt_pct,
    fmt_pct_signed,
    fmt_price,
    fmt_yen,
    round_half_up,
)
from app.settings import settings_view
from app.yahoo import parse_jp_page

pytestmark = pytest.mark.property

JST = ZoneInfo("Asia/Tokyo")


# --- next_run_at ------------------------------------------------------


@given(
    now=st.datetimes(
        min_value=datetime(2024, 1, 1),
        max_value=datetime(2030, 12, 31),
        timezones=st.just(JST),
    ),
    hour=st.integers(min_value=0, max_value=23),
    minute=st.integers(min_value=0, max_value=59),
)
def test_next_run_at_is_the_next_occurrence(now, hour, minute):
    result = next_run_at(now, f"{hour:02d}:{minute:02d}")
    # Strictly in the future, at most one day ahead, and exactly at
    # the configured wall-clock time (seconds zeroed).
    assert result > now
    assert result - now <= timedelta(days=1)
    assert (result.hour, result.minute) == (hour, minute)
    assert result.second == 0
    assert result.microsecond == 0


# --- formatting -------------------------------------------------------


@given(n=st.integers(min_value=-(10**12), max_value=10**12))
def test_fmt_yen_integer_round_trip(n):
    s = fmt_yen(n)
    assert "." not in s
    assert int(s.replace(",", "")) == n


@given(cents=st.integers(min_value=0, max_value=10**11))
def test_fmt_price_round_trip(cents):
    # A price with at most two decimal places survives formatting:
    # stripping the commas reads back the same value, and the display
    # never keeps a trailing zero or a bare decimal point.
    v = cents / 100
    s = fmt_price(v)
    assert float(s.replace(",", "")) == v
    assert not s.endswith(".")
    if "." in s:
        assert not s.endswith("0")


@given(
    v=st.floats(
        min_value=-10, max_value=10, allow_nan=False, allow_infinity=False
    )
)
def test_fmt_pct_round_trip_within_rounding(v):
    s = fmt_pct(v)
    assert s.endswith("%")
    # Two decimals of percent: the displayed value is v*100 rounded
    # to two places, so it reads back within half a unit of the
    # last displayed digit.
    assert abs(float(s[:-1]) - v * 100) <= 0.006


@given(
    v=st.floats(
        min_value=-10, max_value=10, allow_nan=False, allow_infinity=False
    )
)
def test_fmt_pct_signed_sign_and_round_trip(v):
    s = fmt_pct_signed(v)
    assert s.endswith("%")
    if v > 0:
        assert s.startswith("+")
    assert abs(float(s[:-1]) - v) <= 0.006


@given(k=st.integers(min_value=0, max_value=10**9))
def test_round_half_up_on_yen_amounts(k):
    # Yen amounts carry at most two decimals (k is hundredths).
    v = k / 100
    r = round_half_up(v)
    assert abs(r - v) <= 0.5
    frac = k % 100
    if frac < 50:
        assert r == k // 100
    else:
        # .50 and above rounds up (the "half up" contract).
        assert r == k // 100 + 1


# --- settings money calculation (pure) --------------------------------


@given(
    total=st.integers(min_value=0, max_value=10**9),
    tax_bp=st.integers(min_value=0, max_value=10000),
)
def test_settings_view_monthly_target_is_rounded_exact(total, tax_bp):
    tax = tax_bp / 10000
    view = settings_view(
        {"target_dividend_total": total, "tax_rate": tax}
    )
    monthly = view["target_monthly_income"]
    exact = total * (1 - tax) / 12
    assert isinstance(monthly, int)
    assert abs(monthly - exact) <= 0.51


@given(
    totals=st.tuples(
        st.integers(min_value=0, max_value=10**9),
        st.integers(min_value=0, max_value=10**9),
    ),
    tax_bp=st.integers(min_value=0, max_value=10000),
)
def test_settings_view_monthly_target_monotonic_in_total(totals, tax_bp):
    lo, hi = sorted(totals)
    tax = tax_bp / 10000
    m_lo = settings_view(
        {"target_dividend_total": lo, "tax_rate": tax}
    )["target_monthly_income"]
    m_hi = settings_view(
        {"target_dividend_total": hi, "tax_rate": tax}
    )["target_monthly_income"]
    assert m_lo <= m_hi


# --- Yahoo page parsing -----------------------------------------------


def _price_text(int_part, frac_digit):
    """Render a price the way the Yahoo page does (commas, optional
    single decimal digit)."""
    if frac_digit == 0:
        return f"{int_part:,}"
    return f"{int_part:,}.{frac_digit}"


def _price_value(int_part, frac_digit):
    return float(Decimal(int_part) + Decimal(frac_digit) / 10)


def _quote_page(price_text, prev_close_text, volume_text):
    """Minimal quote page modelled on the real structure (common
    price board first, then the data list) — the shape of the
    2026-11-27 incident."""
    return f"""
<html><body>
<div class="_PriceBoardMenu__menu_a">メニュー</div>
<div class="_CommonPriceBoard__price_b">
  <span class="_StyledNumber__value_1">{price_text}</span>
</div>
<dl>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">前日終値</span></dt>
  <dd class="_DataListItem__dd_c"><span class="_StyledNumber__value_2">{prev_close_text}</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">出来高</span></dt>
  <dd><span class="_StyledNumber__value_2">{volume_text}</span></dd>
</dl>
</body></html>
"""


@given(
    price_parts=st.tuples(
        st.integers(min_value=1, max_value=99999),
        st.integers(min_value=0, max_value=9),
    ),
    prev_parts=st.tuples(
        st.integers(min_value=1, max_value=99999),
        st.integers(min_value=0, max_value=9),
    ),
    volume=st.integers(min_value=100, max_value=10**9),
)
def test_parse_jp_page_generated_prices(price_parts, prev_parts, volume):
    price = _price_value(*price_parts)
    prev_close = _price_value(*prev_parts)
    assume(price != volume)
    html = _quote_page(
        _price_text(*price_parts),
        _price_text(*prev_parts),
        f"{volume:,}",
    )
    parsed_price, fields = parse_jp_page(html)
    # The close comes from the price board — never the volume, even
    # when the close has a fractional part (2026-11-27 regression).
    assert parsed_price == price
    assert parsed_price != volume
    # Comma-grouped and fractional field values parse to the numbers
    # that were rendered.
    assert fields["前日終値"] == prev_close
    assert fields["出来高"] == float(volume)
