# 第1回フル実装監査(A-12)監査報告書

**監査対象**: ガバナンススキーマ(migrations/0013, 0019, 0021, 0029)・ops/reminders.yaml・承認記録実装(src/ryza/governance/decisions.py)
**監査コード**: A-12
**監査人**: 独立監査人(実装系統と異なるモデル)

---

## 所見 1: [重大] 追記オンリー保護の回避経路(TRUNCATE)が残存しているテーブルが存在する

### 根拠
- `migrations/0013_governance_assets.sql`(L102-L104): `governance.stances` に対して行トリガ(`BEFORE UPDATE OR DELETE`)と `REVOKE UPDATE, DELETE` のみが定義されている。
- `migrations/0021_decision_vetoes.sql`(L106-L117): 上記の問題を指摘し、`governance.decision_vetoes`, `governance.minutes`, `governance.minute_resolutions`, `governance.stances`, `governance.decisions` に対して文トリガ(`BEFORE TRUNCATE`)と `REVOKE TRUNCATE` を追加している。
- しかし、`0013` の時点で作成された `stances` テーブルに対する `TRUNCATE` 制限は `0021` で後から追加された。仮に `0013` 適用後から `0021` 適用までの間に事故が起きていた場合、証跡の喪失リスクがあった。

### 推奨是正
当該期間のデータはデモ段階であり影響は軽微であるが、今後新たに追記オンリーを要求されるテーブル(`governance.decision_vetoes` 等)を作成する際は、**最初の定義(migration)から必ず行・文の両トリガを標準実装とすること**。スキーマ保護の設計基準をドキュメント化し、審査時のチェックリストに明記すること。

---

## 所見 2: [中] REVOKE構文の不備による意図せぬ権限付与(セキュリティホール)

### 根拠
複数のmigrationファイルにおいて、特定ロールから権限を剥奪する意図で `REVOKE` 文が記述されているが、対象ロールの指定が欠落している。
- `migrations/0013_governance_assets.sql` L105-L106:
  ```sql
  REVOKE UPDATE, DELETE ON governance.minutes FROM PUBLIC;
  REVOKE UPDATE, DELETE ON governance.minute_resolutions FROM PUBLIC;
  ```
- `migrations/0021_decision_vetoes.sql` L145-L149, L227-L231:
  ```sql
  REVOKE UPDATE, DELETE ON governance.decision_vetoes FROM PUBLIC;
  REVOKE TRUNCATE ON governance.decision_vetoes FROM PUBLIC;
  ```

PostgreSQLの標準仕様では、すべてのオブジェクトのデフォルト権限は `PUBLIC` に付与されていないため、これらの `REVOKE FROM PUBLIC` は実質無効(no-op)である。結果として「権限を剥奪したつもりで剥奪できていない」状態か、あるいは単なるドキュメント的な意味しか持たない。`ops/reminders.yaml` の `governance-role-separation`(L941 等)で言及されている通り、真のロール分離は未達であるが、DDL上の意図と文法が乖離している点は統制上のノイズとなる。

### 推奨是正
将来的な `governance-role-separation` の実装を見据え、アプリケーション用ロール等の明示的なロール名を指定した `REVOKE` 文に修正すること。

---

## 所見 3: [中] decision_vetoes の run_id 値が application ロジックに依存しており、DBによるリネージ保証が存在しない

### 根拠
- `migrations/0021_decision_vetoes.sql` L89-L94:
  ```sql
  run_id      bigint REFERENCES meta.runs (run_id),
              -- 記録したジョブ実行(meta.runs)。**NULL 可**。0013 の minutes/stances が
              -- run_id を NOT NULL にしているのは、それらが LLM ジョブの産出物であり
              -- (後略)
  ```
- `decision_vetoes` の否認記録は追記オンリーであるが、`run_id` が `NULL` を許容している。これは「代表の直接操作」という設計上の理由からであるが、別経路からの恶意のある `INSERT`(DB直接操作)に対するリネージの保護が存在しないことを意味する。

### 推奨是正
`governance-role-separation`(実弾移行前提条件)において、本テーブルへの `INSERT` 権限を適切に分離されたアプリケーションロールのみに制限すること。トリアージにおいて現在のリスクは受容されているが、実弾移行までに必ず解消すること。

---

## 検査したが所見なしの領域

### ①追記オンリーの強制ロジック(minutes / stances / decisions)
- 検査内容: DB制約(トリガ・REVOKE)による追記オンリーの強制。
- 結果: `governance.forbid_mutation` トリガおよび `forbid_truncate` トリガにより、UPDATE/DELETE/TRUNCATE が DB レベルで確実にブロックされる設計となっている。ロジックの重複定義(`0013`と`0021`)も `CREATE OR REPLACE` により冪等性が保証されている。

### ②decisions/vetoes のスキーマに偽装や事後改変の余地がないか
- 検査内容: CHECK・UNIQUE・FK の制約による偽装経路の検証。
- 結果:
  - 3専決事項(`constitution`, `breaker_resume`, `budget`)への `deemed`(みなし承認)付与を防ぐ `decisions_deemed_not_reserved_check` が存在する。
  - `vetoed_by` や `decided_by` の権限検証がアプリ層(`approvals.is_owner`)で行われている。DB層では `vetoed_by` や `reason` に空白文字列を許容しない CHECK 制約が存在し、証跡性の担保が適切に機能している。FKによる存在検証も適切である。

### ③ops/reminders.yaml の構造と統制の発火期日
- 検査内容: 発火後の `status: fired` への更新処理。
- 結果: `ops/reminders.yaml` はマシンリーダブルな台帳として構築されており、各項目には明確な `conditions`, `action`, `status` が定義されている。発火後はジョブがステータスを自動更新する設計となっており、構造的な欠陥は見られなかった。

### ④reviewed_sha(0029)の記録経路とスキーマ制約
- 検査内容: `reviewed_sha` および `review_ref` の制約。
- 結果: `migrations/0029_decision_reviewed_sha.sql` において、`reviewed_sha` は 40桁hex小文字 のみを許可する CHECK制約、`review_ref` は空白のみを弾く CHECK制約が適切に定義されている。`decisions.py` の writer ロジック(`normalize_reviewed_sha`, `missing_review_ref_warning`)においても SHA の正規化および実在検査の警告出力が適切に実装されており、突合の前提が满足されている。

### ⑤スキーマと実装(decisions.py)の整合
- 検査内容: 0019/0021/0029 のスキーマと `src/ryza/governance/decisions.py` の整合性。
- 結果: `decisions.py` 内の定数(`RESERVED_KINDS`, `VETO_KINDS`, `VETO_ORIGINS`, `VETOABLE_DECISIONS`)がスキーマ制約と完全に一致している。また、DBのトリガエラーによる呼び出し側トランザクションの巻き込み事故を防ぐため、アプリ層での事前検証と `SAVEPOINT` の使用が適切に行われている。ロジック上の乖離は見られなかった。