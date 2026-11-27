"""Holdings CSV import: POST /api/v1/holdings/import and the
dashboard's import modal.

Domain rules under test (the bulk counterpart of the manual add
in test_holdings_crud.py):

- The endpoint takes multipart form data: the CSV `file`, one
  mapping field per item (`code_col`, `account_type_col`,
  `avg_price_col`, `shares_col`, `lent_shares_col`,
  `ordered_shares_col`, `custody_type_col`) whose value is either
  a header name or a 1-based column number, and `mode`.
  `code_col` is required; the other mappings are optional. When
  any mapping names a header, the first row is the header row;
  column numbers work with or without one, and the two styles
  may be mixed per item. `mode` is one of "overwrite" / "skip" /
  "add" and defaults to "skip" (the non-destructive choice);
  any other value is a 422.
- The file is decoded as UTF-8 or Shift_JIS (auto-detected).
  Numbers may carry comma separators ("1,000"). Blank lines are
  ignored. A row's number in the result is its 1-based line
  number in the file (the header row is row 1).
- Rows are processed in order, each on its own: one bad row
  never stops the rest, and rows that succeeded stay imported.
  A row fails when its code is empty or a numeric value cannot
  be parsed. A code the stock master does not know is fetched
  from Yahoo first (the same path as manual registration);
  when the fetch fails the row fails and nothing is registered
  for it.
- Without an account mapping -- or with an empty account cell --
  the account defaults to "特定". Unmapped lent / ordered /
  custody values default to 0 / 0 / None on new rows.
- A row whose (code, account_type) has no holding yet creates
  one in every mode. When a holding already exists, `mode`
  decides: "skip" leaves it untouched, "overwrite" replaces
  the mapped fields (unmapped fields keep their values), and
  "add" adds the shares and re-averages the price (lent /
  ordered / custody are not touched by "add").
- Every creation and change is recorded in holding_events with
  kind "import" and source_type "import" (source_id NULL), the
  natural extension of the manual events (kind add/delete,
  source_type manual): creations go from (0, NULL) to the new
  state, changes carry the before/after state and the share
  delta. Skipped rows record no event.
- The answer is 200 with the summary
  {"added", "updated", "skipped", "failed", "failures"}, where
  "failures" lists {"row", "reason"} per failed row and
  "failed" is its length.
- File-level problems answer 422 and import nothing: the file
  cannot be decoded as UTF-8 or Shift_JIS, the code mapping
  does not resolve (unknown header name, column number out of
  range), or the file / code mapping is missing.
- The dashboard (GET /) offers an import button with a modal:
  the file input, one mapping input per item, the mode choice,
  and the result summary shown in a dialog after the run.
"""
from conftest import seed_stock_holding
from test_fetch import make_fake_client
from test_holdings_crud import _named_fake_client

from app import main

HEADER = "コード,口座,平均取得単価,株数,貸株数,発注数,保管区分"
FULL_MAPPING = {
    "code_col": "コード",
    "account_type_col": "口座",
    "avg_price_col": "平均取得単価",
    "shares_col": "株数",
    "lent_shares_col": "貸株数",
    "ordered_shares_col": "発注数",
    "custody_type_col": "保管区分",
}
NUMBER_MAPPING = {
    "code_col": "1",
    "account_type_col": "2",
    "avg_price_col": "3",
    "shares_col": "4",
    "lent_shares_col": "5",
    "ordered_shares_col": "6",
    "custody_type_col": "7",
}


def _csv(rows, header=HEADER):
    lines = [header] if header is not None else []
    lines += rows
    return ("\n".join(lines) + "\n").encode("utf-8")


def _import(client, data_bytes, mapping=None, mode=None):
    data = dict(mapping or {})
    if mode is not None:
        data["mode"] = mode
    return client.post(
        "/api/v1/holdings/import",
        files={"file": ("holdings.csv", data_bytes, "text/csv")},
        data=data,
    )


def _seed_stock(db, code="9999", name="テスト銘柄"):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, '東証')",
        (code, name),
    )
    db.commit()


def _seed_holding(db, code="9999", account_type="特定", avg_price=800,
                  shares=100, lent=0, ordered=0, custody=None):
    _seed_stock(db, code)
    db.execute(
        "INSERT INTO holdings(code, account_type, custody_type, avg_price,"
        " shares, lent_shares, ordered_shares)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (code, account_type, custody, avg_price, shares, lent, ordered),
    )
    db.commit()


