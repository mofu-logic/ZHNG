"""The word for an account on screen is 口座, everywhere.

Owner decisions pinned here:

- User-facing wording says 口座, never 口座区分: the add-dialog
  labels on the history screen and the dashboard, the import
  dialog's mapping label, and the error messages a rejected
  account type produces (the API validation message and the CSV
  import row reason). The internal name account_type, the DB
  column and the API shape do not change, and neither do code
  comments.

- The dashboard's account filter always offers the whole
  allowlist （特定 / NISA / 普通, in ACCOUNT_TYPES order), not
  just the accounts that happen to hold something today: with no
  普通 holding, the filter used to have no 普通 option at all.
  The filtering itself is the page's client-side pass over each
  row's data-account attribute, so the pins here are the option
  list and the row attributes it matches against.
"""
import re

from conftest import seed_stock_holding


def _seed_stock(db, code="9999", name="テスト銘柄"):
    db.execute(
        "INSERT INTO stocks(code, name, market) VALUES (%s, %s, '東証')",
        (code, name),
    )
    db.commit()


def _seed_holding(db, code="9999", account_type="特定", avg_price=800,
                  shares=100):
    _seed_stock(db, code)
    db.execute(
        "INSERT INTO holdings(code, account_type, avg_price, shares)"
        " VALUES (%s, %s, %s, %s)",
        (code, account_type, avg_price, shares),
    )
    db.commit()


# ------------------------------------------------------------- wording


def test_history_dialog_labels_say_kouza(client, db):
    page = client.get("/history").text
    assert "口座区分" not in page
    # Both add dialogs label the account select 口座.
    labels = re.findall(
        r"<label>口座\s*<select name=\"account_type\"", page
    )
    assert len(labels) == 2


def test_dashboard_labels_say_kouza(client, db):
    seed_stock_holding(db)
    page = client.get("/").text
    assert "口座区分" not in page
    # The holding-add dialog labels the account select 口座.
    assert re.search(r"<label>口座\s*<select name=\"account_type\"", page)
    # The import dialog asks for the 口座 column.
    assert "口座の列" in page


def test_buy_rejected_account_message_says_kouza(client, db):
    _seed_stock(db)
    r = client.post(
        "/api/v1/buys",
        json={
            "code": "9999",
            "name": "テスト銘柄",
            "avg_price": 1000,
            "shares": 10,
            "status": "約定済",
            "account_type": "一般",
        },
    )
    assert r.status_code == 422
    assert "口座は特定・NISA・普通のいずれかを指定してください" in r.text
    assert "口座区分" not in r.text


def test_import_rejected_account_reason_says_kouza(client, db):
    _seed_stock(db)
    text = "コード,口座,平均取得単価,株数\n9999,一般,800,100\n"
    r = client.post(
        "/api/v1/holdings/import",
        files={"file": ("holdings.csv", text.encode("utf-8"), "text/csv")},
        data={
            "code_col": "コード",
            "account_type_col": "口座",
            "avg_price_col": "平均取得単価",
            "shares_col": "株数",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["failed"] == 1
    reason = body["failures"][0]["reason"]
    assert "口座が不正です（特定・NISA・普通のいずれか）" in reason
    assert "口座区分" not in reason


# -------------------------------------------------------- filter options


def _filter_options(page):
    m = re.search(r'<select id="f-account">(.*?)</select>', page, re.S)
    assert m is not None
    return re.findall(r"<option[^>]*>([^<]*)</option>", m.group(1))


def test_account_filter_lists_all_types_without_futsu_holding(client, db):
    # Holdings exist only under 特定: the filter still offers the
    # whole allowlist, in ACCOUNT_TYPES order.
    _seed_holding(db, account_type="特定")
    page = client.get("/").text
    assert _filter_options(page) == ["すべて", "特定", "NISA", "普通"]


def test_account_filter_keeps_allowlist_order_with_all_holdings(client, db):
    _seed_holding(db, code="9999", account_type="特定")
    _seed_holding(db, code="1111", account_type="NISA")
    _seed_holding(db, code="2222", account_type="普通")
    page = client.get("/").text
    assert _filter_options(page) == ["すべて", "特定", "NISA", "普通"]
    # The rows carry the account the filter matches against, so
    # choosing 普通 keeps exactly the 普通 row.
    assert 'data-account="普通"' in page
    assert 'data-account="特定"' in page
    assert 'data-account="NISA"' in page
