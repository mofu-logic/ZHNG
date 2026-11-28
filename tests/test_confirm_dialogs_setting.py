"""Confirmation dialogs can be switched off in settings.

Four operations ask for confirmation before they run: 保有の削除
(dashboard), 履歴の取消 and 履歴の削除 (history; the delete uses
the native confirm), and 候補の削除 (candidates). The user asked
for a settings switch for those confirmations:

- a new settings key "confirm_dialogs_enabled" -- the *_enabled
  naming follows otel_enabled / swagger_enabled -- defaulting to
  on (True), so the current behaviour stays the default,
- the settings page carries a checkbox for it (labelled with
  確認ダイアログ) and the settings form submits it together with
  the rest of the payload, like the other boolean switches,
- the three pages that own confirmations (dashboard, history,
  candidates) render the current value into the page as a JS
  constant CONFIRM_DIALOGS_ENABLED, and every confirmation site
  consults it: when off, the operation runs immediately (the
  success result dialog pinned in test_result_dialogs.py still
  appears); when on, the confirmation appears exactly as before.
"""
import re

import pytest
from conftest import seed_settings, seed_stock_holding
from test_api import KPI_PAYLOAD

PAGES = ["/", "/history", "/candidates"]


def _store_flag(db, value):
    db.execute(
        "INSERT INTO settings(key, value)"
        " VALUES ('confirm_dialogs_enabled', %s)"
        " ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
        (value,),
    )
    db.commit()


# ------------------------------------------------------------------ API

def test_confirm_dialogs_default_enabled(client, db):
    body = client.get("/api/v1/settings").json()
    assert body["confirm_dialogs_enabled"] is True


def test_confirm_dialogs_roundtrip(client, db):
    r = client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "confirm_dialogs_enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["confirm_dialogs_enabled"] is False
    got = client.get("/api/v1/settings").json()
    assert got["confirm_dialogs_enabled"] is False
    r = client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "confirm_dialogs_enabled": True},
    )
    assert r.json()["confirm_dialogs_enabled"] is True


def test_confirm_dialogs_omitted_defaults_to_enabled(client, db):
    # Like the other boolean switches: a payload that omits the
    # field restores the default (on).
    client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "confirm_dialogs_enabled": False},
    )
    r = client.put("/api/v1/settings", json=KPI_PAYLOAD)
    assert r.json()["confirm_dialogs_enabled"] is True


# ---------------------------------------------------------- settings page

def _checkbox_tag(page):
    m = re.search(r'<input[^>]*name="confirm_dialogs_enabled"[^>]*>', page)
    assert m, "confirm_dialogs_enabled checkbox not found"
    return m.group(0)


def test_settings_page_checkbox_checked_by_default(client, db):
    page = client.get("/settings").text
    assert "確認ダイアログ" in page
    assert "checked" in _checkbox_tag(page)


def test_settings_page_checkbox_follows_stored_value(client, db):
    client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "confirm_dialogs_enabled": False},
    )
    page = client.get("/settings").text
    assert "checked" not in _checkbox_tag(page)


def test_settings_form_submits_confirm_dialogs_flag(client, db):
    page = client.get("/settings").text
    handler = page[page.index("getElementById('settings-form')"):]
    assert "confirm_dialogs_enabled" in handler


# ------------------------------------------- pages render the current flag

@pytest.mark.parametrize("path", PAGES)
def test_pages_render_confirm_flag_default_on(client, db, path):
    page = client.get(path).text
    assert re.search(r"CONFIRM_DIALOGS_ENABLED\s*=\s*true", page), path


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_confirm_flag_off(client, db, path):
    _store_flag(db, "false")
    page = client.get(path).text
    assert re.search(r"CONFIRM_DIALOGS_ENABLED\s*=\s*false", page), path
    assert not re.search(
        r"CONFIRM_DIALOGS_ENABLED\s*=\s*true", page
    ), path


# -------------------------------------------- confirmation sites consult it

def _holding_delete_handler(page):
    start = page.index("querySelectorAll('[data-holding-delete]')")
    end = page.index("holding-import-overlay", start)
    return page[start:end]


def _cancel_handler(page):
    start = page.index("querySelectorAll('[data-cancel]')")
    end = page.index("querySelectorAll('[data-del]')", start)
    return page[start:end]


def _history_delete_handler(page):
    start = page.index("querySelectorAll('[data-del]')")
    end = page.index("const tabs", start)
    return page[start:end]


def _candidate_delete_handler(page):
    start = page.index("querySelectorAll('[data-del-candidate]')")
    return page[start:]


def test_dashboard_delete_consults_flag(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _holding_delete_handler(client.get("/").text)
    assert "CONFIRM_DIALOGS_ENABLED" in js
    # The confirmation itself stays for the enabled path.
    assert "showDialog({" in js


def test_history_cancel_consults_flag(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _cancel_handler(client.get("/history").text)
    assert "CONFIRM_DIALOGS_ENABLED" in js
    assert "showDialog({" in js


def test_history_delete_consults_flag(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _history_delete_handler(client.get("/history").text)
    assert "CONFIRM_DIALOGS_ENABLED" in js
    # The native confirm stays for the enabled path.
    assert "confirm(" in js


def test_candidate_delete_consults_flag(client, db):
    seed_settings(db)
    seed_stock_holding(db)
    js = _candidate_delete_handler(client.get("/candidates").text)
    assert "CONFIRM_DIALOGS_ENABLED" in js
    assert "showDialog({" in js
