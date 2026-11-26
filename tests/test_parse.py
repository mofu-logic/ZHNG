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
