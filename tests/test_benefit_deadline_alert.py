"""Benefit deadline alerts （優待期限アラート）: the
benefit_deadline_alert_days setting and the alert lists shown on
/summary and at the top of /benefit-memos.

Domain rule under test: for the 優待メモ display targets (a benefit
in the dividend/benefit master AND currently held), each of
deadline_1 / deadline_2 whose remaining days (deadline - today in
JST) are at or below the configured threshold raises an alert.
Expired deadlines (negative remaining days) count too; unset
deadlines never do. The two deadlines are judged independently,
and the list is ordered by deadline ascending.

Dates are built relative to "today" (JST) so the tests hold no
matter which exact clock the implementation reads.
"""
import re
from datetime import datetime, timedelta

from app.config import JST
from conftest import seed_stock_holding

ALERT_HEADING = "優待期限アラート"
SETTING_LABEL = "優待期限アラート日数"
SETTING_KEY = "benefit_deadline_alert_days"

KPI_PAYLOAD = dict(
    tax_rate_pct=20.315, target_dividend_total=1200000,
    target_yield_buy_pct=3.0, target_yield_now_pct=3.5,
    target_yield_portfolio_pct=4.0,
)


def _today():
    return datetime.now(JST).date()


def _in(days):
    """ISO date `days` from today (negative = already past)."""
    return (_today() + timedelta(days=days)).isoformat()


def _seed_target(db, code="1001", name="優待銘柄"):
    """A 優待メモ display target: benefit in the master, held."""
    seed_stock_holding(db, code=code, name=name, benefit=5000)


def _set_memo(client, code, **fields):
    r = client.put(f"/api/v1/benefit-memos/{code}", json=fields)
    assert r.status_code == 200


def _set_alert_days(client, days):
    r = client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, SETTING_KEY: days},
    )
    assert r.status_code == 200
    return r


def _alert_part(page):
    """The part of the 優待メモ page above its main table."""
    return page.split('<table id="benefit-memos">')[0]


# --- The setting itself -------------------------------------------------


def test_alert_days_default_is_30(client, db):
    got = client.get("/api/v1/settings").json()
    assert got[SETTING_KEY] == 30


def test_alert_days_in_openapi_schema(client):
    doc = client.get("/openapi.json").json()
    schemas = doc["components"]["schemas"]
    assert SETTING_KEY in schemas["SettingsIn"]["properties"]
    assert SETTING_KEY in schemas["SettingsOut"]["properties"]


def test_alert_days_roundtrip(client, db):
    r = _set_alert_days(client, 7)
    assert r.json()[SETTING_KEY] == 7
    got = client.get("/api/v1/settings").json()
    assert got[SETTING_KEY] == 7


def test_alert_days_omitted_resets_to_default(client, db):
    _set_alert_days(client, 7)
    r = client.put("/api/v1/settings", json=KPI_PAYLOAD)
    assert r.status_code == 200
    assert r.json()[SETTING_KEY] == 30


def test_alert_days_validation(client, db):
    for bad in (-1, 3651, "abc"):
        r = client.put(
            "/api/v1/settings", json={**KPI_PAYLOAD, SETTING_KEY: bad}
        )
        assert r.status_code == 422, bad
    # Zero is valid: only deadlines due today or already past alert.
    r = client.put(
        "/api/v1/settings", json={**KPI_PAYLOAD, SETTING_KEY: 0}
    )
    assert r.status_code == 200
    assert r.json()[SETTING_KEY] == 0


def _input_tag(page, name):
    m = re.search(r'<input[^>]*name="%s"[^>]*>' % name, page)
    assert m, name
    return m.group(0)


def test_settings_page_has_alert_days_input(client, db):
    page = client.get("/settings").text
    assert SETTING_LABEL in page
    assert 'value="30"' in _input_tag(page, SETTING_KEY)


def test_settings_page_shows_stored_alert_days(client, db):
    _set_alert_days(client, 7)
    page = client.get("/settings").text
    assert 'value="7"' in _input_tag(page, SETTING_KEY)


# --- Alert judgement on /summary ----------------------------------------


def test_summary_alert_within_threshold(client, db):
    _seed_target(db, name="期限近い銘柄")
    _set_memo(client, "1001", deadline_1=_in(5))
    page = client.get("/summary").text
    assert ALERT_HEADING in page
    assert "1001" in page
    assert "期限近い銘柄" in page
    assert _in(5) in page
    # The remaining days are shown as a number of days.
    assert "5日" in page


def test_summary_no_alert_beyond_threshold(client, db):
    _seed_target(db)
    _set_memo(client, "1001", deadline_1=_in(40))
    assert ALERT_HEADING not in client.get("/summary").text


def test_summary_alert_at_exact_threshold(client, db):
    # Remaining days == the setting (default 30) still alerts.
    _seed_target(db)
    _set_memo(client, "1001", deadline_1=_in(30))
    page = client.get("/summary").text
    assert ALERT_HEADING in page
    assert _in(30) in page


