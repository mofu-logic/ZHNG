"""Shared column sorting and memo tooltips: one implementation each.

Column sorting is a standard table behaviour in this app, and the
.memo-tip tooltip is how long texts are shown inside tables. Both
used to be spelled out in every page template: the sort wiring was
copied into five templates (six blocks), and the memo truncation
snippet into four templates (six cells). This change keeps exactly
one implementation of each, with no behaviour change:

- The sort wiring lives in the shared script in base.html. It
  selects every sortable header in the page --
  document.querySelectorAll('th.sortable') -- and wires it against
  the header's own table, so any table carrying sortable headers is
  sortable with no per-page code. No page keeps a per-table selector
  of the form '#<id> th.sortable', and the sort logic itself appears
  exactly once in every rendered page (the copy served from base).
- Each page keeps only the table markup: the table id, the sortable
  headers with their data-type, and the raw data-v on numeric cells.
- The memo snippet becomes a single Jinja macro in one shared
  template file, which the four memo templates import instead of
  spelling the snippet out. The rendered cell is unchanged: empty
  renders nothing, up to 20 chars renders as-is, longer text shows
  the first 20 chars + '…' with the full text in the data-tip of a
  .memo-tip span.

Red / Green against the pre-change code:

- Red: the generic shared wiring on every page (including a page
  with no sortable tables, which proves it comes from base); no
  per-table '#<id> th.sortable' selector anywhere; the sort logic
  exactly once on /history (today it is copied in twice); the memo
  snippet in exactly one shared template defining a macro, imported
  by the four memo templates.
- Green (regression pins, already true today and kept so): the
  header structure and numeric data-v cells of the holdings,
  candidates, benefit-memos and master tables (the history tables
  are pinned in test_history_sort.py), and the memo rendering of a
  long benefit-memo status, which no earlier test pinned.
"""
from pathlib import Path

from conftest import seed_settings, seed_stock_holding
from test_candidates import _named_fake_client
from test_history_sort import _table_by_id

from app import main

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "web" / "app" / "templates"
)

SORTABLE_PAGES = [
    "/",
    "/history",
    "/candidates",
    "/benefit-memos",
    "/master",
]
TABLE_IDS = [
    "holdings",
    "buys",
    "sales",
    "candidates",
    "benefit-memos",
    "master",
]
MEMO_TEMPLATES = [
    "candidates.html",
    "history.html",
    "master.html",
    "benefit_memos.html",
]

# (header label, data-type); a data-type of None is a plain header
# that never sorts (the operations column, and the dashboard's
# judgment-memo column -- the known exception).
HOLDINGS_HEADERS = [
    ("銘柄コード", "str"),
    ("銘柄名", "str"),
    ("口座", "str"),
    ("現在値", "num"),
    ("平均取得単価", "num"),
    ("株数", "num"),
    ("評価額", "num"),
    ("損益", "num"),
    ("損益率", "num"),
    ("利回り（買値）", "num"),
    ("利回り（現在値）", "num"),
    ("配当額予想", "num"),
    ("優待体感額", "num"),
    ("優待込み（現在値）", "num"),
    ("方針", "str"),
    ("買付日", "str"),
    ("判断メモ", None),
    (None, None),
]

CANDIDATES_HEADERS = [
    ("銘柄コード", "str"),
    ("銘柄名", "str"),
    ("現在値", "num"),
    ("前日比", "num"),
    ("1株配当", "num"),
    ("配当利回り", "num"),
    ("PER", "num"),
    ("PBR", "num"),
    ("方針", "str"),
    ("メモ", "str"),
    ("希望購入価格", "num"),
    ("取得日", "str"),
    (None, None),
]

BENEFIT_MEMO_HEADERS = [
    ("銘柄コード", "str"),
    ("銘柄名", "str"),
    ("株主番号", "str"),
    ("期限", "str"),
    ("期限2", "str"),
    ("配達待ち", "str"),
    ("ステータス", "str"),
    ("申込書到着日", "str"),
    ("申込書到着日2", "str"),
    ("優待品到着日", "str"),
    ("優待品到着日2", "str"),
    (None, None),
]

MASTER_HEADERS = [
    ("銘柄コード", "str"),
    ("銘柄名", "str"),
    ("1株配当", "num"),
    ("優待体感額", "num"),
    ("優待内容", "str"),
    ("判断日", "str"),
    ("判断メモ", "str"),
    ("方針", "str"),
    ("売却候補", "str"),
    (None, None),
]


