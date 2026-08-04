# T-022: ledger.evidence の不変性ガード(UPDATE/TRUNCATE 封鎖 — F-4 フォローアップ・Issue #128)

- 起草: 2026-08-04 設計リード / 前提: F-4(migrations/0035)統合済み main に対して作業
- 前提知識: CLAUDE.md、migrations/0005_ledger.sql(evidence 定義 L30-37・forbid_mutation L151-169)、migrations/0035_ledger_truncate_guard.sql(追記オンリー標準)、tests/conftest.py L112-137(`_CLEAR_EVIDENCE_SQL` / clear_residual)
- **保護領域**(スキーマ migrations・会計エンジン)。統合は設計リードが独立役員審査+みなし承認手続で行う
- 本仕様書自体を実装ブランチの最初のコミットとして `docs/tasks/T-022-ledger-evidence-guard.md` に含めること

## 問題(Issue #128)

設計書 §4 は証憑の不変保存を要求するが、`ledger.evidence` には UPDATE/DELETE/TRUNCATE ガードが一切無い(0005 の forbid_mutation は仕訳2表のみ、0035 の TRUNCATE ガードも仕訳2表のみ)。仕訳から参照されている証憑の `payload_ref` / `sha256` を UPDATE で書き換えれば、追記オンリーの仕訳証跡が指す**証憑そのものを無音で差し替え**られる。

## 是正方針(設計リード裁定 — Issue #128 の選択肢1を強化した形)

**UPDATE と TRUNCATE を封鎖し、行 DELETE は封鎖しない**。理由:

1. **UPDATE が本丸**: 参照済み証憑の改変は UPDATE でのみ可能(DELETE は FK `journal_entries.evidence_id` / `reconciliations.evidence_id` が既に拒否する)。改竄検知(A-1)の前提である sha256 の不変性は UPDATE 封鎖で DB レベルに確立する
2. **行 DELETE は FK が要所を守っている**: 消せるのは**どの仕訳・照合からも参照されない行**(取込されたが記帳に至らなかった証憑)のみ。この経路は tests/conftest.py の `clear_residual`(`_CLEAR_EVIDENCE_SQL`)が残留データ隔離に使っており、行トリガは rollback と無関係に発火するため DELETE 封鎖はテスト隔離戦略の再設計(Issue #23 テスト専用 DB 化)とセットでなければ導入できない — Issue #128 本文の分析どおり
3. よって完全追記オンリー化(選択肢2)は Issue #23 以降に送る。その判断を失伝させないため ops/reminders.yaml に登録する(下記)

アプリコードに evidence の UPDATE/DELETE 経路は存在しない(リポジトリ全域 grep で確認済み — 唯一の DELETE は conftest)。したがって本変更でアプリ側の修正は不要。

## 実装

### 1. migration `0036_ledger_evidence_guard.sql`

0035 の「追記オンリー標準」の様式・コメント流儀に従うこと。内容:

1. **UPDATE 行トリガ**: 専用関数 `ledger.forbid_evidence_update()` を新設(`RAISE EXCEPTION` のメッセージは evidence の意味論に合わせる — 「証憑は不変。訂正は新しい evidence 行を起こし逆仕訳で参照し直す」旨。0005 の forbid_mutation の「訂正は reversal_of による逆仕訳のみ」は仕訳向けの文言なので流用しない)。`CREATE OR REPLACE TRIGGER evidence_no_update BEFORE UPDATE ON ledger.evidence FOR EACH ROW`
2. **TRUNCATE 文トリガ**: 既存の `ledger.forbid_truncate()`(0035)をそのまま使う。`CREATE OR REPLACE TRIGGER evidence_no_truncate BEFORE TRUNCATE ON ledger.evidence FOR EACH STATEMENT`
3. **REVOKE**: `REVOKE UPDATE, TRUNCATE ON ledger.evidence FROM PUBLIC`(DELETE は REVOKE しない — 未参照行の削除は正当な経路として残す)。0035 と同じく「所有者ロールには no-op、ロール分離後の統制とドキュメント上の意図表明」の注記を書く
4. **DELETE を封鎖しない理由を migration コメントに明記**(上記裁定 1〜3 の要約+Issue #23/#128 参照)。将来の読者が「ガード漏れ」と誤認しないため
5. **カタログ更新**: `COMMENT ON TABLE ledger.evidence` を実態に合わせて更新(不変性の範囲: UPDATE/TRUNCATE 禁止・参照済み行は FK により DELETE 不可・未参照行の DELETE は可)。新関数にも COMMENT
6. 冪等: 関数は CREATE OR REPLACE、トリガは CREATE OR REPLACE TRIGGER(0035 の流儀)

### 2. ops/reminders.yaml への登録

`ledger-evidence-full-append-only` を追加: Issue #23(テスト専用 DB 化)完了後に evidence の DELETE 封鎖(完全追記オンリー化)を再評価する、という趣旨。既存エントリの様式(id / due / owner / status / body)に従う。due は Issue #23 に依存するため日付固定でなく条件記載でよい(既存に前例があればそれに従う)

### 3. conftest は変更しない

`_CLEAR_EVIDENCE_SQL` は未参照行の DELETE であり本ガードに抵触しない。**変更せずそのまま通ることがこの設計の受け入れ根拠**(壊れたら裁定の前提が誤り — 実装で吸収せず設計リードに差し戻すこと)

## テスト(tests/ledger/ — 既存のテスト配置流儀に従う)

DB を使うテストは既存の conn / run_id フィクスチャの流儀に従う(rollback 前提・commit しない)。

1. **UPDATE 封鎖**: evidence 行(参照の有無どちらでも)への UPDATE が例外で拒否される
2. **TRUNCATE 封鎖**: `TRUNCATE ledger.evidence` が例外で拒否される(CASCADE 側も 0035 で塞がれている journal 2 表経由と独立に、単体で確認)
3. **参照済み行の DELETE は FK が拒否**: journal_entries から参照される evidence 行の DELETE が FK 違反で失敗する(既存挙動の固定 — 本ガードの裁定が依存する前提のリグレッション検知)
4. **未参照行の DELETE は通る**: どこからも参照されない evidence 行は DELETE できる(conftest 経路の保全)
5. 既存スイート全通過(特に conftest の clear_residual を使う全テストが無変更で通ること)

## 受け入れ基準

全テスト+ruff 通過 / conftest 無変更 / migration は冪等(2回適用でエラーなし — テスト DB で確認)/ 既存 migration の書き換えなし(migrations は追記オンリーの保護領域)/ LLM 非関与 / コミットは日本語+`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`、push しない(統合は設計リードが行う)
