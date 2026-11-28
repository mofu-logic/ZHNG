"""Playwright によるブラウザテスト（描画の回帰＋E2Eスモーク）の基盤。

通常の pytest スイート（tests/）とは分離しており、既定の収集
（pytest.ini の testpaths = tests）には混ざらない。実行方法は
.agents/skills/run-e2e-tests/SKILL.md を参照。

構成:
- テスト用 DB（stock_test_e2e）にスキーマを適用し、テストごとに
  全表を空にして最小シードを入れ直す（テスト間の独立性）。
- アプリは TestClient ではなく、uvicorn の実サーバとして
  セッション専用に起動し、/healthz で起動完了を待つ。
- テスト中に外部アクセスは発生させない。対象フローは銘柄マスタ
  登録済みのコードだけで完結させ、夜間バッチの開始時刻は
  「少し過去」にずらして、実行中はスケジューラが発火しないように
  する（次回発火は約24時間先になる）。ガード値はサーバ起動前にも
  先行投入し、起動直後・初回シード前の窓も閉じる。
- 失敗時は tests_e2e/artifacts/ にスクリーンショットを残す
  （.gitignore 済み）。
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
TEST_DB_NAME = "stock_test_e2e"

sys.path.insert(0, str(REPO_ROOT / "web"))

TABLES = [
    "app_logs",
    "benefit_memos",
    "candidates",
    "dividend_changes",
    "daily_snapshots",
    "price_points",
    "fetch_runs",
    "sales",
    "buy_lots",
    "dividend_benefits",
    "holdings",
    "holding_events",
    "stocks",
    "settings",
]


# ------------------------------------------------------------ DB 接続先

def _read_db_password():
    """compose の db パスワードを .env から読む（値はログに出さない）。

    worktree には .env が無いので、本体ツリー（git-common-dir の
    親）の .env を見る。
    """
    candidates = [REPO_ROOT / ".env"]
    try:
        common = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        candidates.append(Path(common).resolve().parent / ".env")
    except Exception:
        pass
    for env_path in candidates:
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("DB_PASSWORD="):
                    return line.split("=", 1)[1].strip()
    raise RuntimeError(
        ".env に DB_PASSWORD が見つかりません。"
        " E2E_DATABASE_URL を直接指定してください"
    )


def _db_container_ip():
    """compose の db コンテナの IP を引く（ホストから直接届く）。"""
    cid = subprocess.run(
        [
            "docker", "ps", "-q",
            "--filter", "label=com.docker.compose.project=stock-app",
            "--filter", "label=com.docker.compose.service=db",
        ],
        capture_output=True, text=True,
    ).stdout.split()
    if not cid:
        raise RuntimeError(
            "compose の db コンテナが見つかりません（本体スタックが"
            "起動しているか確認してください）。"
            " E2E_DATABASE_URL を直接指定しても構いません"
        )
    ip = subprocess.run(
        [
            "docker", "inspect", "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            cid[0],
        ],
        capture_output=True, text=True,
    ).stdout.strip()
    if not ip:
        raise RuntimeError("db コンテナの IP を取得できませんでした")
    return ip


def _resolve_database_url():
    url = os.environ.get("E2E_DATABASE_URL")
    if url:
        return url
    return "postgresql://stock:{}@{}:5432/{}".format(
        _read_db_password(), _db_container_ip(), TEST_DB_NAME
    )


# config.py は import 時に DATABASE_URL を読むため、app.db の
# import より前にテスト用 DB を指しておく（SCHEMA の取得専用で、
# アプリ本体をこのプロセスで起動するわけではない）。
os.environ["DATABASE_URL"] = _resolve_database_url()

from app.db import SCHEMA  # noqa: E402


# ------------------------------------------------------------ サーバ

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def db_url():
    return os.environ["DATABASE_URL"]


def _guard_fetch_start_time():
    """夜間バッチ開始時刻のガード値（JST の現在時刻−2分、"HH:MM"）。

    この値を fetch_start_time に入れておくと、スケジューラの次回
    発火は約24時間先になり、テスト実行中は発火しない（＝Yahoo へ
    出ない）。サーバ起動前の先行投入と、各テストのシードで共用する。
    """
    guard = datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(minutes=2)
    return guard.strftime("%H:%M")


@pytest.fixture(scope="session")
def app_server(db_url):
    """アプリを uvicorn の実サーバとして起動する（セッション共有）。"""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(SCHEMA)
        # 初回シードより前にサーバが起動する。settings が空のままだと
        # スケジューラは既定値（01:00）で次回発火を計算し、起動が
        # 01:00 直前に重なると初回シードの投入前に発火し得るため、
        # ガード値を起動前に先行投入する（各テストのシードが後から
        # 同じ考え方の値で入れ直す）。前回の実行で残った行があっても
        # 壊れないよう upsert にする。
        conn.execute(
            "INSERT INTO settings(key, value)"
            " VALUES ('fetch_start_time', %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (_guard_fetch_start_time(),),
        )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    log_path = ARTIFACTS_DIR / "server.log"
    env = dict(
        os.environ,
        DATABASE_URL=db_url,
        PYTHONPATH=str(REPO_ROOT / "web"),
    )
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(port),
            ],
            cwd=str(REPO_ROOT), env=env, stdout=log, stderr=log,
        )
        try:
            deadline = time.time() + 60
            while time.time() < deadline:
                if proc.poll() is not None:
                    tail = log_path.read_bytes()[-2000:].decode(
                        "utf-8", "replace"
                    )
                    raise RuntimeError(
                        f"アプリサーバが起動前に終了しました:\n{tail}"
                    )
                try:
                    with urllib.request.urlopen(
                        base_url + "/healthz", timeout=2
                    ) as res:
                        if res.status == 200:
                            break
                except Exception:
                    time.sleep(0.25)
            else:
                raise RuntimeError("アプリサーバの起動がタイムアウトしました")
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# ------------------------------------------------------------ シード

# 既存スイート（tests/conftest.py の seed_settings）と同じ値。
_SEED_SETTINGS = {
    "tax_rate": "0.20315",
    "target_dividend_total": "1200000",
    "target_monthly_income": "79685",
    "target_yield_buy": "0.03",
    "target_yield_now": "0.035",
    "target_yield_portfolio": "0.04",
}


def _seed(conn):
    conn.execute(
        "TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"
    )
    # 銘柄マスタ: 4444 は保有なし（追加系テストの対象）。
    conn.execute(
        "INSERT INTO stocks(code, name, market) VALUES"
        " ('1111', 'テスト電気', '東証'),"
        " ('2222', 'サンプル食品', '東証'),"
        " ('3333', 'モデル銀行', '東証'),"
        " ('4444', 'ノート工業', '東証')"
    )
    conn.execute(
        "INSERT INTO holdings(code, account_type, avg_price, shares)"
        " VALUES"
        " ('1111', '特定', 800, 100),"
        " ('2222', '特定', 1500, 200),"
        " ('3333', 'NISA', 2000, 50)"
    )
    conn.execute(
        "INSERT INTO buy_lots(code, name, avg_price, shares,"
        " purchase_date, status, account_type) VALUES"
        " ('1111', 'テスト電気', 800, 100, DATE '2026-01-15',"
        "  '約定済', '特定'),"
        " ('2222', 'サンプル食品', 1500, 200, DATE '2026-02-20',"
        "  '約定済', '特定'),"
        " ('3333', 'モデル銀行', 2000, 50, DATE '2026-03-10',"
        "  '約定済', 'NISA')"
    )
    conn.execute(
        "INSERT INTO price_points(code, captured_at, price) VALUES"
        " ('1111', now(), 1000),"
        " ('2222', now(), 1600),"
        " ('3333', now(), 2100)"
    )
    conn.execute(
        "INSERT INTO dividend_benefits(code, dividend_forecast,"
        " dividend_auto, benefit_value) VALUES"
        " ('1111', 40, 40, 5000),"
        " ('2222', 30, 30, 0)"
    )
    settings = dict(_SEED_SETTINGS)
    # 夜間バッチの開始時刻を「少し過去」に固定し、テスト実行中は
    # スケジューラが発火しない（＝Yahoo へ出ない）ようにする。
    settings["fetch_start_time"] = _guard_fetch_start_time()
    for key, value in settings.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (%s, %s)",
            (key, value),
        )
    conn.commit()


@pytest.fixture()
def seeded(db_url, app_server):
    """テストごとに全表を空にして最小シードを入れ直す。"""
    with psycopg.connect(db_url) as conn:
        _seed(conn)


# ------------------------------------------------------------ ブラウザ

@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, "rep_" + rep.when, rep)
    return rep


@pytest.fixture()
def page(browser, seeded, request):
    context = browser.new_context(
        viewport={"width": 1280, "height": 800}, locale="ja-JP"
    )
    page = context.new_page()
    page.set_default_timeout(15000)
    yield page
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        name = request.node.nodeid.replace("/", "_").replace("::", "__")
        try:
            page.screenshot(
                path=str(ARTIFACTS_DIR / f"{name}.png"), full_page=True
            )
        except Exception:
            pass
    context.close()