def test_summary_no_alert_one_day_past_threshold(client, db):
    _seed_target(db)
    _set_memo(client, "1001", deadline_1=_in(31))
    assert ALERT_HEADING not in client.get("/summary").text


def test_summary_alert_for_todays_deadline(client, db):
    _seed_target(db)
    _set_memo(client, "1001", deadline_1=_in(0))
    assert ALERT_HEADING in client.get("/summary").text


def test_summary_alert_for_expired_deadline(client, db):
    # Already past the deadline: still an alert.
    _seed_target(db)
    _set_memo(client, "1001", deadline_1=_in(-3))
    page = client.get("/summary").text
    assert ALERT_HEADING in page
    assert "1001" in page
    assert _in(-3) in page


def test_summary_no_alert_without_deadlines(client, db):
    # A target with a memo but no deadlines, and a target with no
    # memo at all: neither raises an alert.
    _seed_target(db, code="1001")
    _set_memo(client, "1001", status="カタログ選び")
    _seed_target(db, code="1002", name="メモなし銘柄")
    assert ALERT_HEADING not in client.get("/summary").text


def test_summary_deadline2_judged_independently(client, db):
    # deadline_1 is out of range, deadline_2 is in range: the alert
    # is about deadline_2 only.
    _seed_target(db)
    _set_memo(client, "1001", deadline_1=_in(40), deadline_2=_in(5))
    page = client.get("/summary").text
    assert ALERT_HEADING in page
    assert _in(5) in page
    assert _in(40) not in page


def test_summary_alerts_sorted_by_deadline(client, db):
    _seed_target(db, code="1001", name="先の期限の銘柄")
    _set_memo(client, "1001", deadline_1=_in(20), deadline_2=_in(5))
    _seed_target(db, code="1002", name="中間の期限の銘柄")
    _set_memo(client, "1002", deadline_1=_in(10))
    page = client.get("/summary").text
    assert ALERT_HEADING in page
    # Both deadlines of 1001 alert (judged independently), and the
    # whole list is ordered by deadline ascending.
    assert page.index(_in(5)) < page.index(_in(10)) < page.index(_in(20))


# --- Non-targets never alert --------------------------------------------


def test_non_targets_do_not_alert(client, db):
    # Benefit in the master but NOT held, with a near deadline.
    db.execute(
        "INSERT INTO stocks(code, name, market)"
        " VALUES ('2001', '非保有の優待銘柄', '東証')"
    )
    db.execute(
        "INSERT INTO dividend_benefits(code, dividend_forecast,"
        " dividend_auto, benefit_value) VALUES ('2001', 30, 30, 3000)"
    )
    db.commit()
    _set_memo(client, "2001", deadline_1=_in(5))
    # Held but no benefit in the master, with a near deadline.
    seed_stock_holding(db, code="2002", name="優待なし銘柄", benefit=0)
    _set_memo(client, "2002", deadline_1=_in(5))
    assert ALERT_HEADING not in client.get("/summary").text
    assert ALERT_HEADING not in client.get("/benefit-memos").text


# --- Alert list on /benefit-memos ---------------------------------------


def test_benefit_memos_alert_shown_on_top(client, db):
    _seed_target(db, name="期限近い銘柄")
    _set_memo(client, "1001", deadline_1=_in(5))
    top = _alert_part(client.get("/benefit-memos").text)
    assert ALERT_HEADING in top
    assert "1001" in top
    assert "期限近い銘柄" in top
    assert _in(5) in top


def test_benefit_memos_no_alert_when_none_due(client, db):
    _seed_target(db)
    _set_memo(client, "1001", deadline_1=_in(40))
    assert ALERT_HEADING not in client.get("/benefit-memos").text


def test_benefit_memos_alerts_sorted_by_deadline(client, db):
    _seed_target(db, code="1001", name="遅い期限の銘柄")
    _set_memo(client, "1001", deadline_1=_in(20))
    _seed_target(db, code="1002", name="早い期限の銘柄")
    _set_memo(client, "1002", deadline_1=_in(3))
    top = _alert_part(client.get("/benefit-memos").text)
    assert ALERT_HEADING in top
    assert top.index(_in(3)) < top.index(_in(20))


# --- The setting drives the judgement ------------------------------------


def test_setting_change_reflects_in_alerts(client, db):
    _seed_target(db)
    _set_memo(client, "1001", deadline_1=_in(10))
    # Default threshold (30 days): the deadline alerts.
    assert ALERT_HEADING in client.get("/summary").text
    assert ALERT_HEADING in client.get("/benefit-memos").text
    # Narrowing the threshold to 7 days clears it on both pages.
    _set_alert_days(client, 7)
    assert ALERT_HEADING not in client.get("/summary").text
    assert ALERT_HEADING not in client.get("/benefit-memos").text
    # At exactly the remaining days it alerts again.
    _set_alert_days(client, 10)
    assert ALERT_HEADING in client.get("/summary").text
    assert ALERT_HEADING in client.get("/benefit-memos").text
