"""Shareholder-benefit memos （優待メモ）: the display rule, the
/api/v1/benefit-memos endpoints, and the /benefit-memos page.

Domain rule under test: a stock appears on the 優待メモ list only when
it has a dividend_benefits row with a benefit (benefit_value > 0 or a
non-empty benefit_desc) AND it is currently held. The memo itself is
keyed by stock code and is upserted with partial updates.
"""
from conftest import seed_stock_holding

FULL_MEMO = {
    "shareholder_no": "000123456",
    "deadline_1": "2027-04-30",
    "deadline_2": "2027-10-31",
    "delivery_waiting": True,
    "status": "カタログ選び",
    "form_arrival_1": "2026-05-11",
    "form_arrival_2": "2026-10-19",
    "item_arrival_1": "2026-06-01",
    "item_arrival_2": "2026-11-20",
}

MEMO_FIELDS = (
    "shareholder_no", "deadline_1", "deadline_2", "delivery_waiting",
    "status", "form_arrival_1", "form_arrival_2",
    "item_arrival_1", "item_arrival_2",
)


def _seed_targets(db):
    """Seed the display-rule matrix; returns the target codes."""
    # Benefit by value, held -> target.
    seed_stock_holding(db, code="1001", name="優待あり銘柄", benefit=5000)
    # Benefit by description only (value 0), held -> target.
    seed_stock_holding(db, code="1002", name="説明のみ銘柄", benefit=0)
    db.execute(
        "UPDATE dividend_benefits SET benefit_desc='自社製品詰め合わせ'"
        " WHERE code='1002'"
    )
    # Master row without any benefit, held -> not a target.
    seed_stock_holding(db, code="1003", name="優待なし銘柄", benefit=0)
    # Benefit by value but NOT held -> not a target.
    db.execute(
        "INSERT INTO stocks(code, name, market)"
        " VALUES ('1004', '非保有の優待銘柄', '東証')"
    )
    db.execute(
        "INSERT INTO dividend_benefits(code, dividend_forecast,"
        " dividend_auto, benefit_value) VALUES ('1004', 30, 30, 3000)"
    )
    # Held but no master row at all -> not a target.
    seed_stock_holding(db, code="1005", name="マスタなし銘柄", dividend=None)
    # Empty-string description does not count as a benefit.
    seed_stock_holding(db, code="1006", name="空説明銘柄", benefit=0)
    db.execute(
        "UPDATE dividend_benefits SET benefit_desc='' WHERE code='1006'"
    )
    db.commit()
    return {"1001", "1002"}


def test_benefit_memos_openapi(client):
    doc = client.get("/openapi.json").json()
    paths = doc["paths"]
    assert "/api/v1/benefit-memos" in paths
    assert "/api/v1/benefit-memos/{code}" in paths
    assert "get" in paths["/api/v1/benefit-memos"]
    assert "put" in paths["/api/v1/benefit-memos/{code}"]
    schemas = doc["components"]["schemas"]
    for name in ("BenefitMemoIn", "BenefitMemoOut"):
        assert name in schemas, name
    put = paths["/api/v1/benefit-memos/{code}"]["put"]
    ref = put["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/BenefitMemoIn")
    resp_ref = (
        put["responses"]["200"]["content"]["application/json"]
        ["schema"]["$ref"]
    )
    assert resp_ref.endswith("/BenefitMemoOut")


def test_benefit_memos_list_targets_only(client, db):
    targets = _seed_targets(db)
    r = client.get("/api/v1/benefit-memos")
    assert r.status_code == 200
    rows = r.json()
    assert {row["code"] for row in rows} == targets
    by_code = {row["code"]: row for row in rows}
    assert by_code["1001"]["name"] == "優待あり銘柄"
    assert by_code["1002"]["name"] == "説明のみ銘柄"
    # No memo registered yet: every memo field is null, and
    # delivery_waiting falls back to false.
    for row in rows:
        for field in MEMO_FIELDS:
            if field == "delivery_waiting":
                assert row[field] is False
            else:
                assert row[field] is None, field


