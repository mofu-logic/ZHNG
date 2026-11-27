"""Endpoint tests against a real (test) PostgreSQL database.

Covers the pages' rendering, the KPI math on the dashboard, the trade
history create/delete flows incl. status auto-derivation and profit
auto-calculation, the settings derivation, and the data page's run
result formatting.
"""
import re
from datetime import date, datetime, timezone

import pytest

from zoneinfo import ZoneInfo

from conftest import seed_settings, seed_stock_holding


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_dashboard_kpi_math(client, db):
    seed_settings(db)
    seed_stock_holding(db)  # avg 800 x 100 shares, price 1000, div 40
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "テスト銘柄" in body
    assert "100,000" in body  # evaluation value = 1,000 x 100
    assert "20,000" in body  # unrealised pnl = (1,000 - 800) x 100


def test_dashboard_empty(client, db):
    seed_settings(db)
    r = client.get("/")
    assert r.status_code == 200


def test_history_buy_create_and_delete(client, db):
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "avg_price": 900,
            "shares": 50,
            "purchase_date": "2026-11-20",
            "status": "一部約定",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == "9999"
    assert body["status"] == "一部約定"
    assert body["avg_price"] == 900.0
    row = db.execute("SELECT * FROM buy_lots").fetchone()
    assert row["code"] == "9999"

    assert client.delete(f"/api/v1/buys/{body['id']}").status_code == 204
    assert client.delete(f"/api/v1/buys/{body['id']}").status_code == 404
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0


def test_history_buy_invalid_status_falls_back(client, db):
    r = client.post(
        "/api/v1/buys",
        json={"code": "9999", "name": "x", "status": "変な値"},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "約定済"


@pytest.mark.parametrize(
    "executed,expected_status,expected_profit",
    [
        (0, "注文中", None),  # nothing executed yet, no profit
        (50, "一部約定", 5000.0),  # (200 - 100) x 50
        (100, "約定済", 10000.0),  # fully executed
        (150, "約定済", 15000.0),  # over-execution still counts as done
    ],
)
def test_sell_auto_status_and_profit(
    client, db, executed, expected_status, expected_profit
):
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "buy_price": 100,
            "sell_price": 200,
            "sell_shares": 100,
            "executed_shares": executed,
            "status": "自動",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == expected_status
    assert body["profit"] == expected_profit
    client.delete(f"/api/v1/sales/{body['id']}")


def test_sell_explicit_profit_and_status_win(client, db):
    r = client.post(
        "/api/v1/sales",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "buy_price": 100,
            "sell_price": 200,
            "sell_shares": 100,
            "executed_shares": 0,
            "profit": 1234,
            "status": "約定済",
        },
    )
    body = r.json()
    assert body["profit"] == 1234.0
    assert body["status"] == "約定済"
    client.delete(f"/api/v1/sales/{body['id']}")


def test_sell_delete(client, db):
    r = client.post("/api/v1/sales", json={"code": "9999", "name": "x"})
    sale_id = r.json()["id"]
    assert client.delete(f"/api/v1/sales/{sale_id}").status_code == 204
    assert client.delete(f"/api/v1/sales/{sale_id}").status_code == 404
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0


def test_history_page_renders(client, db):
    seed_stock_holding(db, dividend=None, price=None)
    client.post("/api/v1/buys", json={"code": "9999", "name": "テスト銘柄"})
    r = client.get("/history")
    assert r.status_code == 200
    assert "テスト銘柄" in r.text


def test_history_page_date_cells_use_nowrap_class(client, db):
    seed_stock_holding(db, dividend=None, price=None)
    client.post(
        "/api/v1/buys",
        json={"code": "9999", "name": "テスト銘柄",
              "purchase_date": "2026-01-05"},
    )
    client.post(
        "/api/v1/sales",
        json={"code": "9999", "name": "テスト銘柄",
              "order_start_date": "2026-02-01",
              "executed_date": "2026-02-03"},
    )
    page = client.get("/history").text
    # Every date cell carries the .date (white-space: nowrap) class so a
    # date never wraps onto two lines.
    assert '<td class="date">2026-01-05</td>' in page
    assert '<td class="date">2026-02-01</td>' in page
    assert '<td class="date">2026-02-03</td>' in page


