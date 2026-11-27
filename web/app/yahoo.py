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
    Yahoo! Finance Japan quote page (finance.yahoo.co.jp/quote/{code}.T|.N|.S|.F)."""
    m = re.search(r"_PriceBoard.*?_StyledNumber__value_[^\"]*\">([\d,]+)<", html, re.S)
    price = float(m.group(1).replace(",", "")) if m else None
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
    Returns (market, name, price, fields); raises RuntimeError when
    the code cannot be fetched at all."""
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25) as client:
        for suffix, market in EXCHANGE_SUFFIXES:
            r = await client.get(
                f"https://finance.yahoo.co.jp/quote/{code}.{suffix}"
            )
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            price, fields = parse_jp_page(r.text)
            if price is None or price <= 0:
                raise RuntimeError("price not found in page")
            return market, parse_stock_name(r.text, code), price, fields
    raise RuntimeError("銘柄が見つかりませんでした")
