"""Settings numeric ranges: the KPI fields of SettingsIn (R10).

The five numeric KPI inputs are percentages or amounts, and the
/settings form already constrains them in the browser (tax rate
min=0 max=100; the dividend total and the three yield targets
min=0, no max). The API must enforce the same ranges itself:
out-of-range values are rejected with 422 and nothing is stored.

- tax_rate_pct: 0..100 (both ends valid; 100 means the derived
  monthly target is 0).
- target_dividend_total and target_yield_*_pct: 0 or more, with no
  upper bound -- the form sets no max for them either, so a yield
  target above 100 is unusual but accepted.

The remaining SettingsIn fields already carry their own validation
(log_retention_days 1..3650, benefit_deadline_alert_days 0..3650,
fetch_start_time pattern) and are covered by their own test files;
the boolean / endpoint fields have no range to check.
"""
import pytest

from test_api import KPI_PAYLOAD

YIELD_FIELDS = (
    "target_yield_buy_pct",
    "target_yield_now_pct",
    "target_yield_portfolio_pct",
)
NUMERIC_FIELDS = ("tax_rate_pct", "target_dividend_total") + YIELD_FIELDS


def _put(client, **overrides):
    return client.put(
        "/api/v1/settings", json={**KPI_PAYLOAD, **overrides}
    )


def _stored(client):
    got = client.get("/api/v1/settings").json()
    return {k: got[k] for k in NUMERIC_FIELDS}


def _stored_base(client):
    """Store the known-good payload and return what was persisted."""
    r = _put(client)
    assert r.status_code == 200
    return _stored(client)


# --- tax_rate_pct: 0..100 -------------------------------------------------


@pytest.mark.parametrize("bad", [-1, -0.001, 100.001, 101])
def test_settings_tax_rate_pct_out_of_range_rejected(client, db, bad):
    before = _stored_base(client)
    r = _put(client, tax_rate_pct=bad)
    assert r.status_code == 422, bad
    # A rejected update stores nothing at all.
    assert _stored(client) == before


@pytest.mark.parametrize("ok", [0, 100])
def test_settings_tax_rate_pct_boundaries_accepted(client, db, ok):
    r = _put(client, tax_rate_pct=ok)
    assert r.status_code == 200, ok
    assert r.json()["tax_rate_pct"] == ok
    assert _stored(client)["tax_rate_pct"] == ok


# --- target_dividend_total: 0 or more -------------------------------------


def test_settings_target_dividend_total_negative_rejected(client, db):
    before = _stored_base(client)
    r = _put(client, target_dividend_total=-1)
    assert r.status_code == 422
    assert _stored(client) == before


def test_settings_target_dividend_total_zero_accepted(client, db):
    r = _put(client, target_dividend_total=0)
    assert r.status_code == 200
    assert r.json()["target_dividend_total"] == 0
    assert r.json()["target_monthly_income"] == 0


# --- target_yield_*_pct: 0 or more, no upper bound ------------------------


@pytest.mark.parametrize("field", YIELD_FIELDS)
def test_settings_target_yield_negative_rejected(client, db, field):
    before = _stored_base(client)
    r = _put(client, **{field: -0.01})
    assert r.status_code == 422, field
    assert _stored(client) == before


@pytest.mark.parametrize("field", YIELD_FIELDS)
@pytest.mark.parametrize("ok", [0, 150])
def test_settings_target_yield_boundaries_accepted(client, db, field, ok):
    # 0 is the form's minimum; 150 exceeds 100% but the form sets no
    # max for yield targets, so the API accepts it too.
    r = _put(client, **{field: ok})
    assert r.status_code == 200, (field, ok)
    assert r.json()[field] == ok
    assert _stored(client)[field] == ok