def test_history_page_numeric_inputs_have_min_zero(client, db):
    # The API rejects negative quantities/prices with 422, but the form
    # must stop the user from typing them in the first place: every
    # numeric input in the buy and sell forms carries min="0".
    # The sell form no longer asks for buy_price or profit at all --
    # the buy price is filled from the holding's average and the
    # profit is always derived -- so those inputs must be absent
    # rather than merely unbounded below.
    page = client.get("/history").text
    inputs = {}
    for tag in re.findall(r"<input[^>]*>", page):
        m = re.search(r'name="([^"]+)"', tag)
        if m:
            inputs.setdefault(m.group(1), []).append(tag)
    for field in ("avg_price", "shares", "sell_price",
                  "sell_shares", "executed_shares"):
        assert field in inputs, field
        for tag in inputs[field]:
            assert 'min="0"' in tag, tag
    assert "buy_price" not in inputs
    assert "profit" not in inputs


def test_settings_monthly_target_is_derived(client, db):
    # annual 1,200,000 x (1 - 0.20315) / 12 = 79,685 exactly at seed values;
    # put new numbers and check the stored derivation.
    r = client.put(
        "/api/v1/settings",
        json={
            "tax_rate_pct": 20.315,
            "target_dividend_total": 1370347,
            "target_yield_buy_pct": 3.0,
            "target_yield_now_pct": 3.5,
            "target_yield_portfolio_pct": 4.0,
        },
    )
    assert r.status_code == 200
    # 1,370,347 x (1 - 0.20315) / 12 = 90,996.79... -> round half up
    assert r.json()["target_monthly_income"] == 90997
    rows = {}
    for row in db.execute("SELECT key, value FROM settings").fetchall():
        try:
            rows[row["key"]] = float(row["value"])
        except ValueError:
            pass  # non-numeric settings (otel_enabled / otel_endpoint)
    assert rows["target_monthly_income"] == 90997.0
    assert rows["tax_rate"] == pytest.approx(0.20315)
    assert rows["target_yield_portfolio"] == pytest.approx(0.04)
    assert client.get("/api/v1/settings").json()["target_monthly_income"] == 90997


def test_settings_page_renders(client, db):
    seed_settings(db)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "79685" in r.text  # derived monthly target from seed values


