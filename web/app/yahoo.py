"""Yahoo!ファイナンス日本からの株価・銘柄情報の取得とパース。

取得元は Yahoo!ファイナンス日本の銘柄ページに統一する。
DB には依存しない（fetch_all 側が保存を担う）。
"""
import re

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)


def _page_fields(html):
    fields = {}
    for term, dd in re.findall(
        r"_DataListItem__term_[^\" >]*\">(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S
    ):
        name = "".join(
            re.findall(r"_DataListItem__(?:name|sub)_[^\" >]*\">([^<]*)<", term)
        )
        if not name or name in fields:
            continue
        vm = re.search(r"_StyledNumber__value_[^\"]*\">([\d,\.]+)", dd)
        if vm:
            fields[name] = float(vm.group(1).replace(",", ""))
        elif name.startswith("PER"):
            # PER is rendered like （連）11.17 倍 without a styled number span.
            plain = re.sub(r"<[^>]+>", "", dd)
            pm = re.search(r"([\d,\.]+)\s*倍", plain)
            if pm:
                fields[name] = float(pm.group(1).replace(",", ""))
    return fields


# Yahoo! Finance Japan quote pages are suffixed per exchange:
# .T Tokyo, .N Nagoya, .S Sapporo, .F Fukuoka (verified live:
# 2928 RIZAP is .S only, 1771 is .F only).
EXCHANGE_SUFFIXES = (("T", "東証"), ("N", "名証"), ("S", "札証"), ("F", "福証"))


def suffix_for_market(market):
    for suffix, name in EXCHANGE_SUFFIXES:
        if name == market:
            return suffix
    return "T"


def parse_jp_page(html):
    """Extract the current price and the labeled data-list values from a
    Yahoo! Finance Japan quote page (finance.yahoo.co.jp/quote/{code}.T|.N|.S|.F).

    The price is read from the _CommonPriceBoard__price_ board only.
    The old regex scanned from the first "_PriceBoard" substring for
    the first integer styled number: a close with a fractional part
    (e.g. 830.4) never matched at the price element, so the scan
    walked on and returned the volume, the previous close or an
    integer-part-only element as the price (2026-11-27 incident).
    When the common board exists it is authoritative: a board
    showing "---" (no trades today) means there is no price, and the
    numbers behind the board (the data list) must not be picked up
    instead. Pages without the common board (the old layout) fall
    back to the first styled number after _PriceBoard, decimals
    included.
    """
    price = None
    board_at = html.find("_CommonPriceBoard__price_")
    if board_at >= 0:
        board = html[board_at:]
        list_at = board.find("_DataListItem__term_")
        if list_at >= 0:
            board = board[:list_at]
        vm = re.search(r"_StyledNumber__value_[^\"]*\">([\d,\.]+)<", board)
        if vm:
            price = float(vm.group(1).replace(",", ""))
    else:
        m = re.search(
            r"_PriceBoard.*?_StyledNumber__value_[^\"]*\">([\d,\.]+)<",
            html,
            re.S,
        )
        if m:
            price = float(m.group(1).replace(",", ""))
    return price, _page_fields(html)


def parse_stock_name(html, code):
    """The quote page <title> looks like
    'ソフトバンクグループ(株)【9984】：株価・株式情報 ... - Yahoo!ファイナンス'."""
    m = re.search(r"<title>(.*?)【" + re.escape(code) + "】", html, re.S)
    return m.group(1).strip() if m else None


def parse_index_page(html):
    """Nikkei 225 page (quote/998407.O): the main value sits in a
    _CommonPriceBoard__price_ element; the generic _PriceBoard regex
    picks up a different index (e.g. TOPIX) that appears earlier."""
    m = re.search(
        r"_CommonPriceBoard__price_[^\"]*\">.*?_StyledNumber__value_[^\"]*\">([\d,\.]+)<",
        html,
        re.S,
    )
    price = float(m.group(1).replace(",", "")) if m else None
    return price, _page_fields(html)


def parse_profile_page(html):
    """Industry classification from the Yahoo! Finance Japan profile
    page (quote/{code}.{suffix}/profile): a table row labelled
    業種分類 whose value is the industry name (linked to a search)."""
    m = re.search(r"業種分類</th><td[^>]*>(?:<a[^>]*>)?([^<]{1,30})", html)
    if m:
        name = m.group(1).strip()
        if name:
            return name
    return None


async def fetch_one_quote(code):
    """Fetch one code right now (used when a purchase candidate is
    registered). Tries each exchange suffix in EXCHANGE_SUFFIXES order.
    A 404 means the code is not on that exchange; any other non-200
    answer is treated as transient, so the walk moves on to the next
    suffix and only surfaces the last such error when every suffix
    failed that way. Returns (market, name, price, fields); raises
    RuntimeError when the code cannot be fetched at all."""
    last_error = None
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25) as client:
        for suffix, market in EXCHANGE_SUFFIXES:
            r = await client.get(
                f"https://finance.yahoo.co.jp/quote/{code}.{suffix}"
            )
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                last_error = RuntimeError(f"HTTP {r.status_code}")
                continue
            price, fields = parse_jp_page(r.text)
            if price is None or price <= 0:
                raise RuntimeError("price not found in page")
            return market, parse_stock_name(r.text, code), price, fields
    if last_error is not None:
        raise last_error
    raise RuntimeError("銘柄が見つかりませんでした")
