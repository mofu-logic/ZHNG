"""Yahoo! Finance Japan page parsing.

The nightly fetch depends on these CSS-class regexes; if Yahoo changes
its markup, these tests are the canary. Fixtures mimic the real page
structure (styled-number spans inside dt/dd data-list items).
"""
from app.main import parse_index_page, parse_jp_page, parse_profile_page

JP_PAGE = """
<html><body>
<div class="_PriceBoard__main_x">
  <span class="_StyledNumber__value_1">1,000</span>
</div>
<dl>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">前日終値</span></dt>
  <dd class="_DataListItem__dd_c"><span class="_StyledNumber__value_2">990</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">始値</span></dt>
  <dd><span class="_StyledNumber__value_2">995</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">高値</span></dt>
  <dd><span class="_StyledNumber__value_2">1,010</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">安値</span></dt>
  <dd><span class="_StyledNumber__value_2">985</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">出来高</span></dt>
  <dd><span class="_StyledNumber__value_2">123,456</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">時価総額</span></dt>
  <dd><span class="_StyledNumber__value_2">1,234,567</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">1株配当（会社予想）</span></dt>
  <dd><span class="_StyledNumber__value_2">50</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">配当利回り（会社予想）</span></dt>
  <dd><span class="_StyledNumber__value_2">5.00</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">PER（会社予想）</span></dt>
  <dd>（連）11.17 倍</dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">PBR（実績）</span></dt>
  <dd><span class="_StyledNumber__value_2">1.23</span></dd>
</dl>
</body></html>
"""

INDEX_PAGE = """
<html><body>
<div class="_PriceBoard__main_x">
  <span class="_StyledNumber__value_1">2,000</span>
</div>
<div class="_CommonPriceBoard__price_y">
  <span class="_StyledNumber__value_9">58,644.63</span>
</div>
<dl>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">前日終値</span></dt>
  <dd><span class="_StyledNumber__value_2">57,011.62</span></dd>
</dl>
</body></html>
"""


def test_parse_jp_page_price_and_fields():
    price, fields = parse_jp_page(JP_PAGE)
    assert price == 1000.0
    assert fields["前日終値"] == 990.0
    assert fields["始値"] == 995.0
    assert fields["高値"] == 1010.0
    assert fields["安値"] == 985.0
    assert fields["出来高"] == 123456.0
    assert fields["時価総額"] == 1234567.0
    assert fields["1株配当（会社予想）"] == 50.0
    assert fields["配当利回り（会社予想）"] == 5.0
    assert fields["PBR（実績）"] == 1.23


def test_parse_jp_page_per_plain_text():
    # PER has no styled-number span; it is read from the plain "xx.xx 倍".
    _, fields = parse_jp_page(JP_PAGE)
    assert fields["PER（会社予想）"] == 11.17


def test_parse_jp_page_missing_price():
    price, fields = parse_jp_page("<html><body>no board here</body></html>")
    assert price is None
    assert fields == {}


def test_parse_jp_page_duplicate_field_keeps_first():
    html = JP_PAGE.replace(
        "</dl>",
        '<dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">前日終値</span></dt>'
        '<dd><span class="_StyledNumber__value_2">111</span></dd></dl>',
    )
    _, fields = parse_jp_page(html)
    assert fields["前日終値"] == 990.0


def test_parse_index_page_uses_common_price_board():
    # The generic board earlier in the page is a different index (TOPIX);
    # the Nikkei value must come from _CommonPriceBoard__price_.
    price, fields = parse_index_page(INDEX_PAGE)
    assert price == 58644.63
    assert fields["前日終値"] == 57011.62


def test_parse_index_page_missing():
    price, _ = parse_index_page("<html><body></body></html>")
    assert price is None

PROFILE_PAGE = """
<html><body><table>
<tr><th class="head">業種分類</th><td class="detail"><a href="https://finance.yahoo.co.jp/search/qi?ids=7050">銀行業</a></td></tr>
</table></body></html>
"""


def test_parse_profile_page():
    assert parse_profile_page(PROFILE_PAGE) == "銀行業"
    assert parse_profile_page("<html><body></body></html>") is None


# --- Fixtures modelled on the real page structure ---------------------
# The real quote page carries the current price in a
# _CommonPriceBoard__price_ element, preceded by a _PriceBoardMenu and
# followed by a price-change label, a PTS price and the data list.
# JP_PAGE above predates this structure (integer price, simple board),
# which is why it could not catch the decimal-price bug of 2026-11-27:
# the price regex captured only [\d,]+ followed by "<", so a price with
# a fractional part never matched at the price element and the scan
# walked on to the first integer styled number later in the page
# (the volume, the previous close, an integer-part-only element, ...).

