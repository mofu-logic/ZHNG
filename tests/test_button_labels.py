"""Button labels name their target (dashboard main actions).

Owner decision: button labels state what they act on, following the
history screen's "購入を追加" / "売却を追加". Only the page's main
actions are in scope -- buttons inside tables and dialogs act on an
obvious target and keep their short labels.

On the dashboard (holdings list) the two main actions above the
holdings table are renamed:
- the button opening the add dialog (id=holding-add-open):
  "追加" -> "保有株の追加"
- the button opening the import dialog (id=holding-import-open):
  "インポート" -> "保有株のインポート"

The dialog buttons themselves (id=holding-add-save /
holding-import-run) are out of scope and keep "追加" / "インポート";
the last test pins that so the rename cannot spread by accident.
"""
from html.parser import HTMLParser


class _Buttons(HTMLParser):
    """Collect {id: text} for every <button> that carries an id."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buttons = {}
        self._current_id = None
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self._current_id = dict(attrs).get("id")
            self._chunks = []

    def handle_data(self, data):
        if self._current_id is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag):
        if tag == "button" and self._current_id is not None:
            self.buttons[self._current_id] = "".join(self._chunks).strip()
            self._current_id = None


def _dashboard_buttons(client):
    parser = _Buttons()
    parser.feed(client.get("/").text)
    return parser.buttons


def test_dashboard_add_button_names_holdings(client):
    buttons = _dashboard_buttons(client)
    assert buttons.get("holding-add-open") == "保有株の追加"


def test_dashboard_import_button_names_holdings(client):
    buttons = _dashboard_buttons(client)
    assert buttons.get("holding-import-open") == "保有株のインポート"


def test_dashboard_dialog_buttons_keep_short_labels(client):
    # Regression guard: the rename covers the main actions only; the
    # buttons inside the add / import dialogs keep their labels.
    buttons = _dashboard_buttons(client)
    assert buttons.get("holding-add-save") == "追加"
    assert buttons.get("holding-import-run") == "インポート"
