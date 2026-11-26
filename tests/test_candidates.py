"""Purchase candidates: registration fetch, list/delete, batch
inclusion, and isolation from the dividend master and the summary."""
import asyncio

import pytest
from conftest import seed_stock_holding
from test_fetch import make_fake_client
from test_parse import JP_PAGE

from app import main

_TITLE = (
    "<html><head><title>"
    + "テスト候補(株)" + "【" + "7777" + "】"
    + "：株価・株式情報 - Yahoo!ファイナンス</title></head>"
)
CANDIDATE_PAGE = JP_PAGE.replace("<html>", _TITLE, 1)


def _named_fake_client():
    base = make_fake_client()

    class NamedClient(base):
        async def get(self, url):
            r = await super().get(url)
            if "/7777." in url and r.status_code == 200:
                return type(r)(200, CANDIDATE_PAGE)
            return r

    return NamedClient


@pytest.fixture()
def no_sleep(monkeypatch):
    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    main._fetch_lock = asyncio.Lock()


def test_candidate_register_fetches_info(client, db, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    r = client.post("/api/v1/candidates", json={"code": "7777"})
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == "7777"
    assert body["name"] == "テスト候補(株)"
    assert body["close_price"] == 1000.0  # from the JP_PAGE fixture
    assert body["snap_date"] is not None
    # Registered, with a snapshot, but NOT in the dividend master.
    assert db.execute(
        "SELECT count(*) c FROM candidates WHERE code='7777'"
    ).fetchone()["c"] == 1
    assert db.execute(
        "SELECT count(*) c FROM daily_snapshots WHERE code='7777'"
    ).fetchone()["c"] == 1
    assert db.execute(
        "SELECT count(*) c FROM dividend_benefits WHERE code='7777'"
    ).fetchone()["c"] == 0
    stock = db.execute(
        "SELECT name, market FROM stocks WHERE code='7777'"
    ).fetchone()
    assert stock["name"] == "テスト候補(株)"
    assert stock["market"] == "東証"
    # List endpoint and page show it.
    listed = client.get("/api/v1/candidates").json()
    assert [c["code"] for c in listed] == ["7777"]
    page = client.get("/candidates").text
    assert "7777" in page and "テスト候補" in page
    assert "https://finance.yahoo.co.jp/quote/7777" in page
    assert "登録件数：1件" in page


def test_candidate_duplicate_and_delete(client, db, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", make_fake_client())
    assert client.post(
        "/api/v1/candidates", json={"code": "7778"}
    ).status_code == 201
    r = client.post("/api/v1/candidates", json={"code": "7778"})
    assert r.status_code == 409
    assert client.delete("/api/v1/candidates/7778").status_code == 204
    assert client.delete("/api/v1/candidates/7778").status_code == 404
    assert client.get("/api/v1/candidates").json() == []


def test_candidate_unknown_code_not_registered(client, db, monkeypatch):
    monkeypatch.setattr(
        main.httpx, "AsyncClient", make_fake_client(fail_codes=("0000",))
    )
    r = client.post("/api/v1/candidates", json={"code": "0000"})
    assert r.status_code == 502
    assert db.execute(
        "SELECT count(*) c FROM candidates"
    ).fetchone()["c"] == 0
    # Bad code shapes are rejected before any fetch.
    assert client.post(
        "/api/v1/candidates", json={"code": "ab"}
    ).status_code == 422
    assert client.post(
        "/api/v1/candidates", json={"code": "12-4"}
    ).status_code == 422


def test_batch_includes_candidates_without_master_pollution(
    client, db, monkeypatch, no_sleep
):
    seed_stock_holding(db)  # holding 9999 with a master row
    db.execute(
        "INSERT INTO stocks(code, name, market)"
        " VALUES ('7777', '候補', '東証')"
    )
    db.execute("INSERT INTO candidates(code) VALUES ('7777')")
    db.commit()
    monkeypatch.setattr(main.httpx, "AsyncClient", make_fake_client())
    asyncio.run(main.fetch_all("batch"))
    run = db.execute("SELECT * FROM fetch_runs").fetchone()
    assert run["total_count"] == 2  # holding + candidate
    assert run["ok_count"] == 2
    assert db.execute(
        "SELECT count(*) c FROM daily_snapshots WHERE code='7777'"
    ).fetchone()["c"] == 1
    # The candidate got no dividend master row and no change record.
    assert db.execute(
        "SELECT count(*) c FROM dividend_benefits WHERE code='7777'"
    ).fetchone()["c"] == 0
    assert db.execute(
        "SELECT count(*) c FROM dividend_changes WHERE code='7777'"
    ).fetchone()["c"] == 0


def test_summary_counts_holdings_only(client, db):
    seed_stock_holding(db, dividend=None, price=None)
    db.execute(
        "INSERT INTO stocks(code, name, market)"
        " VALUES ('7777', '候補', '東証')"
    )
    db.execute("INSERT INTO candidates(code) VALUES ('7777')")
    # Holding is flat, the candidate doubled: summary must ignore it.
    db.execute(
        "INSERT INTO daily_snapshots(code, snap_date, close_price, prev_close)"
        " VALUES ('9999', '2026-11-25', 100, 100),"
        " ('7777', '2026-11-25', 200, 100)"
    )
    db.commit()
    page = client.get("/summary").text
    assert "値上がり銘柄数：0銘柄" in page
    assert "変わらず銘柄数：1銘柄" in page
    assert "7777" not in page


def test_candidate_preview(client, db, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    r = client.get("/api/v1/candidates/preview/7777")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "テスト候補(株)"
    assert body["close_price"] == 1000.0
    # Preview alone must not register anything.
    assert db.execute(
        "SELECT count(*) c FROM candidates"
    ).fetchone()["c"] == 0
    # The page carries the preview slot and the shared dialog.
    page = client.get("/candidates").text
    assert 'id="candidate-preview"' in page
    assert "showDialog" in page


def test_candidate_preview_not_found(client, db, monkeypatch):
    monkeypatch.setattr(
        main.httpx, "AsyncClient", make_fake_client(fail_codes=("0000",))
    )
    assert client.get("/api/v1/candidates/preview/0000").status_code == 404
    assert client.get("/api/v1/candidates/preview/ab").status_code == 422
