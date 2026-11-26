"""Pure formatting helpers: the numbers every page displays."""
from decimal import Decimal

from app.main import (
    fnum,
    fmt_pct,
    fmt_pct_signed,
    fmt_pct_value,
    fmt_price,
    fmt_signed,
    fmt_yen,
    round_half_up,
)


def test_fnum():
    assert fnum(None) is None
    assert fnum(3) == 3.0
    assert fnum(Decimal("1.5")) == 1.5


def test_fmt_price():
    assert fmt_price(None) == "―"
    assert fmt_price(1000) == "1,000"
    assert fmt_price(1234.5) == "1,234.5"
    assert fmt_price(237.9) == "237.9"
    assert fmt_price(100.10) == "100.1"
    assert fmt_price(0) == "0"


def test_fmt_yen():
    assert fmt_yen(None) == "―"
    assert fmt_yen(36358470) == "36,358,470"
    assert fmt_yen(100.26) == "100.3"
    assert fmt_yen(-2500) == "-2,500"


def test_fmt_signed():
    assert fmt_signed(None) == "―"
    assert fmt_signed(50) == "+50"
    assert fmt_signed(-50) == "-50"
    assert fmt_signed(0) == "0"


def test_fmt_pct_ratio_units():
    assert fmt_pct(None) == "―"
    assert fmt_pct(0.0412) == "4.12%"
    assert fmt_pct(0) == "0.00%"
    assert fmt_pct(-0.0278) == "-2.78%"


def test_fmt_pct_value_already_percent():
    assert fmt_pct_value(None) == "―"
    assert fmt_pct_value(4.12) == "4.12%"
    assert fmt_pct_value(3.7) == "3.70%"


def test_fmt_pct_signed():
    assert fmt_pct_signed(None) == "―"
    assert fmt_pct_signed(1.5) == "+1.50%"
    assert fmt_pct_signed(-2.78) == "-2.78%"
    assert fmt_pct_signed(0) == "0.00%"


def test_round_half_up():
    assert round_half_up(2.5) == 3
    assert round_half_up(2.4) == 2
    assert round_half_up(0.5) == 1
    assert round_half_up(90996.79) == 90997
