---
review: t022-ledger-evidence-guard
reviewed_sha: 8596d2b22eefb171ca4d8bf0d58fe4a8de897df5
reviewer: independent-review-agent (opus)
review_date: 2026-08-04
verdict: approve
---

# T-022 ledger.evidence 不変性ガード 独立審査意見書

## 対象と実差分

- reviewed_sha: 93e71afc8f45cf01b8635eff60ddc7ee8f358b3d(origin/t022-ledger-evidence-guard)
- 実差分(merge-base origin/main..FETCH_HEAD, 4 files, +326 -0):
  - `docs/tasks/T-022-ledger-evidence-guard.md` (新規・55 行)
  - `migrations/0036_ledger_evidence_guard.sql` (新規・86 行)
  - `ops/reminders.yaml` (+12 行 — `ledger-evidence-full-append-only` 追加)
  - `tests/ledger/test_evidence_guard.py` (新規・173 行、10 テスト)

## 実行した検証

1. worktree を `/tmp/review-t022` に作成、共有 DB(15432)に対して tests/ledger/test_evidence_guard.py 実行 → **10 passed in 52.26s**
2. migration 0036 の冪等性: `psql -f migrations/0036_ledger_evidence_guard.sql` を再実行 → エラーなし(`CREATE FUNCTION / COMMENT / CREATE TRIGGER x2 / REVOKE / COMMENT` すべて成功、`CREATE OR REPLACE` の効果を確認)
3. information_schema による FK 列挙(SELECT 読み取りのみ):
   ```
   SELECT ... FROM information_schema.table_constraints ... WHERE ccu.table_schema='ledger' AND ccu.table_name='evidence';
   → ledger.journal_entries.evidence_id, ledger.reconciliations.evidence_id
   ```
   evidence を FK 参照する表は仕様書記載の 2 表のみ。他スキーマの `%evidence%` 名の列(`trade.fills.evidence_id`, `docs.regime_flip_evidence.evidence_row_id`, `meta.audit_findings.resolved_evidence`, `trading.fm_theses.evidence_refs`)は FK ではない(独立命名)。したがって「未参照行の DELETE は FK が守らないケースはない」という論法の前提は成立している。
4. pg_trigger による適用確認: `evidence_no_update`(tgtype=19, BEFORE UPDATE FOR EACH ROW)、`evidence_no_truncate`(tgtype=34, BEFORE TRUNCATE FOR EACH STATEMENT)。両方 tgenabled='O'(Origin — 有効)。
5. アプリコードの grep(`src/**`): `ledger.evidence` への SELECT / INSERT / JOIN しか存在せず、UPDATE / DELETE / TRUNCATE 経路なし(仕様書の主張どおり)。DELETE は `tests/conftest.py._CLEAR_EVIDENCE_SQL`(未参照行のみ削除する WHERE 節付き)にのみ存在。
6. migration 番号衝突: main 最新は 0035、本 PR は 0036 で連番。0027〜0036 の並びに欠番なし。
7. PostgreSQL バージョン確認: 17.10(`CREATE OR REPLACE TRIGGER` は PG14 以降で有効 — 動作可)。

## 所見(番号付き・重大度別)

### (1) [情報] 仕様適合: 仕様書と実装の各要件が 1:1 で対応している

仕様書 §実装1〜6 と migration 0036 の照合:
- 1. UPDATE 行トリガ + 専用関数 `ledger.forbid_evidence_update()`(evidence 意味論の文言、`TG_OP` 埋め込み)→ 実装 L35-52 で確認。「証憑は不変。訂正は新しい evidence 行を起こし、それを参照する逆仕訳で参照し直す」文言。0005 の forbid_mutation 流用回避も守られている。
- 2. TRUNCATE 文トリガ + 0035 の `ledger.forbid_truncate()` 流用 → 実装 L60-62 で確認。関数を新設せず流用しており「統制表面を一本化」の意図と整合。
- 3. `REVOKE UPDATE, TRUNCATE ON ledger.evidence FROM PUBLIC`(DELETE は REVOKE しない) → 実装 L72 で確認。0035 と同じ「ロール分離後の統制と意図表明」の注記もあり(L67-71)。
- 4. DELETE を封鎖しない理由の migration コメント記載 → 実装 L10-24 に裁定 1〜3 の要約+Issue #23/#128 参照あり。
- 5. `COMMENT ON TABLE ledger.evidence` 更新 → 実装 L79-86。新関数の COMMENT も L45-48 にあり。
- 6. 冪等性(CREATE OR REPLACE FUNCTION / TRIGGER) → 実装で採用、実 DB で 2 回目適用成功を確認。

