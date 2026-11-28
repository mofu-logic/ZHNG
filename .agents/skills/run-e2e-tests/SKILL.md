---
name: run-e2e-tests
description: Playwright のブラウザテスト（描画の回帰＋E2Eスモーク）を開発ホストで実行する。画面の描画や操作フローを変更したとき、およびリリース前のイベントで使う。通常の CI には入れない。
---

# e2e テストを実行する

`tests_e2e/` は Playwright で実ブラウザを操作するテスト層。通常の
pytest スイート（`tests/`、文字列と API で固定する層）では検出
できない「描画の崩れ」と「配線はあるが動かない」問題を固定する
ためのもので、対象は少数に絞る。

- 描画の回帰（`test_rendering.py`）：日付セルが1行に収まること、
  設定スイッチの右端位置がそろうことを、描画結果の数値で固定する。
- E2E スモーク（`test_smoke.py`）：保有追加の結果ダイアログ、表の
  ソート、確認ダイアログ設定のオン／オフ、購入登録の保有への
  連動といった主要フローだけを通す。

## 方針：CI には入れない

ブラウザのダウンロードが重いため、毎回の CI には入れない。
実行タイミングは、画面まわりを変更したときのローカル確認と、
リリース・デプロイ前のイベント（Hypothesis の heavy プロファイル
と同じ節目）。通常スイートとは分離してあり、既定の収集
（`pytest.ini` の `testpaths = tests`）には混ざらない。通常
スイートの件数とカバレッジゲート（分岐95%）にも影響しない。

## 前提

- 本体スタック（compose の `db`）が起動していること。アプリは
  テスト用 DB に対して conftest が uvicorn の実サーバとして
  起動する（TestClient ではない）。
- e2e 用 DB `stock_test_e2e` を1回だけ作る（本体ツリーで）：

```sh
cd ~/stock-app && docker compose exec db createdb -U stock stock_test_e2e
```

## 初回セットアップ（開発ホスト）

```sh
python3 -m venv .venv
.venv/bin/pip install -r web/requirements.txt \
  -r tests_e2e/requirements-e2e.txt
.venv/bin/playwright install --only-shell chromium
sudo .venv/bin/playwright install-deps chromium
```

- `python3 -m venv` が ensurepip 不足で失敗する環境では、先に
  `sudo apt-get install -y python3.14-venv`（バージョンは環境の
  Python に合わせる）。
- `install-deps` はブラウザの共有ライブラリを apt で入れる
  （初回のみ）。ブラウザ本体は headless shell で十分。

## 実行

```sh
.venv/bin/python -m pytest tests_e2e -v
```

## 仕組み（conftest）

- DB 接続先は `E2E_DATABASE_URL` があればそれを使い、無ければ
  compose の db コンテナの IP と本体ツリーの `.env` の
  `DB_PASSWORD` から組み立てる。
- スキーマを適用し、アプリを空きポートで起動して `/healthz`
  で起動完了を待つ。ビューポートは 1280px に固定する。
- テストごとに全表を TRUNCATE し、最小シード（銘柄マスタ4件・
  保有3件・売買履歴3件・設定）を入れ直す。
- 外部アクセスは発生させない。フローは銘柄マスタ登録済みの
  コードだけで完結させ、夜間バッチの開始時刻はシードで
  「少し過去」にずらす（スケジューラの次回発火は約24時間先に
  なり、実行中に Yahoo へ出ない）。
- 失敗時は `tests_e2e/artifacts/` にスクリーンショットと
  サーバログ（`server.log`）が残る（.gitignore 済み）。

## 注意

- 保存系フローでは、API の応答を待ってから画面遷移すること
  （待たずに遷移するとリクエストが中断され、設定や登録が
  反映されないことがある）。`test_smoke.py` の設定保存が実例。
- ダイアログは `showDialog` が body 直下に動的生成するものと、
  各画面に静的に置かれた入力ダイアログが別物。セレクタは
  `tests_e2e/helpers.py` の形（body 直下・最前面）を使うこと。
