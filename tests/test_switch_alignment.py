"""Alignment of the three settings switches (same inset from the right).

The user accepted the switch look itself (test_switch_ui.py) but
pointed out that the three switches on the settings page sit at
different horizontal positions: each switch row lives in its own
<table>, the tables use auto layout with no column widths, and the
switch is an inline-level box at the left edge of its value cell.
The label column therefore takes a different width in every table
-- the confirm row's label is the longest, the Swagger label is
the shortest, and the OpenTelemetry table's value column is
widened by the 320px endpoint input in its second row -- so the
distance from the switch to the right edge of its row differs
from row to row.

Spec fixed here: all three switch rows share one structure and
one alignment rule.

- The value cell of each switch row carries the shared class
  "switch-cell" (the confirm row included; no row is special).
- app.css aligns the switch through that cell: the .switch-cell
  rule right-aligns the cell's content and sets a single
  padding-right of 53px, so every switch's right edge sits
  exactly 53px in from the right edge of its row, regardless of
  the label length or of other rows in the same table.

The 53px inset is the reference row's measured current
position. Rendering the production settings page (form width
640px) in headless Chromium and measuring the three switches
gave, for the distance from each switch's right edge to the
right edge of its row: confirm_dialogs_enabled 53px,
swagger_enabled 99.5px, otel_enabled 348.9px (the otel value
column is widened to about 399px by the endpoint input, the
confirm value column is 103px). The spec defines the shared
inset as the reference row's measured 53px, so the reference
row keeps its exact position while the other two rows move to
match it. These tests pin the mechanism (shared hook + one
rule + the defined inset), not rendered pixels.

The switch itself (size, colours, transition) and the settings
save behaviour are unchanged; they stay pinned by
test_switch_ui.py and test_confirm_dialogs_setting.py.
DESIGN.md's switch entry in the component catalogue (section 6)
is expected to gain a line describing this alignment, following
the same not-test-pinned convention as the rest of the
catalogue wording.
"""
import re

import pytest

SWITCH_NAMES = [
    "confirm_dialogs_enabled",
    "otel_enabled",
    "swagger_enabled",
]

CONFIRM_LABEL = "削除・取消の前に確認ダイアログを表示する"


@pytest.fixture()
def rules(client):
    # Parse app.css into (selector, body) pairs, following
    # test_switch_ui.py: selectors are normalised (whitespace
    # collapsed, quotes dropped) so the assertions pin the hook,
    # not the exact spelling.
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


def _cell_tag(page, name):
    # The settings rows are <td>...<input ... name="..."></td>:
    # return the tag of the cell that directly wraps the input.
    m = re.search(
        r"<td[^>]*>\s*<input[^>]*name=\"%s\"" % re.escape(name), page
    )
    assert m, f"switch input {name!r} is not wrapped in a table cell"
    return re.match(r"<td[^>]*>", m.group(0)).group(0)


# ------------------------------------------------------- shared structure

@pytest.mark.parametrize("name", SWITCH_NAMES)
def test_switch_input_sits_in_a_switch_cell(client, db, name):
    page = client.get("/settings").text
    tag = _cell_tag(page, name)
    m = re.search(r'class="([^"]*)"', tag)
    assert m and "switch-cell" in m.group(1).split(), tag


def test_only_the_three_switch_rows_use_switch_cells(client, db):
    # The alignment hook belongs to the three switch rows only:
    # no other settings row may pick it up, and none may miss it.
    page = client.get("/settings").text
    assert page.count("switch-cell") == len(SWITCH_NAMES)


# ------------------------------------------------------------- alignment CSS

def test_switch_cell_rule_right_aligns_with_reference_inset(rules):
    body = _rule(rules, ".switch-cell", "td.switch-cell")
    body = " ".join(body.split())
    # Right-alignment takes the switch out of the label column's
    # geometry: its right edge is measured from the row's right
    # edge, not from the end of the label.
    assert "text-align: right" in body
    # One shared inset, equal to the reference (confirm) row's
    # measured current distance from the right edge (53px).
    assert re.search(r"padding-right\s*:\s*53px", body), body


# --------------------------------------------------- reference row unchanged

def test_confirm_row_keeps_its_label_and_switch(client, db):
    # The reference row is aligned by the same shared hook as the
    # other two rows; its label text and its switch input (the
    # checkbox contract: type, switch class, name) are unchanged.
    page = client.get("/settings").text
    assert CONFIRM_LABEL in page
    m = re.search(
        r'<input[^>]*name="confirm_dialogs_enabled"[^>]*>', page
    )
    assert m, "confirm_dialogs_enabled input not found"
    tag = m.group(0)
    assert 'type="checkbox"' in tag
    cls = re.search(r'class="([^"]*)"', tag)
    assert cls and "switch" in cls.group(1).split(), tag