受け入れ基準(仕様書 §受け入れ基準)も全て満たしている: 全テスト+ruff は 10 tests passed(ruff は本レビューでは実行せず — 指示が tests/ledger 限定)、conftest 無変更、migration 冪等、既存 migration 書き換えなし、LLM 非関与、コミット末尾は `Co-Authored-By: Claude Fable 5`(git log で確認)、push なし。

### (2) [情報] 適用範囲分析(a): UPDATE/TRUNCATE 以外の変更経路

**検出対象外だが理論上残っている経路**を列挙する(いずれも本 migration の射程外・保護領域の別問題):
- **トリガの無効化**: 所有者ロール `ryza`(現構成でアプリと兼用)は `ALTER TABLE ledger.evidence DISABLE TRIGGER USER` 一発でトリガを無音化できる。テスト 6(`test_evidence_guard_triggers_exist_and_are_enabled`)が `tgenabled='O'` を固定するリグレッションになっており、監査ジョブが定期的に読めば `D` のまま戻し忘れは検出できる。0024/0026/0035 と同じ基準で、本 PR に追加要求はない。
- **関数の CREATE OR REPLACE 差し替え**: `ledger.forbid_evidence_update()` / `ledger.forbid_truncate()` の本体を `CREATE OR REPLACE FUNCTION` で書き換えれば実質的にガードを無効化できる。これは保護領域(migrations の追記オンリー保護)と PR レビューで統制する層であり、DB 単体では防げない(PostgreSQL に「関数の不変性」はない)。0035 も同じ穴を持つ既知の制約で、本 PR に追加要求はない。
- **REVOKE の実効性**: 現構成では `ryza` が所有者ロール兼アプリロール(単一ロール構成)であり、`REVOKE ... FROM PUBLIC` は所有者に効かない no-op である。migration 冒頭コメント L67-71 で明示され、ロール分離後の意図表明として位置付けられている — 意図が明確なので所見にとどめる。
- **DDL 経路**: `ALTER TABLE ledger.evidence ALTER COLUMN payload_ref TYPE ...` などの DDL は行トリガを発火させないが、これも所有者権限を要し、保護領域(migrations)の PR 経由 DDL しか想定していない設計と整合。

以上、いずれも「所有者ロールの権限をアプリで濫用できない前提」の設計に統合されており、本 PR の射程外。

### (3) [情報] 適用範囲分析(b): 未参照行 DELETE 論法の妥当性

裁定 2(「消せるのは仕訳・照合から参照されない行のみ」)の前提を information_schema で検証した(実行した検証 §3)。ledger.evidence を FK 参照する表は `ledger.journal_entries` と `ledger.reconciliations` の 2 つに限られ、他スキーマの `%evidence%` 命名列はいずれも独立(FK なし)。したがって「参照済みの証憑を DELETE で消せる経路は存在しない」は現行スキーマ状態で成立。

将来 evidence を FK 参照する新規表を追加した場合、この論法は自動的に成立し続ける(FK があれば PostgreSQL が拒否する)ため、本裁定はスキーマ拡張に対しても頑健である。**ただし** ON DELETE CASCADE で新規 FK を張った場合は「参照済み evidence を CASCADE で消せる」経路が開き、裁定 2 が崩れる。現状の 2 表(journal_entries / reconciliations)はいずれも CASCADE 指定なし(0005 で `REFERENCES ledger.evidence` のみ = デフォルト NO ACTION)なので現時点で穴なし。将来の設計指針として「evidence への FK は CASCADE 禁止」と明文化する余地はあるが、これは本 PR の射程を超えるので情報所見に留める。

### (4) [情報] migration 品質: 冪等性・番号・流儀

- 冪等性: 実 DB で 2 回目適用成功(検証 §2)。`CREATE OR REPLACE FUNCTION` / `CREATE OR REPLACE TRIGGER` は PG14+ の機能で PG17 では確実に動作。`REVOKE` は複数回実行してもエラーなし。`COMMENT` は上書き。
- 0035 の `ledger.forbid_truncate` 流用の妥当性: 関数本体は `RAISE EXCEPTION '% の TRUNCATE は禁止...' TG_TABLE_NAME` で表名を動的に埋め込むため、evidence に張っても違和感なくメッセージが出る(「ledger.evidence の TRUNCATE は禁止(追記オンリーの仕訳証跡...)」)。**厳密には evidence は「仕訳証跡」ではなく「証憑」なので、TRUNCATE 拒否時のエラーメッセージ末尾「訂正は reversal_of による逆仕訳のみ」は evidence に対しては半分ズレる**(この点は仕様書 §1 で「UPDATE 側は evidence 用の文言を使う、TRUNCATE 側は 0035 流用でメッセージ表面統一」と明示的に選択されている裁定なので、実装は仕様に忠実)。ズレの説明が migration コメント L57-59 にあるので読者は迷わない。**軽微でも指摘ではなく情報止まり**。
- 既存 migration の書き換えなし: git diff で確認、追加のみ。
- 番号衝突なし(0036 は連番)。

