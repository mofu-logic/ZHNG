"""The /api/v1 JSON API: OpenAPI conformance, reads, and validation.

The API is the single interface for trades, the dividend/benefit
master and the settings: the web UI uses it, and future integrations
use the same paths and shapes (see DESIGN.md).
"""
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
    ):
        assert p in paths, p
    schemas = doc["components"]["schemas"]
    for name in (
        "BuyIn", "BuyOut", "SaleIn", "SaleOut",
        "BenefitIn", "BenefitOut", "SettingsIn", "SettingsOut",
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
    target_yield_value_pct=4.0,
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