def _holding(db, code="9999", account_type="特定"):
    return db.execute(
        "SELECT * FROM holdings WHERE code = %s AND account_type = %s",
        (code, account_type),
    ).fetchone()


def _events(db, code="9999"):
    return db.execute(
        "SELECT * FROM holding_events WHERE code = %s ORDER BY id",
        (code,),
    ).fetchall()


def _all_events(db):
    return db.execute(
        "SELECT * FROM holding_events ORDER BY id"
    ).fetchall()


def _count(db, table):
    return db.execute(
        f"SELECT count(*) c FROM {table}"
    ).fetchone()["c"]


# ---------------------------------------------------------------- openapi


def test_import_openapi_path(client, db):
    doc = client.get("/openapi.json").json()
    assert "/api/v1/holdings/import" in doc["paths"]
    assert "post" in doc["paths"]["/api/v1/holdings/import"]


# ---------------------------------------------------------------- basics


def test_import_utf8_by_header_names(client, db):
    _seed_stock(db, "9999")
    _seed_stock(db, "1111", "別銘柄")
    data = _csv([
        "9999,特定,800,100,10,2,保護",
        "1111,NISA,1500,200,0,0,",
    ])
    r = _import(client, data, FULL_MAPPING, mode="skip")
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 2
    assert body["updated"] == 0
    assert body["skipped"] == 0
    assert body["failed"] == 0
    assert body["failures"] == []
    h = _holding(db)
    assert h["shares"] == 100
    assert float(h["avg_price"]) == 800.0
    assert h["lent_shares"] == 10
    assert h["ordered_shares"] == 2
    assert h["custody_type"] == "保護"
    h2 = _holding(db, "1111", "NISA")
    assert h2["shares"] == 200
    assert float(h2["avg_price"]) == 1500.0
    assert h2["custody_type"] is None
    events = _events(db)
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "import"
    assert e["source_type"] == "import"
    assert e["source_id"] is None
    assert e["account_type"] == "特定"
    assert e["delta_shares"] == 100
    assert e["shares_before"] == 0
    assert e["avg_price_before"] is None
    assert e["shares_after"] == 100
    assert float(e["avg_price_after"]) == 800.0


def test_import_shift_jis_by_header_names(client, db):
    _seed_stock(db)
    text = HEADER + "\n9999,特定,800,100,0,0,\n"
    r = _import(client, text.encode("cp932"), FULL_MAPPING)
    assert r.status_code == 200
    assert r.json()["added"] == 1
    h = _holding(db)
    assert h["shares"] == 100
    assert float(h["avg_price"]) == 800.0


def test_import_by_column_numbers(client, db):
    _seed_stock(db)
    data = _csv(["9999,特定,800,100,10,2,保護"], header=None)
    r = _import(client, data, NUMBER_MAPPING)
    assert r.status_code == 200
    assert r.json()["added"] == 1
    h = _holding(db)
    assert h["shares"] == 100
    assert h["lent_shares"] == 10
    assert h["ordered_shares"] == 2
    assert h["custody_type"] == "保護"


def test_import_mixed_header_and_number_mapping(client, db):
    _seed_stock(db)
    data = _csv(["9999,800,100"], header="コード,単価,株数")
    mapping = {"code_col": "1", "avg_price_col": "単価", "shares_col": "株数"}
    r = _import(client, data, mapping)
    assert r.status_code == 200
    assert r.json()["added"] == 1
    h = _holding(db)
    assert h["shares"] == 100
    assert float(h["avg_price"]) == 800.0


def test_import_minimal_mapping_defaults(client, db):
    _seed_stock(db)
    data = _csv(["9999,800,100"], header=None)
    mapping = {"code_col": "1", "avg_price_col": "2", "shares_col": "3"}
    r = _import(client, data, mapping)
    assert r.status_code == 200
    assert r.json()["added"] == 1
    h = _holding(db)  # no account mapping: 特定 by default
    assert h is not None
    assert h["shares"] == 100
    assert h["lent_shares"] == 0
    assert h["ordered_shares"] == 0
    assert h["custody_type"] is None


def test_import_empty_account_cell_defaults_to_tokutei(client, db):
    _seed_stock(db, "9999")
    _seed_stock(db, "1111", "別銘柄")
    data = _csv([
        "9999,NISA,800,100,0,0,",
        "1111,,600,50,0,0,",
    ])
    r = _import(client, data, FULL_MAPPING)
    assert r.status_code == 200
    assert r.json()["added"] == 2
    assert _holding(db, "9999", "NISA") is not None
    assert _holding(db, "1111", "特定") is not None