def test_master_update(client, db):
    seed_stock_holding(db)
    r = client.put(
        "/api/v1/dividend-benefits/9999",
        json={
            "benefit_value": 6000,
            "benefit_desc": "食事券",
            "judgment_on": "2026-11-26",
            "judgment_note": "継続保有",
            "funding_target": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["benefit_value"] == 6000.0
    assert body["benefit_desc"] == "食事券"
    assert body["judgment_on"] == "2026-11-26"
    assert body["funding_target"] is True
    row = db.execute(
        "SELECT * FROM dividend_benefits WHERE code = '9999'"
    ).fetchone()
    assert float(row["benefit_value"]) == 6000.0
    assert row["judgment_note"] == "継続保有"
    # dividend itself is auto-fetched only; the API must not touch it
    assert float(row["dividend_forecast"]) == 40.0
    r = client.put("/api/v1/dividend-benefits/0000", json={"benefit_value": 1})
    assert r.status_code == 404


def test_master_partial_update_keeps_unspecified_fields(client, db):
    seed_stock_holding(db)
    # Full send first, as the UI does today.
    r = client.put(
        "/api/v1/dividend-benefits/9999",
        json={
            "benefit_value": 5000,
            "benefit_desc": "食事券",
            "judgment_on": "2026-11-26",
            "judgment_note": "継続保有",
            "funding_target": True,
            "policy": "配当・優待",
        },
    )
    assert r.status_code == 200
    # Sending only one field must keep the rest (partial update).
    r = client.put(
        "/api/v1/dividend-benefits/9999", json={"benefit_value": 6000}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["benefit_value"] == 6000.0
    assert body["benefit_desc"] == "食事券"
    assert body["judgment_on"] == "2026-11-26"
    assert body["judgment_note"] == "継続保有"
    assert body["funding_target"] is True
    assert body["policy"] == "配当・優待"
    row = db.execute(
        "SELECT * FROM dividend_benefits WHERE code = '9999'"
    ).fetchone()
    assert float(row["benefit_value"]) == 6000.0
    assert row["policy"] == "配当・優待"
    assert row["judgment_note"] == "継続保有"


def test_master_explicit_null_clears_only_that_field(client, db):
    seed_stock_holding(db)
    client.put(
        "/api/v1/dividend-benefits/9999",
        json={
            "benefit_value": 5000,
            "benefit_desc": "食事券",
            "judgment_on": "2026-11-26",
            "judgment_note": "継続保有",
            "funding_target": True,
            "policy": "配当・優待",
        },
    )
    # An explicit null clears that field, and only that field.
    r = client.put(
        "/api/v1/dividend-benefits/9999", json={"policy": None}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["policy"] is None
    assert body["benefit_value"] == 5000.0
    assert body["benefit_desc"] == "食事券"
    assert body["judgment_note"] == "継続保有"
    assert body["funding_target"] is True
    row = db.execute(
        "SELECT policy, judgment_note FROM dividend_benefits"
        " WHERE code = '9999'"
    ).fetchone()
    assert row["policy"] is None
    assert row["judgment_note"] == "継続保有"


def test_master_empty_update_keeps_all_fields(client, db):
    seed_stock_holding(db)
    client.put(
        "/api/v1/dividend-benefits/9999",
        json={
            "benefit_value": 5000,
            "benefit_desc": "食事券",
            "judgment_on": "2026-11-26",
            "judgment_note": "継続保有",
            "funding_target": True,
            "policy": "配当・優待",
        },
    )
    # An empty body names no field at all: the update degenerates to
    # an existence check and the row must come back unchanged.
    r = client.put("/api/v1/dividend-benefits/9999", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["benefit_value"] == 5000.0
    assert body["benefit_desc"] == "食事券"
    assert body["judgment_on"] == "2026-11-26"
    assert body["judgment_note"] == "継続保有"
    assert body["funding_target"] is True
    assert body["policy"] == "配当・優待"
    row = db.execute(
        "SELECT * FROM dividend_benefits WHERE code = '9999'"
    ).fetchone()
    assert float(row["benefit_value"]) == 5000.0
    assert row["benefit_desc"] == "食事券"
    assert row["judgment_note"] == "継続保有"
    assert row["policy"] == "配当・優待"
    # dividend itself is auto-fetched only; still untouched
    assert float(row["dividend_forecast"]) == 40.0
    # The existence check still applies: unknown code is a 404.
    assert client.put(
        "/api/v1/dividend-benefits/0000", json={}
    ).status_code == 404


def test_master_page_renders(client, db):
    seed_stock_holding(db)
    r = client.get("/master")
    assert r.status_code == 200
    assert "テスト銘柄" in r.text


def _insert_run(db, **kw):
    cols = {
        "started_at": datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 11, 26, 1, 37, tzinfo=timezone.utc),
        "ok_count": None,
        "ng_count": None,
        "note": "batch",
        "total_count": 63,
        "done_count": 63,
    }
    cols.update(kw)
    db.execute(
        "INSERT INTO fetch_runs(started_at, finished_at, ok_count, ng_count,"
        " note, total_count, done_count) VALUES (%(started_at)s,"
        " %(finished_at)s, %(ok_count)s, %(ng_count)s, %(note)s,"
        " %(total_count)s, %(done_count)s)",
        cols,
    )
    db.commit()


def test_data_page_run_results(client, db):
    _insert_run(db, finished_at=None)  # running -> result "―"
    _insert_run(db, ok_count=None)  # finished without counts -> 中断
    _insert_run(db, ok_count=63, ng_count=0)  # success
    r = client.get("/data")
    assert r.status_code == 200
    assert "成功 63件 ／ 失敗 0件" in r.text
    assert "中断" in r.text
    assert "自動取得" in r.text


def test_data_page_date_cells_use_nowrap_class(client, db):
    db.execute(
        "INSERT INTO stocks(code, name, market)"
        " VALUES ('9999', 'テスト銘柄', '東証')"
    )
    db.execute(
        "INSERT INTO price_points(code, captured_at, price)"
        " VALUES ('9999', '2026-11-25 09:30:00+09', 1000)"
    )
    db.execute(
        "INSERT INTO daily_snapshots(code, snap_date, close_price)"
        " VALUES ('9999', '2026-11-25', 1000)"
    )
    db.commit()
    page = client.get("/data").text
    # The snapshot date and the latest-price timestamp carry the .date
    # (white-space: nowrap) class so they never wrap onto two lines.
    assert '<td class="date">2026-11-25</td>' in page
    captured = db.execute(
        "SELECT captured_at FROM price_points WHERE code='9999'"
    ).fetchone()["captured_at"]
    # The app reads timestamps back in JST (its session timezone).
    expected = captured.astimezone(ZoneInfo("Asia/Tokyo")).strftime(
        "%Y/%m/%d %H:%M"
    )
    assert '<td class="date">%s</td>' % expected in page


def test_fetch_status_api(client, db):
    _insert_run(db, ok_count=63, ng_count=0)
    r = client.get("/api/fetch-status")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 63
    assert body["done"] == 63
    assert body["ok"] == 63
    assert body["finished"] is True


def test_fetch_status_no_runs(client, db):
    r = client.get("/api/fetch-status")
    assert r.json() == {"running": False}


def test_summary_single_snapshot_fallbacks(client, db):
    # Only one snapshot date so far: the old dividend yield is expressed
    # at the latest price, and movers fall back to each snapshot's own
    # prev_close.
    seed_stock_holding(db)
    run_id = db.execute(
        "INSERT INTO fetch_runs(started_at, finished_at, ok_count, ng_count,"
        " note, total_count, done_count) VALUES (now(), now(), 1, 0,"
        " 'batch', 1, 1) RETURNING id"
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO daily_snapshots(code, snap_date, close_price,"
        " prev_close, dividend_yield)"
        " VALUES ('9999', '2026-11-25', 1000, 900, 5.0)"
    )
    db.execute(
        "INSERT INTO dividend_changes(run_id, code, old_div, new_div)"
        " VALUES (%s, '9999', 40, 50)",
        (run_id,),
    )
    db.commit()
    r = client.get("/summary")
    assert r.status_code == 200
    assert "4.00% → 5.00%" in r.text  # 40 / 1,000 at the latest price
    assert "+11.11%" in r.text  # mover vs own prev_close


def test_summary_change_without_any_price(client, db):
    # A dividend change whose snapshot carries neither a yield nor a
    # close price: both yields render as placeholders.
    seed_stock_holding(db)
    run_id = db.execute(
        "INSERT INTO fetch_runs(started_at, finished_at, ok_count, ng_count,"
        " note, total_count, done_count) VALUES (now(), now(), 1, 0,"
        " 'batch', 1, 1) RETURNING id"
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO daily_snapshots(code, snap_date)"
        " VALUES ('9999', '2026-11-25')"
    )
    db.execute(
        "INSERT INTO dividend_changes(run_id, code, old_div, new_div)"
        " VALUES (%s, '9999', 40, 50)",
        (run_id,),
    )
    db.commit()
    r = client.get("/summary")
    assert r.status_code == 200
    assert "― → ―" in r.text


def test_summary_falls_back_to_latest_finished_run(client, db):
    # No finished batch run yet: the page uses the latest finished run
    # of any kind instead of failing.
    _insert_run(db, ok_count=5, ng_count=0, note="manual",
                total_count=5, done_count=5)
    r = client.get("/summary")
    assert r.status_code == 200


def test_summary_page(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    run_id = db.execute(
        "INSERT INTO fetch_runs(started_at, finished_at, ok_count, ng_count,"
        " note, total_count, done_count) VALUES (now(), now(), 2, 0,"
        " 'batch', 2, 2) RETURNING id"
    ).fetchone()["id"]
    for snap_date, close, prev_close, yld in [
        (date(2026, 11, 24), 900, 890, 4.2),
        (date(2026, 11, 25), 1000, 900, 5.0),
    ]:
        db.execute(
            "INSERT INTO daily_snapshots(code, snap_date, close_price,"
            " prev_close, dividend_yield) VALUES ('9999', %s, %s, %s, %s)",
            (snap_date, close, prev_close, yld),
        )
    db.execute(
        "INSERT INTO daily_snapshots(code, snap_date, close_price, prev_close)"
        " VALUES ('N225', '2026-11-25', 58000, 57000)"
    )
    db.execute(
        "INSERT INTO dividend_changes(run_id, code, old_div, new_div)"
        " VALUES (%s, '9999', 40, 50)",
        (run_id,),
    )
    db.commit()
    r = client.get("/summary")
    assert r.status_code == 200
    body = r.text
    assert "58,000" in body  # Nikkei close
    assert "テスト銘柄" in body  # mover / dividend change rows

