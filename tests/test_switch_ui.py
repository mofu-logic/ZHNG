"""Toggle switches for the on/off settings; decorated checkboxes.

The user asked for a more modern settings UI:

- The three on/off settings on the settings page
  (confirm_dialogs_enabled, otel_enabled, swagger_enabled) become
  toggle switches: a pill-shaped track with a round knob that
  slides with a transition when the state flips. Only the
  appearance changes -- the control stays an
  <input type="checkbox"> with the same name attribute and the
  same checked linkage, so saving and the settings API behave
  exactly as before (that behaviour side stays pinned by
  test_confirm_dialogs_setting.py and test_api.py).
- Every other checkbox keeps the checkbox form but is decorated
  by the shared stylesheet instead of the browser default. Those
  checkboxes are all created by the edit dialogs (the candidates
  and master policy multi-selects, the master funding-target
  check, the benefit-memos delivery-waiting check), so the
  decoration hooks onto the shared selector
  input[type="checkbox"]:not(.switch) in app.css: no per-site
  class is needed and every dialog gets the same look.

Hooks fixed by this spec:

- A switch is <input type="checkbox" class="switch">. app.css
  styles the input itself as the track (appearance: none, pill
  radius, explicit size, a transition over the background timed
  with the shared motion token) and its ::after pseudo-element
  as the knob (round, with a transform transition). The :checked
  rules paint the track var(--color-navy) and slide the knob
  with translateX. A :focus-visible rule keeps a visible focus
  outline, so keyboard operation stays usable.
- The decorated checkbox base rule removes the native appearance
  and draws a custom box (border in var(--color-border-input),
  a radius, surface background); its :checked rule fills the
  box var(--color-navy); a ::before/::after rule draws the
  check mark; :focus-visible keeps a visible focus outline.
  An accent-color-only tweak does not satisfy this spec.

DESIGN.md: the component catalogue (section 6) gains an entry
for the switch (スイッチ) and one for the decorated checkbox
(チェックボックス), following the catalogue rule that new
components are recorded there.
"""
import re

import pytest

from conftest import seed_settings
from test_api import KPI_PAYLOAD

SWITCH_NAMES = [
    "confirm_dialogs_enabled",
    "otel_enabled",
    "swagger_enabled",
]

LINK_TAG = "<link rel=\"stylesheet\" href=\"/static/app.css\">"

CHECKBOX = 'input[type="checkbox"]:not(.switch)'


@pytest.fixture()
def rules(client):
    # Parse app.css into (selector, body) pairs. Selectors are
    # normalised (whitespace collapsed, quotes dropped) so the
    # assertions below pin the hook, not the exact spelling.
    css_text = client.get("/static/app.css").text
    pairs = []
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css_text):
        sel = " ".join(selector.split()).replace('"', "")
        pairs.append((sel, body))
    return pairs


def _rule(rules, *selectors):
    wanted = {" ".join(s.split()).replace('"', "") for s in selectors}
    for sel, body in rules:
        if sel in wanted:
            return body
    raise AssertionError(
        f"app.css has no rule for any of {sorted(wanted)}"
    )


def _input_tag(page, name):
    m = re.search(r'<input[^>]*name="%s"[^>]*>' % re.escape(name), page)
    assert m, f"input {name!r} not found"
    return m.group(0)


# ------------------------------------------------------- settings page

@pytest.mark.parametrize("name", SWITCH_NAMES)
def test_settings_on_off_inputs_are_switches(client, db, name):
    page = client.get("/settings").text
    tag = _input_tag(page, name)
    # Still a checkbox input (the save path reads .checked by
    # name); only the hook for the switch look is added.
    assert 'type="checkbox"' in tag
    m = re.search(r'class="([^"]*)"', tag)
    assert m and "switch" in m.group(1).split(), tag