JP_PAGE_DECIMAL = """
<html><body>
<div class="_PriceBoardMenu__menu_a">メニュー</div>
<div class="_CommonPriceBoard__price_b">
  <span class="_StyledNumber__value_1">830.4</span>
</div>
<div class="_PriceChangeLabel__label_c">
  <span class="_StyledNumber__value_2">3.4</span>
</div>
<div class="_PtsPrice__price_d">
  <span class="_StyledNumber__value_2">831.9</span>
</div>
<dl>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">前日終値</span></dt>
  <dd class="_DataListItem__dd_c"><span class="_StyledNumber__value_2">825.3</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">始値</span></dt>
  <dd><span class="_StyledNumber__value_2">828.0</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">高値</span></dt>
  <dd><span class="_StyledNumber__value_2">835.2</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">安値</span></dt>
  <dd><span class="_StyledNumber__value_2">826.5</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">出来高</span></dt>
  <dd><span class="_StyledNumber__value_2">1,234,500</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">時価総額</span></dt>
  <dd><span class="_StyledNumber__value_2">1,234,567</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">1株配当（会社予想）</span></dt>
  <dd><span class="_StyledNumber__value_2">50</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">配当利回り（会社予想）</span></dt>
  <dd><span class="_StyledNumber__value_2">6.02</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">PER（会社予想）</span></dt>
  <dd>（連）28.63 倍</dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">PBR（実績）</span></dt>
  <dd><span class="_StyledNumber__value_2">1.23</span></dd>
</dl>
</body></html>
"""

JP_PAGE_INT_PART = """
<html><body>
<div class="_PriceBoardMenu__menu_a">メニュー</div>
<div class="_CommonPriceBoard__price_b">
  <span class="_StyledNumber__value_1">830.4</span>
</div>
<div class="_PriceBoard__subPrice_c">
  <span class="_StyledNumber__value_3">830</span>
</div>
<dl>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">前日終値</span></dt>
  <dd class="_DataListItem__dd_c"><span class="_StyledNumber__value_2">825</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">始値</span></dt>
  <dd><span class="_StyledNumber__value_2">828</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">高値</span></dt>
  <dd><span class="_StyledNumber__value_2">835</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">安値</span></dt>
  <dd><span class="_StyledNumber__value_2">826</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">出来高</span></dt>
  <dd><span class="_StyledNumber__value_2">1,234,500</span></dd>
</dl>
</body></html>
"""

JP_PAGE_NO_TRADE = """
<html><body>
<div class="_PriceBoardMenu__menu_a">メニュー</div>
<div class="_CommonPriceBoard__price_b">
  <span class="_CommonPriceBoard__priceValue_c">---</span>
</div>
<dl>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">前日終値</span></dt>
  <dd class="_DataListItem__dd_c"><span class="_StyledNumber__value_2">3,029</span></dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">始値</span></dt>
  <dd>---</dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">高値</span></dt>
  <dd>---</dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">安値</span></dt>
  <dd>---</dd>
  <dt class="_DataListItem__term_a"><span class="_DataListItem__name_b">出来高</span></dt>
  <dd>---</dd>
</dl>
</body></html>
"""


def test_parse_jp_page_decimal_price_is_not_the_volume():
    # Regression (2026-11-27): a close with a fractional part did not
    # match the price regex at the price element, and the scan walked
    # on to the volume, which was then stored as the close.
    price, _ = parse_jp_page(JP_PAGE_DECIMAL)
    assert price == 830.4


def test_parse_jp_page_all_decimal_fields():
    price, fields = parse_jp_page(JP_PAGE_DECIMAL)
    assert price == 830.4
    assert fields["前日終値"] == 825.3
    assert fields["始値"] == 828.0
    assert fields["高値"] == 835.2
    assert fields["安値"] == 826.5
    assert fields["出来高"] == 1234500.0


def test_parse_jp_page_decimal_price_is_not_its_integer_part():
    # Variant of the same bug: an element showing only the integer
    # part of the price sits right after the price board, and the
    # fractional part was silently dropped (830.4 stored as 830.0).
    price, _ = parse_jp_page(JP_PAGE_INT_PART)
    assert price == 830.4


def test_parse_jp_page_no_trade_day_has_no_price():
    # No trades today: the price board shows "---" and OHLC/volume are
    # "---" too. The previous close (still listed in the data list)
    # must not be mistaken for today's price.
    price, fields = parse_jp_page(JP_PAGE_NO_TRADE)
    assert price is None
    assert fields["前日終値"] == 3029.0
    assert "始値" not in fields
    assert "高値" not in fields
    assert "安値" not in fields
    assert "出来高" not in fields
