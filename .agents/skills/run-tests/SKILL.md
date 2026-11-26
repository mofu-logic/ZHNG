---
name: run-tests
description: Run the pytest suite (with branch coverage) the same way CI does, against a throwaway PostgreSQL. Use before pushing any change to web/app, and when adding tests.
---

# Run the tests

The suite has three layers:

- `tests/test_format.py`, `test_helpers.py`, `test_parse.py` — pure
  functions (formatting, form parsing, Yahoo page parsing). No DB.
- `tests/test_endpoints.py` — pages and calculations against a real
  PostgreSQL (dashboard KPIs, trade create/delete flows, status and
  profit derivation, settings derivation, summary fallbacks).
- `tests/test_fetch.py` — the nightly fetch pipeline end to end with
  `httpx` replaced by a fake client (prices, snapshots, dividend
  auto-update + change records, Nikkei failure isolation).

DB-backed tests read `TEST_DATABASE_URL`; when no test database is
reachable they **skip** instead of failing, so a green run with skips
is not a full verification. Always check the skip count.

## Against the compose stack (development host)

Create a test database in the running `db` container once:

```sh
docker compose exec db createdb -U stock stock_test
```

Then run the suite in a throwaway container built from the web image,
on the compose network, with the repo mounted:

```sh
PW=$(grep -E "^DB_PASSWORD=" .env | cut -d= -f2-)
docker run --rm --network stock-app_default -v "$PWD":/repo -w /repo \
  -e TEST_DATABASE_URL="postgresql://stock:${PW}@db:5432/stock_test" \
  stock-app-web:latest sh -c \
  "pip install -q -r web/requirements-dev.txt; \
   python -m pytest tests --cov=app --cov-report=term-missing -q"
```

## Coverage

- Branch coverage (C1) is enabled by `.coveragerc`; CI fails below
  95%. Do not chase 100%: the scheduler loop body (it sleeps until
  01:00) is intentionally uncovered.
- When adding a feature, add tests in the matching layer. For fetch
  changes, extend the fake client in `tests/test_fetch.py` rather
  than mocking deeper internals.
- A test that finds a production bug is a success, not a nuisance:
  fix the bug, keep the test as the regression guard, and say what
  happened in the commit body.