def test_settings_switch_follows_stored_value(client, db):
    # The switch keeps the checkbox contract: the rendered checked
    # state follows the stored setting (shown for otel_enabled,
    # which defaults to off).
    tag = _input_tag(client.get("/settings").text, "otel_enabled")
    assert "checked" not in tag
    client.put(
        "/api/v1/settings",
        json={**KPI_PAYLOAD, "otel_enabled": True},
    )
    tag = _input_tag(client.get("/settings").text, "otel_enabled")
    assert "checked" in tag


# ------------------------------------------------------------- switch CSS

def test_switch_track_rule(rules):
    body = _rule(rules, ".switch", "input.switch")
    # The input itself becomes the pill-shaped track.
    assert "appearance: none" in body
    assert "border-radius" in body and "var(--radius-pill)" in body
    assert "width:" in body and "height:" in body
    # The on/off flip animates: a transition over the background,
    # timed with the shared motion token.
    assert "transition" in body
    assert "background" in body
    assert "var(--motion-fast)" in body


def test_switch_knob_rule(rules):
    body = _rule(
        rules,
        ".switch::after", ".switch::before",
        "input.switch::after", "input.switch::before",
    )
    assert "content" in body
    # Round knob.
    assert "border-radius" in body
    assert "50%" in body or "var(--radius-pill)" in body
    # The knob slides: its transition targets the transform.
    assert "transition" in body and "transform" in body
    assert "var(--motion-fast)" in body


def test_switch_checked_track_is_navy(rules):
    body = _rule(rules, ".switch:checked", "input.switch:checked")
    assert "background" in body
    assert "var(--color-navy)" in body


def test_switch_checked_knob_slides(rules):
    body = _rule(
        rules,
        ".switch:checked::after", ".switch:checked::before",
        "input.switch:checked::after", "input.switch:checked::before",
    )
    assert "translateX(" in body


def test_switch_focus_visible(rules):
    body = _rule(
        rules, ".switch:focus-visible", "input.switch:focus-visible"
    )
    assert "outline" in body


# ------------------------------------------------- decorated checkbox CSS

def test_checkbox_base_rule(rules):
    body = _rule(rules, CHECKBOX)
    # Not an accent-color tweak: the native appearance is replaced
    # by a custom box drawn from the design tokens.
    assert "appearance: none" in body
    assert "border" in body and "var(--color-border-input)" in body
    assert "border-radius" in body
    assert "background" in body and "var(--color-surface)" in body


def test_checkbox_checked_rule(rules):
    body = _rule(
        rules,
        CHECKBOX + ":checked",
        'input[type="checkbox"]:checked:not(.switch)',
    )
    assert "background" in body
    assert "var(--color-navy)" in body


def test_checkbox_checkmark_rule(rules):
    body = _rule(
        rules,
        CHECKBOX + "::before", CHECKBOX + "::after",
        CHECKBOX + ":checked::before", CHECKBOX + ":checked::after",
    )
    # Some visible mark is drawn by the pseudo-element.
    assert "content" in body or "background" in body or "clip-path" in body


def test_checkbox_focus_visible(rules):
    body = _rule(rules, CHECKBOX + ":focus-visible")
    assert "outline" in body


# ------------------------------------------- dialog pages use the same CSS

@pytest.mark.parametrize(
    "path", ["/candidates", "/master", "/benefit-memos"]
)
def test_dialog_pages_link_app_css(client, db, path):
    # The decorated look reaches the dialog pages through the one
    # shared stylesheet.
    seed_settings(db)
    body = client.get(path).text
    assert LINK_TAG in body


@pytest.mark.parametrize(
    "path", ["/candidates", "/master", "/benefit-memos"]
)
def test_dialog_pages_still_create_plain_checkboxes(client, db, path):
    # The decoration comes from the shared stylesheet only: the
    # dialogs keep creating plain checkbox inputs in JS, with no
    # per-site class to add.
    seed_settings(db)
    body = client.get(path).text
    assert "checkbox" in body
