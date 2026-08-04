---
review: f4-ledger-truncate-guard
reviewed_sha: 489946b6b3d8e018d80f2b031f368126c3cf3b69
reviewer: independent-officer
review_date: 2026-08-04
verdict: approve
---

# 独立審査意見書 — F-4 ledger TRUNCATE ガード + journal_lines 金額 CHECK

## 判定

**approve**(承認)。以下の理由による:

- 意図(TRUNCATE 穴の封鎖・DB 側の金額不正防止)は SQL として過不足なく実現されている。行トリガは PostgreSQL 仕様上 TRUNCATE を素通しするという診断は正しく、`BEFORE TRUNCATE ... FOR EACH STATEMENT` の文トリガと `REVOKE TRUNCATE` の組合せは 0021 が governance スキーマで既に採用した実効性の確認された流儀の踏襲である。
- 既存アプリ経路(`src/ryza/ledger/posting.py` L83-88)・既存シード(0006 / 0011)・既存テストの INSERT はいずれも `debit>=0 AND credit>=0 AND (debit=0 OR credit=0)` を満たしており、新設 CHECK 制約と矛盾しない(grep で全 INSERT を目視確認済み)。よって `NOT VALID` を使わず制約追加時に全行検証させる設計判断は安全であり、監査目的に対して正しい選択である。
- 実 DB(PG17 test)で `pytest tests/test_migrations.py -v` を実行し、24 件全 PASS。0035 が追加した 7 件(TRUNCATE 拒否 2 例・トリガ有効性検査・CHECK 拒否 3 例)も含めて実行され赤にならない。
- 0035 の SQL 本体を直接 3 回連続で再適用しても失敗しないことを検証し、ヘッダの「冪等」主張は事実であることを確認した。
- スコープが引き締まっている(migration は 107 行、テストは 77 行、他への副作用なし)。監査コミット単位の diff は `migrations/0035_ledger_truncate_guard.sql` と `tests/test_migrations.py` の 2 ファイル 184 行だけであり、混入変更は無い。

## 確認した事項

1. branch head と `reviewed_sha` の一致: `489946b6b3d8e018d80f2b031f368126c3cf3b69` を確認(f4-ledger-truncate-guard = 489946b)。
2. コミット単体の diff: `git show --stat 489946b` で 2 ファイル・+184 行のみ。
3. 既存 0005(ledger)・0021(governance)を精読し、追記オンリー 3 点セット(行トリガ / 文トリガ / REVOKE)の流儀との整合を確認。関数名 `ledger.forbid_truncate()` は 0021 の `governance.forbid_truncate()` とスキーマ違いで対を成す。
4. `posting.post_entry` L83-88 の非負・両建て禁止の検査ロジックと新 CHECK が等価であることを目視で確認。
5. `INSERT INTO ledger.journal_lines` を含む全ファイル(migrations/0006, 0011, src/ryza/ledger/posting.py, tests/risk/test_daily.py, tests/test_migrations.py)を grep し、負値・両建ての正当経路が無いことを確認。
6. PG バージョン(compose.yaml / ci.yml で `postgres:17`)を確認し、`CREATE OR REPLACE TRIGGER`(PG14+)が使えることを確認。
7. マイグレーションランナー(`src/ryza/db/migrate.py`)は `migrations/*.sql` をファイル名順に適用し `meta.schema_migrations` で version を記録する。0035 の命名・番号連続性は既存規約(0034 に続く)に整合。
8. 実 DB 実行: `pytest tests/test_migrations.py -v` を PG17 test DB(`ryza_test`)に対して実行、24 件全 PASS(TRUNCATE 拒否・CHECK 拒否のパラメトライズ 5 件・トリガ有効性検査を含む)。
9. 0035 SQL の 3 連続直接実行が成功することを確認(冪等性の実測)。
10. `pg_trigger.tgenabled` を実 DB で SELECT し、`journal_entries_no_truncate` / `journal_lines_no_truncate` が両表で有効(`O`)であることを確認。
11. 他の ledger 表(evidence / reconciliations / nav_snapshots / books / accounts / budgets)には既存の行トリガも文トリガも無いことを確認。0035 が 2 表に限定していることは既存の追記オンリー領域(0005 の 2 表)と一致しており、過剰でも過小でもない。

## 所見(合否には影響しない)

### 重要度: 情報(FYI)

**所見-1**: `test_ledger_integrity_triggers_exist`(test_migrations.py L108-121)は行トリガ側のみを列挙しており、`journal_entries_no_truncate` / `journal_lines_no_truncate` を含めていない。0035 が別テスト `test_ledger_truncate_guard_triggers_exist_and_are_enabled` で個別カバーしているため実害は無いが、trigger inventory の一箇所化を後日の掃除で検討する余地はある。

**所見-2**: `test_query_indexes_migration_sql_is_idempotent`(L442-457)は 0027 の SQL 単体を直接 2 回叩いて冪等性を検査するが、0035 に対する同種のテストは無い。0035 SQL 本体の連続実行が失敗しないことは本審査で外部から確認済みだが、テストコードに固定するかどうかは執筆側の判断に委ねる(合否には含めない)。

**所見-3**(範囲外・非合否)**: `ledger.evidence` / `ledger.reconciliations` は「追記オンリー」の意味論を持つはずの表だが、0005 は行トリガも文トリガも張っていない(0035 のヘッダも「対象外」と明言)。証憑・照合の書き換え可能性はゲート A-1 / A-2 の前提を静かに崩し得るため、別 PR で追記オンリー化を検討することを推奨する。この提言は本 PR の合否には含めない(指示どおり)。

**所見-4**: `REVOKE ... FROM PUBLIC` はテーブル所有者ロール(現構成でアプリと同一の `ryza`)には効かず実質 no-op であることは 0035 のヘッダで明記されており、ロール分離が `ops/reminders.yaml governance-role-separation` で追跡されている点を確認した。文トリガ側が主防壁として機能する構図は 0021 と同じ整合的な設計で、指摘に留める。

## サマリ(200 字以内)

行トリガでは TRUNCATE を捕捉できない仕様上の穴を、0021 と同型の文トリガ+REVOKE で塞ぎ、あわせて `journal_lines` にアプリ層と等価の CHECK を DB 側へ置く是正。SQL の正しさ・既存経路との整合・冪等性・スコープ限定を確認し、実 DB で 24/24 PASS を再現。合否に響く欠陥は見つからず、承認する。