def test_import_comma_separated_numbers(client, db):
    _seed_stock(db)
    data = _csv(['9999,特定,"1,234.5","1,000",0,0,'])
    r = _import(client, data, FULL_MAPPING)
    assert r.status_code == 200
    assert r.json()["added"] == 1
    h = _holding(db)
    assert h["shares"] == 1000
    assert float(h["avg_price"]) == 1234.5


def test_import_code_cell_is_stripped(client, db):
    _seed_stock(db)
    data = _csv([" 9999 ,特定,800,100,0,0,"])
    r = _import(client, data, FULL_MAPPING)
    assert r.status_code == 200
    assert r.json()["added"] == 1
    assert _holding(db) is not None


# ---------------------------------------------------------------- modes


def test_import_skip_mode_leaves_existing_and_adds_new(client, db):
    _seed_holding(db)  # 9999 特定: 100 @ 800
    _seed_stock(db, "1111", "別銘柄")
    data = _csv([
        "9999,特定,950,300,0,0,",
        "1111,特定,600,50,0,0,",
    ])
    r = _import(client, data, FULL_MAPPING, mode="skip")
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1
    assert body["updated"] == 0
    assert body["skipped"] == 1
    assert body["failed"] == 0
    h = _holding(db)
    assert h["shares"] == 100
    assert float(h["avg_price"]) == 800.0
    # A skipped row records no event; the new row does.
    assert _events(db) == []
    assert len(_events(db, "1111")) == 1


def test_import_default_mode_is_skip(client, db):
    _seed_holding(db)
    data = _csv(["9999,特定,950,300,0,0,"])
    r = _import(client, data, FULL_MAPPING)  # no mode field
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == 1
    assert body["updated"] == 0
    assert _holding(db)["shares"] == 100
    assert _all_events(db) == []


def test_import_overwrite_mode_replaces_mapped_fields(client, db):
    _seed_holding(db, lent=5, custody="保護")
    mapping = {
        k: v for k, v in FULL_MAPPING.items() if k != "custody_type_col"
    }
    data = _csv(["9999,特定,950,200,7,1,"])
    r = _import(client, data, mapping, mode="overwrite")
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0
    h = _holding(db)
    assert h["shares"] == 200
    assert float(h["avg_price"]) == 950.0
    assert h["lent_shares"] == 7
    assert h["ordered_shares"] == 1
    # custody was not mapped: the existing value survives.
    assert h["custody_type"] == "保護"
    events = _events(db)
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "import"
    assert e["source_type"] == "import"
    assert e["delta_shares"] == 100
    assert e["shares_before"] == 100
    assert float(e["avg_price_before"]) == 800.0
    assert e["shares_after"] == 200
    assert float(e["avg_price_after"]) == 950.0


def test_import_add_mode_merges_with_weighted_average(client, db):
    _seed_holding(db, lent=5)  # 100 @ 800
    data = _csv(["9999,特定,1000,100,3,0,"])
    r = _import(client, data, FULL_MAPPING, mode="add")
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 0
    assert body["updated"] == 1
    h = _holding(db)
    assert h["shares"] == 200
    assert float(h["avg_price"]) == 900.0
    # "add" merges shares and price only: lent shares stay as
    # they were even though the CSV row carries a value.
    assert h["lent_shares"] == 5
    events = _events(db)
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "import"
    assert e["delta_shares"] == 100
    assert e["shares_before"] == 100
    assert float(e["avg_price_before"]) == 800.0
    assert e["shares_after"] == 200
    assert float(e["avg_price_after"]) == 900.0


def test_import_add_mode_creates_missing_holding(client, db):
    _seed_stock(db)
    data = _csv(["9999,特定,800,100,0,0,"])
    r = _import(client, data, FULL_MAPPING, mode="add")
    assert r.status_code == 200
    assert r.json()["added"] == 1
    assert _holding(db) is not None


def test_import_rows_apply_in_order_within_one_file(client, db):
    _seed_stock(db)
    data = _csv([
        "9999,特定,800,100,0,0,",
        "9999,特定,1000,100,0,0,",
    ])
    r = _import(client, data, FULL_MAPPING, mode="add")
    assert r.status_code == 200
    body = r.json()
    # The second row sees the holding the first row created.
    assert body["added"] == 1
    assert body["updated"] == 1
    h = _holding(db)
    assert h["shares"] == 200
    assert float(h["avg_price"]) == 900.0
    assert len(_events(db)) == 2