### (5) [情報] テスト品質: 10 件の網羅と rollback 隔離

- 網羅性: 仕様書 §テスト1〜5 に対して、実際は 10 件(UPDATE 封鎖 4 パラメータ + 参照済み UPDATE 1 + TRUNCATE 2 経路 + 参照済み DELETE FK 拒否 1 + 未参照 DELETE 許容 1 + トリガ存在確認 1)。UPDATE のパラメータ化(payload_ref / sha256 / kind / source)は改竄検知の主要フィールドをカバー。仕様書に無い「トリガ存在・有効性チェック」(test 10)は 0024/0026/0035 の基準に合わせた追加防御で、`DISABLE TRIGGER USER` によるガード無音化のリグレッション検知として妥当。
- rollback 隔離: `tests/ledger/conftest.py` の `conn` フィクスチャが `finally: c.rollback()` で終わり、commit しない。各テストも `conn.rollback()` を明示的に呼んでいる(L70, 82, 97, 112, 129, 147)。共有 DB との競合を起こさない設計。
- 消極的だが指摘に値する点: `test_evidence_truncate_without_cascade_rejected_by_fk`(test 2)は 0036 の migration が **ない場合でも通る** (PostgreSQL の FK が先に拒否するため)。0036 の増分効果を単独で示すテストは `test_evidence_truncate_cascade_rejected`(test 3)側にある。テストのコメント L86-93 でこの分担が明示されているので混同はしない。両方ある方が裁定の前提(FK が守る + 0036 が守る)の両輪をリグレッションできる。
- match 文字列: `"証憑は不変"`(UPDATE)/ `"TRUNCATE は禁止"`(TRUNCATE)いずれも実際の RAISE EXCEPTION 本文と一致。

### (6) [情報] アプリコード整合: UPDATE/TRUNCATE 経路の不在

grep 結果(実行した検証 §5)より、`src/**` に `UPDATE ledger.evidence` / `DELETE FROM ledger.evidence` / `TRUNCATE ... ledger.evidence` は **一切存在しない**。読み取り経路(SELECT)と INSERT のみ。ingest / ledger / provenance / execution など証憑を扱う 26 ファイル全てで確認。したがって本 migration の本番投入で既存アプリが壊れる懸念はない。

### (7) [中] ops/reminders.yaml の `ledger-evidence-full-append-only` 発火条件は現状では成立不能

登録された条件は `issue_label_open: test-isolation-dedicated-db`。gh 経由で確認したところ:
- ラベル `test-isolation-dedicated-db` はリポジトリに **存在しない**(`gh api repos/klonyapin/ryza/labels` 全件で該当なし)。
- Issue #23(仕様書が言及する「テスト隔離の改善」— タイトル: 「テスト隔離の改善: ingest テストが共有 DB の残留データで壊れる」)は **状態 CLOSED**、ラベルは `impl` / `in-progress` で、`test-isolation-dedicated-db` は付与されていない。

`weekly.py` の条件エバリュエータ(L102-103)は `len(client.list_issues(state="open", labels=[cond["label"]])) > 0` で判定するため、この条件は「該当ラベル付きの OPEN Issue が存在するとき」に True。現状は False であり、将来もその label が付与された新規 Issue が作られなければ発火しない。

**問題**: 仕様書は「Issue #23 が完了した時点で再評価」と述べており、Issue #23 は既に CLOSED なので、意味論的には**このリマインダーは既に発火すべき状態**である。しかし現在の条件では発火せず、将来誰かがラベル付きで再オープンしない限り永遠に pending のまま残る。CLAUDE.md「将来のアクションは必ず ops/reminders.yaml に機械可読で登録」の趣旨(将来アクションを失伝させない)と整合しない。

**選択肢**:
- (a) 条件を `date_after` に変える(例: 3 ヶ月後の日付を設定して定期見直しの起点にする)
- (b) 条件を「Issue #23 が closed であることを検出する型」に変える(現在 T-004 の条件エバリュエータには `issue_closed` 型がないため実装が要る)
- (c) Issue #23 とは別に「証憑完全追記オンリー化の再評価」という新規 Issue を作成し、その番号 N に対して `issue_open: N` 型で追跡する(こちらも新型が要る)
- (d) label をリポジトリに実在させ、意図として「証憑追記オンリー再評価トリガ」として運用開始する(付与された Issue が生まれたら発火する形にする — 現在の設計に最も近い)

**重大度は「中」**: PR の主目的(evidence 不変性ガードの DB レベル導入)を阻害するものではなく、後続のフォローアップ運用の穴。ただし CLAUDE.md の明示ルール(将来アクション制度化)に触れているため放置は避けたい。

