"""The /api/v1 JSON API: OpenAPI conformance, reads, and validation.

The API is the single interface for trades, the dividend/benefit
master and the settings: the web UI uses it, and future integrations
use the same paths and shapes (see ARCHITECTURE.md).
"""
import pytest

from conftest import seed_stock_holding


def test_openapi_document(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc["openapi"].startswith("3.")
    assert doc["info"]["version"] == "1.0.0"
    paths = doc["paths"]
    for p in (
        "/api/v1/buys",
        "/api/v1/buys/{lot_id}",
        "/api/v1/sales",
        "/api/v1/sales/{sale_id}",
        "/api/v1/dividend-benefits",
        "/api/v1/dividend-benefits/{code}",
        "/api/v1/settings",
        "/api/v1/candidates",
        "/api/v1/candidates/{code}",
    ):
        assert p in paths, p
    schemas = doc["components"]["schemas"]
    for name in (
        "BuyIn", "BuyOut", "SaleIn", "SaleOut",
        "BenefitIn", "BenefitOut", "SettingsIn", "SettingsOut",
        "CandidateIn", "CandidateOut", "CandidateUpdateIn",
    ):
        assert name in schemas, name
    # Requests and responses are described by the named schemas.
    post_buy = paths["/api/v1/buys"]["post"]
    ref = post_buy["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/BuyIn")
    resp_ref = post_buy["responses"]["201"]["content"]["application/json"]["schema"]["$ref"]
    assert resp_ref.endswith("/BuyOut")
    # The spec document itself is always available; the test UI is not.
    assert client.get("/docs").status_code == 404


def test_api_reads(client, db):
    seed_stock_holding(db)
    client.post("/api/v1/buys", json={"code": "9999", "name": "テスト銘柄"})

    buys = client.get("/api/v1/buys").json()
    assert len(buys) == 1
    assert buys[0]["code"] == "9999"
    assert buys[0]["status"] == "約定済"  # default when unspecified

    assert client.get("/api/v1/sales").json() == []

    benefits = client.get("/api/v1/dividend-benefits").json()
    assert len(benefits) == 1
    assert benefits[0]["code"] == "9999"
    assert benefits[0]["name"] == "テスト銘柄"
    assert benefits[0]["dividend_forecast"] == 40.0

    one = client.get("/api/v1/dividend-benefits/9999")
    assert one.status_code == 200
    assert one.json()["code"] == "9999"
    assert client.get("/api/v1/dividend-benefits/0000").status_code == 404

    settings = client.get("/api/v1/settings").json()
    assert settings["target_dividend_total"] == 0
    assert settings["target_monthly_income"] == 0
    assert settings["tax_rate_pct"] == 20.315  # built-in default


KPI_PAYLOAD = dict(
    tax_rate_pct=20.315, target_dividend_total=1200000,
    target_yield_buy_pct=3.0, target_yield_now_pct=3.5,
    target_yield_portfolio_pct=4.0,
)


def test_settings_app_config_roundtrip(client, db):
    r = client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "log_retention_days": 30,
              "otel_enabled": True,
              "otel_endpoint": "http://collector:4318/v1/logs"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["log_retention_days"] == 30
    assert body["otel_enabled"] is True
    assert body["otel_endpoint"] == "http://collector:4318/v1/logs"
    got = client.get("/api/v1/settings").json()
    assert got["log_retention_days"] == 30
    assert got["otel_enabled"] is True
    # The non-numeric endpoint value must not break the KPI readers.
    assert client.get("/").status_code == 200
    page = client.get("/settings").text
    assert "OpenTelemetry連携" in page
    assert "動作ログの保存期間" in page
    # Retention bounds are validated.
    r = client.put("/api/v1/settings", json={**KPI_PAYLOAD, "log_retention_days": 0})
    assert r.status_code == 422
    # Defaults apply when the new fields are omitted.
    r = client.put("/api/v1/settings", json=KPI_PAYLOAD)
    assert r.json()["log_retention_days"] == 90
    assert r.json()["otel_enabled"] is False
    assert r.json()["swagger_enabled"] is False


def test_swagger_ui_gate(client, db):
    # Off by default: neither docs UI is served.
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    page = client.get("/settings").text
    assert "開発者向け" in page
    assert 'href="/docs"' not in page

    r = client.put(
        "/api/v1/settings", json={**KPI_PAYLOAD, "swagger_enabled": True}
    )
    assert r.json()["swagger_enabled"] is True
    assert client.get("/docs").status_code == 200
    assert "swagger" in client.get("/docs").text.lower()
    assert client.get("/redoc").status_code == 200
    # The settings page shows the /docs link while the switch is on.
    assert 'href="/docs"' in client.get("/settings").text

    client.put(
        "/api/v1/settings", json={**KPI_PAYLOAD, "swagger_enabled": False}
    )
    assert client.get("/docs").status_code == 404
    assert 'href="/docs"' not in client.get("/settings").text


def test_log_retention_prunes_on_update(client, db):
    db.execute(
        "INSERT INTO app_logs(ts, severity, severity_number, body, attributes)"
        " VALUES (now() - interval '100 days', 'INFO', 9, 'old.log', '{}')"
    )
    db.execute(
        "INSERT INTO app_logs(ts, severity, severity_number, body, attributes)"
        " VALUES (now(), 'INFO', 9, 'new.log', '{}')"
    )
    db.commit()
    r = client.put(
        "/api/v1/settings", json={**KPI_PAYLOAD, "log_retention_days": 30}
    )
    assert r.status_code == 200
    bodies = [
        row["body"]
        for row in db.execute("SELECT body FROM app_logs").fetchall()
    ]
    assert "old.log" not in bodies
    assert "new.log" in bodies


def test_api_validation(client, db):
    # Wrong types and impossible dates are rejected with 422.
    r = client.post("/api/v1/buys", json={"avg_price": "abc"})
    assert r.status_code == 422
    r = client.post("/api/v1/sales", json={"purchase_date": "2026-13-40"})
    assert r.status_code == 422
    # Settings requires every field.
    r = client.put("/api/v1/settings", json={"tax_rate_pct": 20.0})
    assert r.status_code == 422
    # Unknown trade ids are 404.
    assert client.delete("/api/v1/buys/424242").status_code == 404
    assert client.delete("/api/v1/sales/424242").status_code == 404


# --- R3: BuyIn / SaleIn require code; numeric fields reject negatives ---
#
# The history screen (templates/history.html) sends code always for buys
# (the input is `required`) and may send it blank for sells (blank becomes
# null via strOrNull). A trade without a code is meaningless either way,
# so code is required for both. Price and share counts stay optional:
# the buy form lets an unfilled order (status 注文中) be registered with
# neither price nor shares, and the sell form leaves every other field
# blankable (an unfilled sell order has no sell price / executed shares
# yet, and the original buy price / share count may be unknown).
# Negative prices / share counts are never valid, so they get ge=0.
# Profit is the exception: a loss is a real outcome, so it stays
# unbounded (see test_sale_negative_profit_accepted).


def test_buy_requires_code(client, db):
    # R3: POST /api/v1/buys {} used to return 201 and create an all-NULL row.
    r = client.post("/api/v1/buys", json={})
    assert r.status_code == 422
    # A payload with everything except the code is rejected the same way.
    r = client.post(
        "/api/v1/buys",
        json={"name": "テスト銘柄", "avg_price": 900, "shares": 50,
              "purchase_date": "2026-11-20", "status": "注文中"},
    )
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0


def test_sale_requires_code(client, db):
    # R3: POST /api/v1/sales {} used to return 201 (status auto-derived
    # to 注文中) and create an all-NULL row.
    r = client.post("/api/v1/sales", json={})
    assert r.status_code == 422
    r = client.post(
        "/api/v1/sales",
        json={"name": "テスト銘柄", "buy_price": 100, "sell_price": 200,
              "sell_shares": 100, "executed_shares": 100, "status": "約定済"},
    )
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0


@pytest.mark.parametrize("code", ["", "   "])
def test_buy_empty_or_blank_code_rejected(client, db, code):
    # R3-1: {"code": ""} used to return 201 and create a row with an
    # empty code; a whitespace-only code is equally meaningless.
    r = client.post("/api/v1/buys", json={"code": code})
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0


@pytest.mark.parametrize("code", ["", "   "])
def test_sale_empty_or_blank_code_rejected(client, db, code):
    r = client.post("/api/v1/sales", json={"code": code})
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0


def test_buy_code_with_surrounding_spaces_is_trimmed(client, db):
    # R3-1: a padded code is normalized to the bare code, not rejected
    # or stored with the spaces (same trimming the screen already does).
    # The name is sent explicitly so the test stays about trimming:
    # an omitted name is now resolved from the stock master (or
    # fetched), which is covered by test_history_form_simplify.py.
    r = client.post(
        "/api/v1/buys", json={"code": " 9999 ", "name": "テスト銘柄"}
    )
    assert r.status_code == 201
    assert db.execute(
        "SELECT code FROM buy_lots").fetchone()["code"] == "9999"


@pytest.mark.parametrize("field", ["avg_price", "shares"])
def test_buy_negative_values_rejected(client, db, field):
    payload = {"code": "9999"}
    payload[field] = -1
    r = client.post("/api/v1/buys", json=payload)
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM buy_lots").fetchone()["c"] == 0


@pytest.mark.parametrize(
    "field",
    ["buy_price", "sell_price", "shares", "sell_shares", "executed_shares"],
)
def test_sale_negative_values_rejected(client, db, field):
    payload = {"code": "9999"}
    payload[field] = -1
    r = client.post("/api/v1/sales", json=payload)
    assert r.status_code == 422
    assert db.execute("SELECT count(*) c FROM sales").fetchone()["c"] == 0


def test_buy_unfilled_order_without_price_or_shares(client, db):
    # Guard: avg_price / shares must stay optional, because the screen
    # registers unfilled orders (注文中) with neither filled in yet.
    r = client.post(
        "/api/v1/buys",
        json={"code": "9999", "name": "テスト銘柄", "status": "注文中"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "注文中"
    assert body["avg_price"] is None
    assert body["shares"] is None


def test_buy_zero_price_and_shares_accepted(client, db):
    # Guard: the bound is ge=0, not gt=0, so zero stays acceptable.
    # The name is sent explicitly so the test stays about the zero
    # bound: an omitted name is now resolved from the stock master
    # (or fetched), which is covered by test_history_form_simplify.py.
    r = client.post(
        "/api/v1/buys",
        json={"code": "9999", "name": "テスト銘柄",
              "avg_price": 0, "shares": 0},
    )
    assert r.status_code == 201


def test_sale_negative_profit_accepted(client, db):
    # Guard: profit must NOT get a ge=0 bound — a loss is a real outcome.
    r = client.post(
        "/api/v1/sales",
        json={"code": "9999", "name": "テスト銘柄", "buy_price": 200,
              "sell_price": 100, "sell_shares": 10, "executed_shares": 10,
              "profit": -1000, "status": "約定済"},
    )
    assert r.status_code == 201
    assert r.json()["profit"] == -1000.0
