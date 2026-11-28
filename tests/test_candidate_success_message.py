"""Success feedback for candidate registration.

Registering a purchase candidate used to give feedback only on
failure: the submit handler showed a dialog for errors but silently
reloaded the page on success (201). The user asked for a success
dialog as well, worded "{code} {name} を登録しました。" with the
code and name taken from the API response (the name in particular
is only known server-side until the quote has been fetched).

Pinned here, in the page-JS style of the suite, against the
registration submit handler of /candidates:

- the handler composes the 〜を登録しました。 message from the
  response values (.code / .name) and shows it via showDialog,
- the list refresh (location.reload) survives and is composed
  after the success dialog, so the dialog is seen before the
  page reloads,
- the failure dialogs (登録済みの銘柄です / 登録できませんでした)
  are unchanged,
- the 201 response itself carries code and name (the data source
  for the message).
"""
from test_candidates import _named_fake_client

from app import main


def _register_handler(page):
    """JS of the candidate registration submit handler: from the
    form's submit listener up to the editor dialog code."""
    start = page.index("getElementById('candidate-form')")
    end = page.index("openCandidateEditor", start)
    return page[start:end]


def test_success_message_wording_in_register_handler(client, db):
    handler = _register_handler(client.get("/candidates").text)
    assert "を登録しました。" in handler


def test_success_message_uses_response_code_and_name(client, db):
    # The wording must be built from the API response values, not
    # from the typed-in code alone: the handler has to read .code
    # and .name off the parsed response.
    handler = _register_handler(client.get("/candidates").text)
    assert ".code" in handler
    assert ".name" in handler


def test_success_message_goes_through_show_dialog(client, db):
    handler = _register_handler(client.get("/candidates").text)
    # The failure branch and the network-error branch already
    # account for two showDialog calls in this handler; the
    # success path adds a third one.
    assert handler.count("showDialog({") >= 3


def test_success_dialog_precedes_reload(client, db):
    # The list refresh must survive, and it has to run after the
    # success dialog is composed: a reload first would discard
    # the dialog before it is ever seen.
    handler = _register_handler(client.get("/candidates").text)
    assert "location.reload()" in handler
    assert handler.index("を登録しました。") < handler.index(
        "location.reload()"
    )


def test_failure_dialogs_kept_in_register_handler(client, db):
    handler = _register_handler(client.get("/candidates").text)
    assert "登録済みの銘柄です" in handler
    assert "登録できませんでした" in handler
    assert "showDialog" in handler


def test_register_response_carries_code_and_name(client, db, monkeypatch):
    # The data source for the success message: the 201 response
    # already returns the registered code and name.
    monkeypatch.setattr(main.httpx, "AsyncClient", _named_fake_client())
    r = client.post("/api/v1/candidates", json={"code": "7777"})
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == "7777"
    assert body["name"] == "テスト候補(株)"
