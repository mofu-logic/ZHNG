---
name: run-tests
description: CI と同じ条件で pytest スイート（分岐カバレッジ付き）を使い捨ての PostgreSQL に対して実行する。web/app を変更して push する前、およびテストを追加するときに使う。
---

# テストを実行する

スイートは3層構成：

- `tests/test_format.py`、`test_helpers.py`、`test_parse.py` — 純粋関数
  （整形、フォーム変換、Yahoo ページのパース）。DB 不要。
- `tests/test_endpoints.py` — 実 PostgreSQL に対する画面と計算
  （ダッシュボードの KPI、売買 API の往復、約定状態と損益の
  自動算出、設定の自動算出、サマリーのフォールバック）。
- `tests/test_api.py` — `/api/v1` の OpenAPI ドキュメント、
  参照系、バリデーション。
- `tests/test_logging.py` — 構造化ログ（OTel）の出力形、イベント
  記録、/logs 画面の絞り込み。
- `tests/test_fetch.py` — `httpx` をフェイククライアントに差し替えて
  夜間取得パイプラインを通しで実行（価格、スナップショット、配当の
  自動更新と変更記録、日経取得失敗の分離）。

DB 依存テストは `TEST_DATABASE_URL` を読む。テスト用 DB に接続
できない場合は**失敗ではなくスキップ**になるため、スキップありの
全合格は完全な検証ではない。必ずスキップ件数を確認すること。

## compose スタックに対して実行する（開発ホスト）

実行中の `db` コンテナにテスト用データベースを1回だけ作る：

```sh
docker compose exec db createdb -U stock stock_test
```

その後、web イメージから作った使い捨てコンテナで、compose の
ネットワークに接続し、リポジトリをマウントして実行する：

```sh
PW=$(grep -E "^DB_PASSWORD=" .env | cut -d= -f2-)
docker run --rm --network stock-app_default -v "$PWD":/repo -w /repo \
  -e TEST_DATABASE_URL="postgresql://stock:${PW}@db:5432/stock_test" \
  stock-app-web:latest sh -c \
  "pip install -q -r web/requirements-dev.txt; \
   python -m pytest tests --cov=app --cov-report=term-missing -q"
```

## カバレッジ

- 分岐カバレッジ（C1）は `.coveragerc` で有効化。CI は 95% 未満を
  失敗とする。100% を追わないこと：スケジューラのループ本体
  （01:00 まで sleep する）は意図的に対象外。
- 機能を足したら、対応する層にテストを足すこと。取得まわりの変更
  では、より深い内部をモックするのではなく、`tests/test_fetch.py`
  のフェイククライアントを拡張する。
- テストが本番バグを見つけるのは成功であって迷惑ではない：
  バグを直し、テストを回帰防止として残し、コミット本文に
  何が起きたかを書くこと。
