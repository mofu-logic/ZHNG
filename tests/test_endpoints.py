"""Endpoint tests against a real (test) PostgreSQL database.

Covers the pages' rendering, the KPI math on the dashboard, the trade
history create/delete flows incl. status auto-derivation and profit
auto-calculation, the settings derivation, and the data page's run
result formatting.
"""
from datetime import date, datetime, timezone

import pytest

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
        "/history/buy",
        data={
            "code": "9999",
            "name": "テスト銘柄",
            "avg_price": "900",
            "shares": "50",
            "purchase_date": "2026-11-20",
            "status": "一部約定",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = db.execute("SELECT * FROM buy_lots").fetchone()
    assert row["code"] == "9999"
    assert row["status"] == "一部約定"
    assert float(row["avg_price"]) == 900.0

    r = client.post(f"/history/buy/{row['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0


def test_history_buy_invalid_status_falls_back(client, db):
    client.post(
        "/history/buy",
        data={"code": "9999", "name": "x", "status": "変な値"},
        follow_redirects=False,
    )
    row = db.execute("SELECT status FROM buy_lots").fetchone()
    assert row["status"] == "約定済"


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
    client.post(
        "/history/sell",
        data={
            "code": "9999",
            "name": "テスト銘柄",
            "buy_price": "100",
            "sell_price": "200",
            "sell_shares": "100",
            "executed_shares": str(executed),
            "status": "自動",
        },
        follow_redirects=False,
    )
    row = db.execute("SELECT * FROM sales").fetchone()
    assert row["status"] == expected_status
    if expected_profit is None:
        assert row["profit"] is None
    else:
        assert float(row["profit"]) == expected_profit


def test_sell_explicit_profit_and_status_win(client, db):
    client.post(
        "/history/sell",
        data={
            "code": "9999",
            "name": "テスト銘柄",
            "buy_price": "100",
            "sell_price": "200",
            "sell_shares": "100",
            "executed_shares": "0",
            "profit": "1234",
            "status": "約定済",
        },
        follow_redirects=False,
    )
    row = db.execute("SELECT * FROM sales").fetchone()
    assert float(row["profit"]) == 1234.0
    assert row["status"] == "約定済"


def test_sell_delete(client, db):
    client.post(
        "/history/sell",
        data={"code": "9999", "name": "x"},
        follow_redirects=False,
    )
    row = db.execute("SELECT id FROM sales").fetchone()
    r = client.post(f"/history/sell/{row['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0


def test_history_page_renders(client, db):
    seed_stock_holding(db, dividend=None, price=None)
    client.post(
        "/history/buy",
        data={"code": "9999", "name": "テスト銘柄"},
        follow_redirects=False,
    )
    r = client.get("/history")
    assert r.status_code == 200
    assert "テスト銘柄" in r.text


def test_settings_monthly_target_is_derived(client, db):
    # annual 1,200,000 x (1 - 0.20315) / 12 = 79,685 exactly at seed values;
    # post new numbers and check the stored derivation.
    r = client.post(
        "/settings",
        data={
            "tax_rate_pct": "20.315",
            "target_dividend_total": "1370347",
            "target_yield_buy_pct": "3.0",
            "target_yield_now_pct": "3.5",
            "target_yield_value_pct": "4.0",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    rows = {
        row["key"]: float(row["value"])
        for row in db.execute("SELECT key, value FROM settings").fetchall()
    }
    # 1,370,347 x (1 - 0.20315) / 12 = 90,996.79... -> round half up
    assert rows["target_monthly_income"] == 90997.0
    assert rows["tax_rate"] == pytest.approx(0.20315)
    assert rows["target_yield_value"] == pytest.approx(0.04)


def test_settings_page_renders(client, db):
    seed_settings(db)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "79685" in r.text  # derived monthly target from seed values


def test_master_update(client, db):
    seed_stock_holding(db)
    r = client.post(
        "/master/9999",
        data={
            "benefit_value": "6000",
            "benefit_desc": "食事券",
            "judgment_on": "2026-11-26",
            "judgment_note": "継続保有",
            "funding_target": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = db.execute(
        "SELECT * FROM dividend_benefits WHERE code = '9999'"
    ).fetchone()
    assert float(row["benefit_value"]) == 6000.0
    assert row["benefit_desc"] == "食事券"
    assert row["judgment_note"] == "継続保有"
    assert row["funding_target"] is True
    # dividend itself is auto-fetched only; the form must not touch it
    assert float(row["dividend_forecast"]) == 40.0


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