def test_benefit_memos_list_empty_without_targets(client, db):
    seed_stock_holding(db, code="1003", name="優待なし銘柄", benefit=0)
    r = client.get("/api/v1/benefit-memos")
    assert r.status_code == 200
    assert r.json() == []


def test_benefit_memo_put_creates_and_list_reflects(client, db):
    _seed_targets(db)
    r = client.put("/api/v1/benefit-memos/1001", json=FULL_MEMO)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "1001"
    assert body["name"] == "優待あり銘柄"
    for field, value in FULL_MEMO.items():
        assert body[field] == value, field
    # The list endpoint returns the stored memo.
    listed = {
        row["code"]: row
        for row in client.get("/api/v1/benefit-memos").json()
    }
    for field, value in FULL_MEMO.items():
        assert listed["1001"][field] == value, field
    # The other target still has no memo.
    assert listed["1002"]["shareholder_no"] is None
    assert listed["1002"]["delivery_waiting"] is False
    # And the row is really in the new table.
    row = db.execute(
        "SELECT * FROM benefit_memos WHERE code='1001'"
    ).fetchone()
    assert row["shareholder_no"] == "000123456"
    assert row["deadline_1"].isoformat() == "2027-04-30"
    assert row["deadline_2"].isoformat() == "2027-10-31"
    assert row["delivery_waiting"] is True
    assert row["status"] == "カタログ選び"
    assert row["form_arrival_1"].isoformat() == "2026-05-11"
    assert row["item_arrival_2"].isoformat() == "2026-11-20"