def _assert_headers(table, spec):
    heads = table["headers"]
    assert len(heads) == len(spec)
    for cell, (label, dtype) in zip(heads, spec):
        classes = cell["attrs"].get("class", "").split()
        if label is not None:
            assert cell["text"].strip() == label
        if dtype is None:
            assert "sortable" not in classes
            continue
        assert "sortable" in classes
        assert cell["attrs"].get("data-type") == dtype
        if dtype == "num":
            assert "num" in classes


def test_sort_wiring_is_shared_and_generic(client, db):
    seed_settings(db)
    for path in SORTABLE_PAGES:
        page = client.get(path).text
        # The one shared wiring: a generic pass over every sortable
        # header in the page, served from base.html.
        assert "querySelectorAll('th.sortable')" in page, path
        # No per-table selector survives on any page.
        for tid in TABLE_IDS:
            assert f"querySelectorAll('#{tid} th.sortable')" not in page, (
                path,
                tid,
            )
        # The sort logic itself arrives exactly once per page.
        assert page.count("dataset.dir === 'asc'") == 1, path


def test_sort_wiring_arrives_on_pages_without_sortable_tables(client, db):
    # /summary has tables but no sortable headers. The wiring still
    # arriving proves it is served from the shared base template,
    # not copied into the pages that sort.
    seed_settings(db)
    page = client.get("/summary").text
    assert "querySelectorAll('th.sortable')" in page
    assert page.count("dataset.dir === 'asc'") == 1


def test_holdings_table_markup_unchanged(client, db):
    seed_stock_holding(db)
    page = client.get("/").text
    table = _table_by_id(page, "holdings")
    _assert_headers(table, HOLDINGS_HEADERS)
    row = table["rows"][0]
    # Numeric cells carry the raw values the shared sort compares:
    # current price, average buy price, share count.
    assert float(row[3]["attrs"]["data-v"]) == 1000
    assert float(row[4]["attrs"]["data-v"]) == 800
    assert float(row[5]["attrs"]["data-v"]) == 100


def test_candidates_table_markup_unchanged(client, db, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    r = client.post("/api/v1/candidates", json={"code": "7777"})
    assert r.status_code == 201, r.text
    page = client.get("/candidates").text
    table = _table_by_id(page, "candidates")
    _assert_headers(table, CANDIDATES_HEADERS)
    row = table["rows"][0]
    # The close price from the fetched page, as a raw value.
    assert float(row[2]["attrs"]["data-v"]) == 1000


def test_benefit_memos_table_markup_unchanged(client, db):
    seed_stock_holding(db, code="1001", name="優待あり銘柄", benefit=5000)
    page = client.get("/benefit-memos").text
    table = _table_by_id(page, "benefit-memos")
    _assert_headers(table, BENEFIT_MEMO_HEADERS)


def test_master_table_markup_unchanged(client, db):
    seed_stock_holding(db)
    page = client.get("/master").text
    table = _table_by_id(page, "master")
    _assert_headers(table, MASTER_HEADERS)
    row = table["rows"][0]
    assert float(row[2]["attrs"]["data-v"]) == 40  # dividend per share
    assert float(row[3]["attrs"]["data-v"]) == 5000  # benefit value


def test_memo_snippet_lives_in_one_shared_macro():
    sources = {
        p.name: p.read_text(encoding="utf-8")
        for p in TEMPLATES_DIR.glob("*.html")
    }
    # Exactly one template spells the .memo-tip span out, and it is
    # a shared macro file, not one of the pages that show memos.
    with_snippet = [
        name
        for name, src in sources.items()
        if '<span class="memo-tip"' in src
    ]
    assert len(with_snippet) == 1
    shared = with_snippet[0]
    assert shared not in MEMO_TEMPLATES
    assert "{% macro" in sources[shared]
    # Each memo page imports the shared file instead of copying the
    # snippet.
    for name in MEMO_TEMPLATES:
        assert '<span class="memo-tip"' not in sources[name]
        assert shared in sources[name]
        assert "{% import" in sources[name] or "{% from" in sources[name]


def test_benefit_memo_long_status_truncated_with_hover(client, db):
    seed_stock_holding(db, code="1001", name="優待あり銘柄", benefit=5000)
    status = "カタログから好きな商品を選んで申し込む形式の優待です"
    r = client.put("/api/v1/benefit-memos/1001", json={"status": status})
    assert r.status_code == 200, r.text
    page = client.get("/benefit-memos").text
    # The cell shows only the first 20 chars; the full text rides in
    # the hover tooltip attribute.
    assert status[:20] + "…" in page
    assert 'data-tip="' + status + '"' in page
    assert "memo-tip" in page
