---
name: github-settings-baseline
description: GitHub リポジトリの初期設定リスト（一般、Pull Requests、ルールセット、セキュリティ、Actions、Dependabot の基準値）と、新規リポジトリへの適用手順、定期的な設定ドリフトチェックの手順。新しいリポジトリを作るときと、既存リポジトリの設定を定期確認するときに使う。
---

# GitHub リポジトリの初期設定と定期確認

新規リポジトリの初期設定リスト。基準値は ZHNG の現行設定
（2026-11-27 実測）。新規作成時はこのリストどおりに設定し、
既存リポジトリでは定期確認（ドリフトチェック）に使う。

## 基準値

### リポジトリ一般

- デフォルトブランチは `main`、可視性は Public
- Issues は有効、Wiki と Discussions は無効
- Web からのコミットへの signoff は必須にしない

### Pull Requests

- マージ方法はスカッシュのみ（マージコミットとリベースは無効）
- スカッシュのコミットタイトルは PR タイトル、本文は空
- Auto-merge は有効
- Automatically delete head branches は有効

### ルールセット（ブランチ保護はルールセットに一本化する）

- 対象はデフォルトブランチ、enforcement は active
  （ZHNG では名前は「ZHNG-Rules」）
- Repository Admin に bypass（always）を残す（緊急時の逃げ道）
- ルールは次の4つ：
  - `deletion`：ブランチの削除を禁止する
  - `non_fast_forward`：force push を禁止する
  - `pull_request`：PR を必須にする。許可するマージ方法は
    スカッシュのみ、必要承認数は 0、Code Owner レビューは不要
  - `required_status_checks`：CI の主要ジョブを必須にする
    （ZHNG では tests／python-check／docker-build／gitleaks の
    4つ）。**チェック名は各リポジトリの CI ジョブ名に
    置き換えること**。最新化の必須化（strict）はオフ
- クラシックなブランチ保護は使わない。タグ保護も設定しない

### Security

- Secret scanning は有効、Push protection も有効
- Private vulnerability reporting は有効
- Dependabot alerts は有効、Dependabot security updates
  （automated security fixes）も有効
- Code scanning は CodeQL の default setup を使う。言語は
  リポジトリに合わせる（ZHNG は actions と python）
- Secret scanning の non-provider patterns と validity checks
  は無効

### Actions

- allowed_actions は `local_only`（外部アクションを禁止する）
- SHA pinning は必須
- default_workflow_permissions は `read`
- ワークフローからの PR レビュー承認は不可
  （can_approve_pull_request_reviews は false）

### Dependabot のバージョン更新

- `.github/dependabot.yml` で、使うエコシステムを週次
  （weekly）の更新に設定する
- ZHNG の実例：pip（`/web`）、docker（`/web`）、
  docker-compose（`/`）

## 新規リポジトリへの適用手順

1. リポジトリを作成し、デフォルトブランチを `main` にする。
2. Settings → General で、一般と Pull Requests の項目を
   基準値どおりに設定する。
3. Settings → Security でセキュリティの各項目を有効にし、
   CodeQL の default setup を構成する。
4. Settings → Actions → General で、allowed_actions、
   SHA pinning、ワークフローの権限を設定する。
5. `.github/dependabot.yml` を置く。
6. CI ワークフローを入れて一度実行してから、Settings →
   Rules → Rulesets でルールセットを作る。必須ステータス
   チェックの名前は、過去に実行されたチェックからしか
   選べないため、この順番にする。
7. 最後に、下の「定期確認」のコマンドで現行値を取得し、
   基準値と照合する。

## 定期確認（ドリフトチェック）

- 頻度の目安は月次、またはリリース前の品質イベントのとき。
- 下のコマンドで現行値を取得し、基準値と突き合わせる。
  **差分があっても勝手に戻さないこと**。意図的な変更かも
  しれないので、差分を報告して、戻すかどうか判断を仰ぐ。
- 設定の変更はリポジトリ管理者が行う。Muse 側のトークンは
  Administration Read-only のため、できるのは確認
  （読み取り）のみ。

```bash
# リポジトリ一般／Pull Requests／Security の要約
gh api repos/<owner>/<repo> --jq "{default_branch, visibility, allow_squash_merge, allow_merge_commit, allow_rebase_merge, allow_auto_merge, delete_branch_on_merge, squash_merge_commit_title, squash_merge_commit_message, web_commit_signoff_required, has_issues, has_wiki, has_discussions, security_and_analysis}"

# ルールセット（一覧と詳細）
gh api repos/<owner>/<repo>/rulesets
gh api repos/<owner>/<repo>/rulesets/<ruleset-id>

# Actions の権限
gh api repos/<owner>/<repo>/actions/permissions
gh api repos/<owner>/<repo>/actions/permissions/workflow

# CodeQL の default setup
gh api repos/<owner>/<repo>/code-scanning/default-setup

# Dependabot security updates
gh api repos/<owner>/<repo>/automated-security-fixes

# Private vulnerability reporting（必ず専用エンドポイントで）
gh api -i repos/<owner>/<repo>/private-vulnerability-reporting
```

注意：

- Private vulnerability reporting は、リポジトリ情報の
  フィールドが `null` を返すことがあり、それだけでは
  有効／無効を判定できない。必ず専用エンドポイントの
  応答で確認すること。
- 必須ステータスチェックの名前は CI のジョブ名そのもの。
  CI のジョブを変更したら、ルールセット側も追随しているか
  あわせて確認すること。
