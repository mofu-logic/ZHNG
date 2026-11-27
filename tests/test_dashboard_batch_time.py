"""Dashboard subtitle: the batch time follows fetch_start_time.

The subtitle under the dashboard heading used to hard-code the
nightly-batch time ("（夜間バッチ 1:00 JST ＋ 手動取得）"). The batch
start time is a setting (settings.fetch_start_time, default
"01:00"), and the /data page tooltip already follows it; the
dashboard subtitle must show the configured value too, so the two
pages cannot drift apart when the start time is changed.
"""
from conftest import seed_settings
from test_api import KPI_PAYLOAD


def _put_fetch_start_time(client, value):
    return client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "fetch_start_time": value},
    )


def test_dashboard_shows_default_fetch_start_time(client, db):
    seed_settings(db)
    page = client.get("/").text
    assert "（夜間バッチ 01:00 JST ＋ 手動取得）" in page


def test_dashboard_shows_configured_fetch_start_time(client, db):
    seed_settings(db)
    r = _put_fetch_start_time(client, "02:30")
    assert r.status_code == 200

    page = client.get("/").text
    assert "（夜間バッチ 02:30 JST ＋ 手動取得）" in page
    assert "夜間バッチ 1:00 JST" not in page
