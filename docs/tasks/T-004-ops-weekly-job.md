# T-004: 週次運用ジョブ ops-weekly(経営管理部の最初の本番ジョブ)

- 発行日: 2026-08-02 / 発行者: 設計リード(Fable)/ 依存: なし(T-001〜003 と独立。ただし git 競合回避のため T-001 完了後に着手)
- 必読: `CLAUDE.md`、`ops/reminders.yaml`(v2 スキーマがこのジョブの仕様)

## ゴール

アプリやセッションに依存せず GCP 上で毎週動く運用ジョブを実装する: ①リマインダー条件の評価と発火 ②週次進捗ダイジェストの投稿。これは経営管理部ジョブの第1号であり、以後の日次バッチの雛形になる。

## 構成

```
src/ryza/ops/weekly.py     -- 本体(条件エバリュエータ+アクション実行+ダイジェスト)
src/ryza/ops/github.py     -- GitHub API 薄クライアント(PAT、Issue コメント/作成、repo clone不要のAPI操作)
docker/ops/Dockerfile      -- python:3.12-slim ベース
ops/deploy-ops-weekly.sh   -- デプロイスクリプト(下記コマンド一式)
tests/ops/
```

## 仕様

1. **リポジトリ取得**: GitHub API で `ops/reminders.yaml` と `docs/tasks/` ファイル一覧を取得(git clone せず contents API で足りる)。認証は Secret Manager の `github-token`(fine-grained PAT)
2. **条件評価**: reminders.yaml v2 の conditions(OR)を評価。type は date_after / issue_label_open / task_file_glob / bq_table_missing(bq は google-cloud-bigquery、ジョブのサービスアカウント権限で)
3. **発火**: action を実行(issue_comment / issue_create、`only_if` があれば追加条件)。発火した項目は reminders.yaml の status を `fired: <ISO日付>` に更新し、GitHub contents API でコミット(メッセージ: `chore(ops): reminder <id> fired`+Co-Authored-By 行)
4. **ダイジェスト**: 直近7日の commits(GitHub API)と Issues の変化を集計し、専用 Issue「週次ダイジェスト」(無ければ label `digest` で作成)にコメント: 今週のコミット数と主な変更 / OPEN Issue の状態 / 停滞警告(7日更新なしの impl Issue)/ 発火したリマインダー
5. **冪等性**: 同一週に再実行しても二重コメント・二重発火しない(status と当週コメント有無で判定)
6. **dry-run**: 環境変数 `DRY_RUN=1` で書き込みせずログのみ

## デプロイ(スクリプト化して ops/deploy-ops-weekly.sh に)

> **2026-08-04 追記(構成変更)**: 実行環境は Cloud Run Job + Cloud Scheduler から
> **GCE VM(ryza-bot)の systemd timer**(`ryza-ops-weekly.timer`)へ移した。理由は
> 週次ダイジェストの「決議の批判経由」(`BOARDROOM_AUDIT`)が VM 内 PostgreSQL の
> `governance.minute_resolutions` を読むためで、Cloud Run からは届かない
> (決議精緻化審査 2026-08-03 懸念4 の配線)。以下の Cloud Run 記述は**旧構成の記録**であり、
> 正は `ops/deploy-ops-weekly.sh` 冒頭の構成判断。Cloud SQL 移行後に再検討する。

- プロジェクト `ryza-main`。イメージは Artifact Registry(repo: `ryza`, region us-west1)
- Cloud Run Job `ops-weekly`(region us-west1、SA はデフォルト。BigQuery データ閲覧ロールを付与)
- Secret `github-token` を env 経由でマウント(値の投入はユーザー作業 — Issue 参照)
- Cloud Scheduler: `0 1 * * 1`(UTC。JST 月曜10:00)で Cloud Run Job を起動(OAuth、Cloud Run Invoker)

## 受け入れ基準

- [ ] 条件エバリュエータ4種の単体テスト(フィクスチャで真偽両方)
- [ ] 冪等性テスト(同週2回実行で書き込み1回)
- [ ] DRY_RUN=1 でエンドツーエンド(GitHub API はモック)
- [ ] deploy スクリプトが冪等(再実行可能)
- [ ] 実デプロイ後、手動実行(`gcloud run jobs execute ops-weekly`)で週次ダイジェストが Issue に投稿される
- [ ] `ruff check` パス

## 完了時

コミット: `feat(ops): 週次運用ジョブ ops-weekly (T-004)`。デプロイ完了を確認したら、ローカルの Claude 定例タスク廃止を最終報告に明記(廃止操作自体は設計リードが行う)。
