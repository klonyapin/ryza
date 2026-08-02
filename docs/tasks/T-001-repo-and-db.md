# T-001: リポジトリ骨格と DB マイグレーション基盤

- 発行日: 2026-08-02 / 発行者: 設計リード(Fable)
- 前提知識: このファイルと参照文書だけで実装可能。会話履歴は不要
- 必読: `CLAUDE.md`(不変原則)、`docs/design/10-data-accounting.md`(スキーマ定義の正)

## ゴール

Python プロジェクトの骨格と、PostgreSQL 17(pgvector + pg_partman)に対する SQL マイグレーション基盤を作り、`docs/design/10-data-accounting.md` の 5 スキーマ(market / docs / trade / ledger / meta)を全テーブル分マイグレーションとして実装する。

## 技術規約

- Python 3.12+、パッケージ管理は `uv`(pyproject.toml)。lint は `ruff`、テストは `pytest`
- DB 接続は `psycopg`(v3)。ORM は使わない(SQL が正。設計書の DDL をそのまま原典とする)
- マイグレーションは `migrations/NNNN_name.sql` の連番 SQL ファイル+自作の薄いランナー `ryza/db/migrate.py`(適用済みを `meta.schema_migrations` に記録、再実行は冪等)
- ローカル開発 DB: `docker/postgres/Dockerfile`(postgres:17 ベースに `postgresql-17-pgvector` と `postgresql-17-partman` を apt で導入)+ `compose.yaml`

## ディレクトリ構成(作成するもの)

```
pyproject.toml
compose.yaml
docker/postgres/Dockerfile
migrations/0001_meta.sql        -- meta.runs, meta.schema_migrations, meta.lineage_edges, meta.audit_findings
migrations/0002_market.sql      -- instruments, bars(パーティション親+pg_partman 設定), indicators
migrations/0003_docs.sql        -- documents, embeddings(vector 1024, HNSW), market_view, research_reports
migrations/0004_trade.sql       -- signals, order_intents, orders, fills
migrations/0005_ledger.sql      -- books, accounts, evidence, journal_entries, journal_lines, nav_snapshots, reconciliations, budgets
migrations/0006_seed.sql        -- 帳簿・勘定科目の初期データ(下記)
src/ryza/__init__.py
src/ryza/db/__init__.py
src/ryza/db/migrate.py
src/ryza/db/conn.py             -- 接続ヘルパー(環境変数 RYZA_DATABASE_URL)
tests/test_migrations.py
```

## スキーマ実装の要点(設計書 10-data-accounting.md に完全準拠)

1. DDL は設計書 §2〜§6 のとおり。カラム追加・省略をしない。疑問があれば TODO コメントを残して設計書どおりに実装
2. `market.bars` は `PARTITION BY RANGE (ts)`、pg_partman で月次パーティション自動管理(`run_maintenance()` は後続タスクで pg_cron 化。本タスクでは partman 登録まで)
3. **ledger の整合性制約**(最重要・テスト必須):
   - `journal_entries.evidence_id` は NOT NULL(証憑必須)
   - 仕訳単位で Σdebit = Σcredit を CONSTRAINT TRIGGER(DEFERRABLE INITIALLY DEFERRED)で強制
   - `journal_lines.book_id` が親 entry の book_id と不一致なら挿入拒否(トリガ)= 帳簿混合の物理的禁止
   - `journal_entries` / `journal_lines` への UPDATE・DELETE を禁止(REVOKE + トリガ)。訂正は `reversal_of` による逆仕訳のみ
4. 全テーブルに `COMMENT ON TABLE / COLUMN` を付ける(データカタログの源泉)

## シード(0006_seed.sql)

- `ledger.books`: `DEMO_FUND`(fund, JPY, is_real_money=false)/ `OPS`(ops, JPY, true)。`LIVE_FUND` はまだ作らない
- `ledger.accounts`: 設計書 §5「勘定科目表(初期セット)」の全科目を両帳簿に登録
- **開始仕訳(DEMO_FUND)**: 借方 `cash` ¥1,000,000 / 貸方 `capital` ¥1,000,000(2026-08-02 投資委員会決定)。証憑は kind='decision'、payload に決定記録の JSON を保存した evidence 行を作って紐付ける

## 受け入れ基準(pytest で自動検証)

- [ ] `uv run python -m ryza.db.migrate` が空 DB に全マイグレーションを適用し、再実行しても冪等
- [ ] 5 スキーマ・全テーブル・全制約が存在する(information_schema で検証)
- [ ] 貸借不一致の仕訳が INSERT できない
- [ ] evidence_id なしの仕訳が INSERT できない
- [ ] 親 entry と異なる book_id の journal_line が INSERT できない
- [ ] journal_entries への UPDATE が拒否される
- [ ] シード適用後、DEMO_FUND の試算表残高: cash 借方 ¥1,000,000 / capital 貸方 ¥1,000,000
- [ ] `ruff check` パス

## 非ゴール

会計エンジンの記帳 API(T-002)、証憑ストア GCS 実装(T-003)、取込ジョブ、GCP デプロイ。

## 完了時

コミットメッセージ: `feat(db): 5スキーマのマイグレーション基盤と帳簿制約 (T-001)`。不明点・設計書との矛盾を見つけたら実装せず `docs/tasks/T-001-questions.md` に書いて停止すること。