現実的な最小手は (d): ラベル `test-isolation-dedicated-db` を実在させ、必要時にそのラベルを付けた Issue を起票することを本 PR の統合後の申し送りにする、あるいは reminders.yaml の記載を残したまま統合時に Issue を 1 本立てる。**この所見だけで request_changes にはしない**(統合承認後にラベル作成と Issue 起票を tracking issue で扱えば足りる)。

### (8) [情報] 仕様書に基づき「反対すべき点を探して見つからなかった」項目

CLAUDE.md「議論規約」に従い、以下について反対可能性を検討したが根拠のある反対点は見つからなかった:

- **裁定「UPDATE と TRUNCATE を封鎖し DELETE は封鎖しない」の妥当性**: 反対案「完全追記オンリー化(DELETE も封鎖)を今すぐ」は tests/conftest.py の `_CLEAR_EVIDENCE_SQL` を必ず巻き添えにする。行トリガは rollback と独立に発火する(BEFORE トリガでも RAISE EXCEPTION は transaction rollback を起こすが、次のテストでの `_CLEAR_EVIDENCE_SQL` が同じトリガに引っかかるため conftest を書き換える必要がある)。conftest 書き換えはテスト隔離戦略の再設計と一体で行うべきで(Issue #23 とセット)、これを本 PR に含めると保護領域 3 つ(migrations / 会計エンジン / conftest 経由の全テスト)にまたがる大改修になる。段階的にする裁定は妥当。
- **`ledger.forbid_truncate` 流用**: 独立関数 `ledger.forbid_evidence_truncate` を新設する反対案はあり得るが、統制表面を一本化する 0035 の設計基準(migration L16-25)と整合しない。関数を分けると監査時に「TRUNCATE 禁止関数がスキーマに 2 つある」状態を説明する必要が生じ、統制の複雑度が増す。流用が優る。
- **REVOKE の順序**: `REVOKE UPDATE, TRUNCATE` を分けて 2 文にする反対案はあり得るが、SQL 上のセマンティクスは同じで、可読性は 1 文の方が「意図的にセットで REVOKE している」ことを表現できる。単一文の選択が優る。

## Verdict の根拠

- 重大: 0 件
- 中: 1 件(所見 7 — reminders.yaml の発火条件が現状成立不能)
- 軽微: 0 件
- 情報: 7 件

所見 7 は PR の統合を止める理由にはならない(証憑不変性ガードという本 PR の目的自体は完全に達成されており、リマインダーの発火条件は後続の統合作業で調整可能な運用パラメータ)。CLAUDE.md 準拠の観点で放置は避けたいが、統合後のフォローアップ Issue で追跡できる範囲。他の観点はすべて仕様と実装が一致し、10 件のテストも実 DB で通り、既存アプリコードへの影響もない。

**verdict: approve**(所見 7 は統合後に対応する申し送り事項として記録することを条件とする)

## 追記(是正確認)

- reviewed_sha を 8596d2b22eefb171ca4d8bf0d58fe4a8de897df5 に更新。
- 差分検証(`git show 8596d2b`): `ops/reminders.yaml` 1 ファイルのみ・+10/-4 行。migration / src / tests への変更なし(保護領域の範囲外)。
- 是正内容: `ledger-evidence-full-append-only` エントリの conditions を `issue_label_open: test-isolation-dedicated-db` → `date_after: 2026-09-01` に差し替え、是正根拠のコメント(所見7の三重の発火不能理由と決定論的な日付固定に切り替える裁定)を YAML コメントで併記。action.body も「Issue #23 完了時点で再評価」から「Issue #23 は CLOSED だが共有 DB 実行時の残留データ隔離は依然 conftest の DELETE 経路に依存する」ため再評価トリガは日付固定という記述に更新。
- 型の実装対応: `src/ryza/ops/weekly.py` L100-101 の `evaluate_condition` が `date_after` を `now.date() >= date.fromisoformat(cond["date"])` で判定。2026-09-01 以降の週次実行で確実に True になる型で、所見 7 (a) の発火不能問題は解消。
- YAML 妥当性: `.venv/bin/python -c "import yaml; ..."` で全体をロード成功、当該エントリの conditions は `[{'type': 'date_after', 'date': '2026-09-01'}]` として正しくパースされる。
- 所見 7 の裁定妥当性: 反対案「新型 `issue_closed` を追加し #23 を追跡」は weekly.py の GitHubClient 拡張(保護領域外だが T-004 系の別 PR 相当)を要し PR 粒度に不適合。裁定の `date_after` は決定論・冪等・実装済み型で最小侵襲。反対点なし。

verdict: **approve** を維持。
