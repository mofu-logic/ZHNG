"""Static CSS extraction tests (DESIGN.md section 9).

The shared stylesheet must live in web/app/static/app.css and be
served at /static/app.css, with the design tokens defined as CSS
custom properties in :root. Pages link that one stylesheet instead
of inlining the common rules from base.html. Visual values (colours,
sizes) stay unchanged -- only their form changes (tokens, not hex).
"""
import re

import pytest

from conftest import seed_settings

# Token name -> value, as fixed by DESIGN.md (colours) and the spec
# for this change (radius / motion). The implementation must define
# exactly these names in :root.
TOKENS = {
    "--color-ink": "#1c2330",
    "--color-navy": "#20304d",
    "--color-navy-mid": "#38557f",
    "--color-border": "#dde2ea",
    "--color-positive": "#177245",
    "--color-negative": "#c62f3b",
    "--color-surface-active": "#e8eff7",
    "--radius-md": "8px",
    "--motion-fast": "0.15s",
}

LINK_TAG = "<link rel=\"stylesheet\" href=\"/static/app.css\">"


@pytest.fixture()
def css(client):
    # No status assertion here: a missing endpoint must surface as
    # plain assertion failures in the tests below, not fixture errors.
    return client.get("/static/app.css").text


def _root_vars(css_text):
    m = re.search(r":root\s*\{([^}]*)\}", css_text)
    assert m, "app.css must define the design tokens in a :root block"
    return {
        name: value.strip().lower()
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1))
    }


def _rule(css_text, selector):
    m = re.search(
        r"(?<![\w#.\-])" + re.escape(selector) + r"\s*\{([^}]*)\}", css_text
    )
    assert m, f"app.css must contain a rule for {selector!r}"
    return m.group(1)


def test_app_css_served_with_css_content_type(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_app_css_defines_design_tokens(css):
    root = _root_vars(css)
    for name, expected in TOKENS.items():
        assert root.get(name) == expected.lower(), (
            f"{name} must be defined as {expected} (got {root.get(name)!r})"
        )


def test_app_css_preserves_current_visual_values(css):
    # The refactor changes form, not appearance: the current key
    # values must still be present (as token definitions).
    for value in ("#20304d", "#38557f", "#e8eff7", "216px", "50px"):
        assert value in css


def test_app_css_common_rules_reference_tokens(css):
    # Representative rules: common styles reference the tokens
    # instead of writing hex colours directly.
    assert "var(--color-navy)" in _rule(css, "header")
    assert "var(--color-navy-mid)" in _rule(css, "th")


@pytest.mark.parametrize("path", ["/", "/summary"])
def test_pages_link_app_css(client, db, path):
    seed_settings(db)
    body = client.get(path).text
    assert LINK_TAG in body


@pytest.mark.parametrize("path", ["/", "/summary"])
def test_pages_do_not_inline_common_styles(client, db, path):
    seed_settings(db)
    body = client.get(path).text
    # The big base.html <style> block (common rule definitions)
    # must be gone from the rendered pages.
    assert ".kpi {" not in body
    assert "table {" not in body
    assert "#sidebar {" not in body
