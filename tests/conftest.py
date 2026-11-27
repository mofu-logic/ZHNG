"""Shared fixtures for the stock-app test suite.

Pure-function tests need no database. DB-backed tests use the database
named by TEST_DATABASE_URL (falling back to DATABASE_URL); when no test
database is reachable they are skipped, so the suite still runs anywhere.

Local run (against the dev stack on ubuntu):
    TEST_DATABASE_URL=postgresql://stock:***@localhost:5432/stock_test \
    PYTHONPATH=web pytest tests --cov=app --cov-report=term-missing
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL", "postgresql://stock:stock@localhost:5432/stock_test"
)
os.environ["DATABASE_URL"] = TEST_DB_URL

import psycopg
import pytest
from hypothesis import settings as _hypothesis_settings
from psycopg.rows import dict_row

from app import main

# Hypothesis profiles (two-tier operation for the property tests in
# tests/test_properties.py):
# - "light" (default, also what CI runs): few examples, derandomized
#   so failures reproduce exactly and the suite stays fast.
# - "heavy": many more examples for pre-release / pre-deploy digging.
# Select with the HYPOTHESIS_PROFILE environment variable.
_hypothesis_settings.register_profile(
    "light", max_examples=25, derandomize=True
)
_hypothesis_settings.register_profile("heavy", max_examples=300)
_hypothesis_settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE", "light")
)

TABLES = [
    "app_logs",
    "benefit_memos",
    "candidates",
    "dividend_changes",
    "daily_snapshots",
    "price_points",
    "fetch_runs",
    "sales",
    "buy_lots",
    "dividend_benefits",
    "holdings",
    "holding_events",
    "stocks",
    "settings",
]


def _db_available():
    try:
        with psycopg.connect(TEST_DB_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    if not DB_AVAILABLE:
        yield
        return
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(main.SCHEMA)
    yield


@pytest.fixture()
def db():
    """A clean, empty database (schema applied, all tables truncated)."""
    if not DB_AVAILABLE:
        pytest.skip("test database not available (set TEST_DATABASE_URL)")
    conn = psycopg.connect(TEST_DB_URL, row_factory=dict_row)
    conn.execute(
        "TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def seed_settings(conn, **overrides):
    values = {
        "tax_rate": "0.20315",
        "target_dividend_total": "1200000",
        "target_monthly_income": "79685",
        "target_yield_buy": "0.03",
        "target_yield_now": "0.035",
        "target_yield_portfolio": "0.04",
    }
    values.update(overrides)
    for k, v in values.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (%s, %s)", (k, v)
        )
    conn.commit()


def seed_stock_holding(
    conn,
    code="9999",
    name="テスト銘柄",
    market="東証",
    avg_price=800,
    shares=100,
    price=1000,
    dividend=40,
    benefit=5000,
):
    conn.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, %s)",
        (code, name, market),
    )
    conn.execute(
        "INSERT INTO holdings(code, account_type, avg_price, shares)"
        " VALUES (%s, '特定', %s, %s)",
        (code, avg_price, shares),
    )
    if price is not None:
        conn.execute(
            "INSERT INTO price_points(code, captured_at, price, source)"
            " VALUES (%s, now(), %s, 'test')",
            (code, price),
        )
    if dividend is not None:
        conn.execute(
            "INSERT INTO dividend_benefits(code, dividend_forecast,"
            " dividend_auto, benefit_value) VALUES (%s, %s, %s, %s)",
            (code, dividend, dividend, benefit),
        )
    conn.commit()
