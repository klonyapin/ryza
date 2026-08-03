# 独立役員意見書 — 0019_decisions_deemed(c5c361b)

- 日付: 2026-08-03 / 対象: migrations/0019_decisions_deemed.sql + tests/governance/test_governance_schema.py(2 files, +88)
- 審査者: 独立役員(非執行・批判専任。起草者の選好は不知)
- 根拠: 定款 v0.4 第3条・第5条、config/governance.yaml(deemed_approval・protected_areas)、migrations/0007・0012、src/ryza/db/migrate.py、src/ryza/bot/approvals.py、src/ryza/audit/a13.py
- 検証: トランザクション性・制約名・既存行互換・CI での DB テスト実行を確認(いずれも問題なし。IF EXISTS 不使用は正しい)

## 判定: 条件付き承認

- **C-2(重大・マージ前必須)**: `kind` と `decision` が独立で `(kind='budget'|'breaker_resume', decision='deemed')` を阻む統制がスキーマにもコードにも無い。3専決(定款第3条)の承認証跡を偽装できる。是正: `CHECK (decision <> 'deemed' OR kind NOT IN ('breaker_resume','budget'))` + 定款改廃分の kind 追加か不変条件テスト、およびテスト。
- **C-1(重大・後続 PR 条件)**: 0007 の `UNIQUE(proposal_ref)` により deemed 発効後の事後否認が記録できない(起草者指摘に同意)。結果として deemed_ratio 形骸化アラートが構造的に発火不能、UPDATE 回避策は `Approved:` トレーラの意味を遡及改変する。是正: `governance.decision_vetoes` 追記テーブル(revert_commit・派生効果参照を含む)。**`'deemed'` の writer 実装より前**にマージすること。
- **C-3〜C-7(中〜低)**: writer 不在で第3条の記録要件は未充足(後続タスク登録が条件) / テストの `decided_by='representative'` は deemed の定義と矛盾(`'system:deemed'` へ) / COMMENT が記録不能な「事後否認可」を主張 / 本コミットに `Approved:` トレーラ無し(migrations は保護領域) / `decisions.kind` のカタログコメントが 0012 以来 stale。

## 設計リード裁定(2026-08-03 追記)

- C-2: CHECK は `decision <> 'deemed' OR kind NOT IN ('breaker_resume','budget','constitution')` とする
  (kind='constitution' は現語彙に無いが、将来追加時に穴が開かないよう先回りで列挙。無害)。
  加えて3専決ルールの不変条件テストを tests/governance に追加。
- C-4: 採用 — テストは `decided_by='system:deemed'`、CHECK `(decision <> 'deemed' OR decided_by LIKE 'system:%')` を追加。
- C-5・C-7: COMMENT を実態に合わせ修正(事後否認の記録は decision_vetoes 実装まで不可の旨を明記)。
- C-6: Approved トレーラはマージコミットに付与(既存 PR 運用と同一)。
- C-1・C-3: ops/reminders.yaml に登録。decision_vetoes スキーマは deemed writer 実装 PR より前にマージする
  順序制約を リマインダー本文に明記。
