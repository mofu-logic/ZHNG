"""History screen: both trade tables get the shared column sorting.

Column sorting is the standard table behaviour in this app (holdings,
candidates, benefit memos, master): a client-side sort whose wiring
lives once in the shared script in base.html, which wires every
sortable header in the page against its own table. Data headers
carry class="sortable" and a data-type of "str" or "num"; numeric
cells carry the raw value in data-v, which the sort compares instead
of the formatted text (so "10,000" and "9,000" order by value, not
by string); the trailing operation column (cancel / delete buttons)
is not sortable. The server-side default order (newest first) is
unchanged -- sorting is something the reader applies on top of it.

These tests fix that spec for the buy and sell tables on /history:
the header structure and data-v cells below, plus the shared wiring
form (a generic querySelectorAll('th.sortable') served from
base.html, no per-table '#buys' / '#sales' selectors, and the sort
logic present exactly once). The per-column str/num split follows
the other tables: dates and text sort as strings, money and share
counts sort as numbers. The memo columns sort as strings, like the
candidate and master memos.
"""
from html.parser import HTMLParser


class _TableParser(HTMLParser):
    """Collect tables as {id, headers, rows}; a cell is {text, attrs}."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._table = None
        self._section = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._table = {"id": a.get("id"), "headers": [], "rows": []}
            self.tables.append(self._table)
        elif self._table is not None and tag in ("thead", "tbody"):
            self._section = tag
        elif self._table is not None and tag == "tr" and self._section == "tbody":
            self._row = []
            self._table["rows"].append(self._row)
        elif self._table is not None and tag in ("th", "td"):
            self._cell = {"text": "", "attrs": a}
            if self._section == "thead" and tag == "th":
                self._table["headers"].append(self._cell)
            elif self._section == "tbody" and tag == "td" and self._row is not None:
                self._row.append(self._cell)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data

    def handle_endtag(self, tag):
        if tag in ("th", "td"):
            self._cell = None
        elif tag in ("thead", "tbody"):
            self._section = None
        elif tag == "tr":
            self._row = None
        elif tag == "table":
            self._table = None


def _tables(page):
    parser = _TableParser()
    parser.feed(page)
    return parser.tables


def _table_by_id(page, table_id):
    for table in _tables(page):
        if table["id"] == table_id:
            return table
    raise AssertionError(f"table id={table_id!r} not found")


def _seed_stock(db, code, name):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, '東証')",
        (code, name),
    )
    db.commit()


def _post_buy(client, **kw):
    body = {
        "code": "1111",
        "name": "銘柄甲",
        "avg_price": 1000,
        "shares": 100,
        "status": "約定済",
    }
    body.update(kw)
    r = client.post("/api/v1/buys", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _post_sale(client, **kw):
    body = {
        "code": "1111",
        "name": "銘柄甲",
        "sell_price": 1200,
        "sell_shares": 10,
        "executed_shares": 10,
        "status": "約定済",
    }
    body.update(kw)
    r = client.post("/api/v1/sales", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# (header label, data-type); the trailing operation column is
# (None, None): present in the table but never sortable.
BUY_HEADERS = [
    ("買付日", "str"),
    ("銘柄コード", "str"),
    ("銘柄名", "str"),
    ("口座", "str"),
    ("取得単価", "num"),
    ("株数", "num"),
    ("購入方針", "str"),
    ("購入契機", "str"),
    ("メモ", "str"),
    ("約定状態", "str"),
    (None, None),
]

SALE_HEADERS = [
    ("注文日", "str"),
    ("約定日", "str"),
    ("銘柄コード", "str"),
    ("銘柄名", "str"),
    ("口座", "str"),
    ("売り単価", "num"),
    ("約定株数／注文株数", "num"),
    ("損益", "num"),
    ("約定状態", "str"),
    ("メモ", "str"),
    (None, None),
]


def _assert_headers(table, spec):
    heads = table["headers"]
    assert len(heads) == len(spec)
    for cell, (label, dtype) in zip(heads, spec):
        classes = cell["attrs"].get("class", "").split()
        if label is None:
            assert "sortable" not in classes
            continue
        assert cell["text"].strip() == label
        assert "sortable" in classes
        assert cell["attrs"].get("data-type") == dtype
        if dtype == "num":
            assert "num" in classes


def test_buy_table_headers_are_sortable(client, db):
    _seed_stock(db, "1111", "銘柄甲")
    _post_buy(client)
    page = client.get("/history").text
    _assert_headers(_table_by_id(page, "buys"), BUY_HEADERS)


def test_sell_table_headers_are_sortable(client, db):
    _seed_stock(db, "1111", "銘柄甲")
    _post_sale(client)
    page = client.get("/history").text
    _assert_headers(_table_by_id(page, "sales"), SALE_HEADERS)


def test_history_tables_sort_wiring(client, db):
    page = client.get("/history").text
    # The wiring is the shared one from base.html: a generic pass
    # over every sortable header in the page, not a per-table block
    # copied into this template.
    assert "querySelectorAll('th.sortable')" in page
    assert "querySelectorAll('#buys th.sortable')" not in page
    assert "querySelectorAll('#sales th.sortable')" not in page
    # The sort logic arrives exactly once (from base), and keeps the
    # direction on the header itself.
    assert page.count("dataset.dir === 'asc'") == 1
    assert "dataset.dir" in page


def test_num_cells_carry_raw_sort_values(client, db):
    _seed_stock(db, "1111", "銘柄甲")
    _post_buy(client, avg_price=1234.5, shares=300)
    # Profits whose formatted text would mis-order as strings
    # ("+10,000" < "+9,000"), plus a partial fill whose executed and
    # ordered counts differ.
    _post_sale(client, buy_price=1000, sell_price=2000,
               sell_shares=10, executed_shares=10)
    _post_sale(client, buy_price=3000, sell_price=2100,
               sell_shares=10, executed_shares=10)
    _post_sale(client, buy_price=1000, sell_price=1100,
               sell_shares=100, executed_shares=30)
    page = client.get("/history").text

    # Cell indexes: the account column sits at index 3 in both
    # tables (right after the stock name), shifting the numeric
    # cells one place from the old layout.
    buy_row = _table_by_id(page, "buys")["rows"][0]
    assert buy_row[4]["text"].strip() == "1,234.5"
    assert float(buy_row[4]["attrs"]["data-v"]) == 1234.5
    assert buy_row[5]["text"].strip() == "300"
    assert float(buy_row[5]["attrs"]["data-v"]) == 300

    sale_rows = _table_by_id(page, "sales")["rows"]
    profits = sorted(
        float(r[7]["attrs"]["data-v"]) for r in sale_rows
    )
    assert profits == [-9000, 3000, 10000]
    for r in sale_rows:
        assert float(r[5]["attrs"]["data-v"]) > 0  # sell price
    partial = [r for r in sale_rows if r[6]["text"].strip() == "30 / 100"]
    assert len(partial) == 1
    # The combined executed/ordered cell sorts by one raw count.
    assert float(partial[0][6]["attrs"]["data-v"]) in (30, 100)


def test_default_order_is_newest_first(client, db):
    # Regression: with no sorting applied, the server-side order is
    # unchanged (buys by purchase date, sales by order date, newest
    # first; same-day rows newest id first).
    for code, name in (("1111", "銘柄甲"), ("2222", "銘柄乙"), ("3333", "銘柄丙")):
        _seed_stock(db, code, name)
    _post_buy(client, code="1111", name="銘柄甲", purchase_date="2026-01-10")
    _post_buy(client, code="2222", name="銘柄乙", purchase_date="2026-03-05")
    _post_buy(client, code="3333", name="銘柄丙", purchase_date="2026-03-05")
    _post_sale(client, code="1111", name="銘柄甲", order_start_date="2026-02-01")
    _post_sale(client, code="2222", name="銘柄乙", order_start_date="2026-04-01")
    page = client.get("/history").text
    tables = _tables(page)
    assert len(tables) == 2
    buy_codes = [r[1]["text"].strip() for r in tables[0]["rows"]]
    assert buy_codes == ["3333", "2222", "1111"]
    sale_codes = [r[2]["text"].strip() for r in tables[1]["rows"]]
    assert sale_codes == ["2222", "1111"]


def test_cancelled_and_unlinked_rows_still_render(client, db):
    # Regression: a cancelled row and a row with no account type mix
    # into the table without breaking its rendering (sorting itself
    # only reorders DOM rows, so these rows sort like any other).
    _seed_stock(db, "1111", "銘柄甲")
    linked = _post_buy(client, account_type="特定", purchase_date="2026-01-15")
    r = client.post(f"/api/v1/buys/{linked['id']}/cancel")
    assert r.status_code == 200, r.text
    _post_buy(client, status="注文中")
    page = client.get("/history").text
    tables = _tables(page)
    rows = tables[0]["rows"]
    assert len(rows) == 2
    # With the link-state column gone, the rows are told apart by
    # the account cell (index 3, right after the stock name): the
    # cancelled row is the linked one （特定）, the unlinked row's
    # cell stays empty. The operation cell is the last (index 10).
    by_account = {r[3]["text"].strip(): r for r in rows}
    assert set(by_account) == {"特定", ""}
    # The cancelled row loses its cancel button, keeps delete.
    assert by_account["特定"][10]["text"].strip() == "削除"
    # The unlinked row keeps both actions.
    assert "取消" in by_account[""][10]["text"]
    assert "削除" in by_account[""][10]["text"]
    # No cell carries a link-state tag any more.
    for r in rows:
        assert len(r) == 11
        texts = [c["text"].strip() for c in r]
        assert "取消済み" not in texts
        assert "対象外" not in texts
        assert "反映済み" not in texts