# ---------------------------------------------------------------- row failures


def test_import_row_failures_do_not_stop_other_rows(client, db):
    _seed_stock(db, "1111", "銘柄甲")
    _seed_stock(db, "2222", "銘柄乙")
    data = _csv([
        "1111,特定,500,100,0,0,",   # row 2: ok
        ",特定,500,100,0,0,",        # row 3: empty code
        "2222,特定,500,abc,0,0,",   # row 4: shares not a number
        "2222,特定,600,50,0,0,",    # row 5: ok
    ])
    r = _import(client, data, FULL_MAPPING)
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 2
    assert body["failed"] == 2
    assert body["failed"] == len(body["failures"])
    assert [f["row"] for f in body["failures"]] == [3, 4]
    assert all(f["reason"] for f in body["failures"])
    assert _holding(db, "1111")["shares"] == 100
    assert _holding(db, "2222")["shares"] == 50
    assert len(_events(db, "1111")) == 1
    assert len(_events(db, "2222")) == 1


def test_import_unknown_code_fetches_stock(client, db, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    data = _csv(["7777,特定,800,100,0,0,"])
    r = _import(client, data, FULL_MAPPING)
    assert r.status_code == 200
    assert r.json()["added"] == 1
    stock = db.execute(
        "SELECT name FROM stocks WHERE code = '7777'"
    ).fetchone()
    assert stock["name"] == "テスト候補(株)"
    assert _holding(db, "7777") is not None
    assert len(_events(db, "7777")) == 1


def test_import_unknown_code_fetch_failure_fails_only_that_row(
        client, db, monkeypatch):
    monkeypatch.setattr(
        main.httpx, "AsyncClient", make_fake_client(fail_codes=("0000",))
    )
    _seed_stock(db)
    data = _csv([
        "0000,特定,800,100,0,0,",   # row 2: fetch fails
        "9999,特定,800,100,0,0,",   # row 3: ok
    ])
    r = _import(client, data, FULL_MAPPING)
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1
    assert body["failed"] == 1
    assert body["failures"][0]["row"] == 2
    assert body["failures"][0]["reason"]
    assert _count(db, "stocks") == 1  # only the seeded 9999
    assert _holding(db, "0000") is None
    assert _holding(db) is not None


# ---------------------------------------------------------------- file errors


def test_import_unknown_header_name_is_422(client, db):
    _seed_stock(db)
    data = _csv(["9999,特定,800,100,0,0,"])
    mapping = dict(FULL_MAPPING, code_col="証券コード")
    r = _import(client, data, mapping)
    assert r.status_code == 422
    assert _count(db, "holdings") == 0
    assert _all_events(db) == []


def test_import_code_column_out_of_range_is_422(client, db):
    _seed_stock(db)
    data = _csv(["9999,800,100"], header=None)
    mapping = {"code_col": "9", "avg_price_col": "2", "shares_col": "3"}
    r = _import(client, data, mapping)
    assert r.status_code == 422
    assert _count(db, "holdings") == 0
    assert _all_events(db) == []


def test_import_undecodable_file_is_422(client, db):
    r = _import(client, b"\xff\xff\xff\xff\xff", FULL_MAPPING)
    assert r.status_code == 422
    assert _count(db, "holdings") == 0


def test_import_missing_code_mapping_is_422(client, db):
    data = _csv(["9999,特定,800,100,0,0,"])
    mapping = {
        k: v for k, v in FULL_MAPPING.items() if k != "code_col"
    }
    r = _import(client, data, mapping)
    assert r.status_code == 422
    assert _count(db, "holdings") == 0


def test_import_missing_file_is_422(client, db):
    r = client.post("/api/v1/holdings/import", data=dict(FULL_MAPPING))
    assert r.status_code == 422
    assert _count(db, "holdings") == 0


def test_import_invalid_mode_is_422(client, db):
    _seed_stock(db)
    data = _csv(["9999,特定,800,100,0,0,"])
    r = _import(client, data, FULL_MAPPING, mode="replace")
    assert r.status_code == 422
    assert _count(db, "holdings") == 0
    assert _all_events(db) == []


def test_import_truly_undecodable_file_is_422(client, db):
    # A truncated Shift_JIS lead byte decodes as neither UTF-8 nor
    # cp932. (0xFF alone would slip through cp932 as a private-use
    # character and only fail later, at header resolution.)
    r = _import(client, b"\x82\x20\x82", FULL_MAPPING)
    assert r.status_code == 422
    assert _count(db, "holdings") == 0


def test_import_blank_code_mapping_is_422(client, db):
    # A whitespace-only code_col is a present-but-empty mapping:
    # stripped away, it leaves the required code mapping missing.
    _seed_stock(db)
    data = _csv(["9999,特定,800,100,0,0,"])
    mapping = dict(FULL_MAPPING, code_col=" ")
    r = _import(client, data, mapping)
    assert r.status_code == 422
    assert _count(db, "holdings") == 0


def test_import_header_only_file_imports_nothing(client, db):
    r = _import(client, _csv([]), FULL_MAPPING)
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 0
    assert body["updated"] == 0
    assert body["skipped"] == 0
    assert body["failed"] == 0
    assert body["failures"] == []


# ------------------------------------------------- unmapped core columns
# The brief left rows without a shares / avg_price mapping to the
# implementer; the chosen rule (documented in ARCHITECTURE.md) is:
# shares is required for every row, and avg_price is required
# whenever a row creates a holding or merges in "add" mode. An
# "overwrite" of an existing holding keeps its stored price when
# avg_price is unmapped.


def test_import_without_shares_mapping_fails_every_row(client, db):
    _seed_stock(db)
    data = _csv(["9999,800"], header=None)
    mapping = {"code_col": "1", "avg_price_col": "2"}
    r = _import(client, data, mapping)
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 0
    assert body["failed"] == 1
    assert body["failures"][0]["row"] == 1
    assert body["failures"][0]["reason"]
    assert _count(db, "holdings") == 0
    assert _all_events(db) == []


def test_import_without_avg_price_mapping(client, db):
    _seed_holding(db)  # 9999 特定: 100 @ 800
    _seed_stock(db, "1111", "別銘柄")
    data = _csv([
        "9999,200",
        "1111,50",
    ], header=None)
    mapping = {"code_col": "1", "shares_col": "2"}
    r = _import(client, data, mapping, mode="overwrite")
    assert r.status_code == 200
    body = r.json()
    # The existing row is overwritten without a price mapping: the
    # shares are replaced and the stored price survives. The new
    # code cannot be created without a price and fails as a row.
    assert body["updated"] == 1
    assert body["failed"] == 1
    assert body["failures"][0]["row"] == 2
    h = _holding(db)
    assert h["shares"] == 200
    assert float(h["avg_price"]) == 800.0
    assert _holding(db, "1111") is None


def test_import_row_value_edge_cases(client, db):
    _seed_stock(db, "1111", "銘柄甲")
    _seed_stock(db, "2222", "銘柄乙")
    _seed_stock(db, "3333", "銘柄丙")
    data = _csv([
        "1111,特定,abc,100,0,0,",    # row 2: price not a number
        "2222,特定,500,-5,0,0,",     # row 3: negative shares
        "3333,特定,500,100,,,",      # row 4: blank lent / ordered
        "1111,特定,-100,100,0,0,",   # row 5: negative price
        "2222,特定,NaN,100,0,0,",    # row 6: non-finite price
    ])
    r = _import(client, data, FULL_MAPPING)
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1
    assert body["failed"] == 4
    assert [f["row"] for f in body["failures"]] == [2, 3, 5, 6]
    assert all(f["reason"] for f in body["failures"])
    # Blank auxiliary counts import as 0, like unmapped ones.
    h = _holding(db, "3333")
    assert h["lent_shares"] == 0
    assert h["ordered_shares"] == 0
    assert _holding(db, "1111") is None
    assert _holding(db, "2222") is None


# ---------------------------------------------------------------- page


def test_dashboard_import_controls(client, db):
    seed_stock_holding(db)
    r = client.get("/")
    assert r.status_code == 200
    page = r.text
    assert "インポート" in page
    # The modal: file input, one mapping input per item, the
    # mode choice, all posted to the import endpoint.
    assert 'type="file"' in page
    assert 'name="file"' in page
    for field in ("code_col", "account_type_col", "avg_price_col",
                  "shares_col", "lent_shares_col", "ordered_shares_col",
                  "custody_type_col", "mode"):
        assert f'name="{field}"' in page, field
    for value in ("overwrite", "skip", "add"):
        assert f'value="{value}"' in page, value
    assert "/api/v1/holdings/import" in page