def test_benefit_memo_partial_update_keeps_unspecified_fields(client, db):
    _seed_targets(db)
    client.put("/api/v1/benefit-memos/1001", json=FULL_MEMO)
    # Sending only one field must keep the rest (partial update).
    r = client.put(
        "/api/v1/benefit-memos/1001", json={"status": "申込済み"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "申込済み"
    for field, value in FULL_MEMO.items():
        if field != "status":
            assert body[field] == value, field
    row = db.execute(
        "SELECT * FROM benefit_memos WHERE code='1001'"
    ).fetchone()
    assert row["shareholder_no"] == "000123456"
    assert row["deadline_1"].isoformat() == "2027-04-30"
    assert row["delivery_waiting"] is True


def test_benefit_memo_explicit_null_clears_only_that_field(client, db):
    _seed_targets(db)
    client.put("/api/v1/benefit-memos/1001", json=FULL_MEMO)
    # An explicit null clears that field, and only that field.
    r = client.put(
        "/api/v1/benefit-memos/1001", json={"deadline_1": None}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deadline_1"] is None
    assert body["deadline_2"] == "2027-10-31"
    assert body["shareholder_no"] == "000123456"
    row = db.execute(
        "SELECT deadline_1, deadline_2 FROM benefit_memos"
        " WHERE code='1001'"
    ).fetchone()
    assert row["deadline_1"] is None
    assert row["deadline_2"].isoformat() == "2027-10-31"


def test_benefit_memo_put_unknown_code_is_404(client, db):
    _seed_targets(db)
    r = client.put(
        "/api/v1/benefit-memos/0000", json={"status": "対象なし"}
    )
    assert r.status_code == 404
    assert db.execute(
        "SELECT count(*) c FROM benefit_memos WHERE code='0000'"
    ).fetchone()["c"] == 0


def test_benefit_memo_put_invalid_date_is_422(client, db):
    _seed_targets(db)
    for bad in ("2027-13-01", "not-a-date"):
        r = client.put(
            "/api/v1/benefit-memos/1001", json={"deadline_1": bad}
        )
        assert r.status_code == 422, bad
    # Nothing was stored by the rejected requests.
    assert db.execute(
        "SELECT count(*) c FROM benefit_memos WHERE code='1001'"
    ).fetchone()["c"] == 0


def test_benefit_memo_put_for_non_target_stock_stays_unlisted(client, db):
    _seed_targets(db)
    # 1003 is a real stock (held, but no benefit): the upsert itself is
    # keyed on the stock, so it succeeds, yet the display rule keeps it
    # off the list.
    r = client.put(
        "/api/v1/benefit-memos/1003", json={"status": "優待なしメモ"}
    )
    assert r.status_code == 200
    codes = {
        row["code"] for row in client.get("/api/v1/benefit-memos").json()
    }
    assert "1003" not in codes
    assert db.execute(
        "SELECT count(*) c FROM benefit_memos WHERE code='1003'"
    ).fetchone()["c"] == 1


def test_benefit_memos_page_lists_targets_only(client, db):
    _seed_targets(db)
    r = client.get("/benefit-memos")
    assert r.status_code == 200
    page = r.text
    assert ">優待メモ</h2>" in page
    # Display targets are listed by their DB company name.
    assert "優待あり銘柄" in page
    assert "説明のみ銘柄" in page
    # Non-targets never appear: no benefit, not held, no master row,
    # empty benefit description.
    for name in ("優待なし銘柄", "非保有の優待銘柄", "マスタなし銘柄", "空説明銘柄"):
        assert name not in page
    # The sidebar links to the new page.
    assert 'href="/benefit-memos"' in page
    assert '<span class="side-label">優待メモ</span>' in page


def test_benefit_memos_page_shows_memo_values(client, db):
    _seed_targets(db)
    client.put("/api/v1/benefit-memos/1001", json=FULL_MEMO)
    page = client.get("/benefit-memos").text
    assert "000123456" in page  # shareholder number
    assert "カタログ選び" in page  # status
    assert "2027-04-30" in page or "2027/04/30" in page  # deadline_1


def test_benefit_memo_empty_put_creates_default_row(client, db):
    _seed_targets(db)
    # An empty body still creates the memo row, with every field at
    # its default: null everywhere, delivery_waiting false.
    r = client.put("/api/v1/benefit-memos/1001", json={})
    assert r.status_code == 200
    body = r.json()
    for field in MEMO_FIELDS:
        if field == "delivery_waiting":
            assert body[field] is False
        else:
            assert body[field] is None, field
    row = db.execute(
        "SELECT * FROM benefit_memos WHERE code='1001'"
    ).fetchone()
    assert row is not None
    assert row["delivery_waiting"] is False
    for field in MEMO_FIELDS:
        if field != "delivery_waiting":
            assert row[field] is None, field


def test_benefit_memo_null_delivery_waiting_clears_to_false(client, db):
    _seed_targets(db)
    client.put("/api/v1/benefit-memos/1001", json=FULL_MEMO)
    # delivery_waiting is NOT NULL, so an explicit null clears the
    # flag to false rather than storing null; other fields stay.
    r = client.put(
        "/api/v1/benefit-memos/1001", json={"delivery_waiting": None}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["delivery_waiting"] is False
    assert body["status"] == FULL_MEMO["status"]
    assert body["shareholder_no"] == FULL_MEMO["shareholder_no"]
    row = db.execute(
        "SELECT delivery_waiting FROM benefit_memos WHERE code='1001'"
    ).fetchone()
    assert row["delivery_waiting"] is False


def test_benefit_memos_page_date_cells_use_nowrap_class(client, db):
    _seed_targets(db)
    client.put("/api/v1/benefit-memos/1001", json=FULL_MEMO)
    page = client.get("/benefit-memos").text
    # Every date cell carries the .date (white-space: nowrap) class so a
    # date never wraps onto two lines.
    for field in ("deadline_1", "deadline_2", "form_arrival_1",
                  "form_arrival_2", "item_arrival_1", "item_arrival_2"):
        assert '<td class="date">%s</td>' % FULL_MEMO[field] in page
