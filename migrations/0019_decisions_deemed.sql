-- 0019: governance.decisions.decision に 'deemed'(みなし承認)を追加
--
-- 根拠: 定款 v0.4 第3条(docs/design/06-constitution.md L31)
--   「みなし承認も `governance.decisions` に『deemed』として記録し、監査対象と
--     する(明示承認と区別する)」
-- 機械可読版は config/governance.yaml の deemed_approval(version 0.4):
--   「governance.decisions に decision='deemed' で記録し、明示承認と区別する」
--
-- 0007 の CHECK は decision IN ('approve','reject','question') で 'deemed' を
-- 許容しないため、定款どおりの記録が INSERT できない(定款と実装の乖離)。
-- 監査 A-13(無承認変更検出)は承認トレーラ `Approved:` が指す decisions 行と
-- 突合するため、みなし承認が記録できないことは統制の穴になる。
--
-- 'approve'(代表の明示承認 — 第3条の3専決事項)と 'deemed'(通知即発効の
-- みなし承認)を別の語彙に保つのは意図的である。監査部門が追う deemed_ratio
-- (形骸化アラート)は両者を区別できて初めて計算できる。
--
-- 既存行への影響: 語彙の拡大のみ(既存の3値は全て新 CHECK でも真)。
-- 0012 が kind に対して行った拡大と同じ流儀。
--
-- 旧: CHECK (decision IN ('approve', 'reject', 'question'))
-- 新: CHECK (decision IN ('approve', 'reject', 'question', 'deemed'))

ALTER TABLE governance.decisions DROP CONSTRAINT decisions_decision_check;
ALTER TABLE governance.decisions ADD CONSTRAINT decisions_decision_check
    CHECK (decision IN ('approve', 'reject', 'question', 'deemed'));

-- ────────────────────────────────────────────────────────────────────────────
-- 3専決事項への 'deemed' 適用を禁止(独立役員審査 C-2・設計リード裁定 2026-08-03)
-- ────────────────────────────────────────────────────────────────────────────
-- 定款第3条の representative_reserved(config/governance.yaml)— 定款改正・実弾
-- マネー・Kill Switch 復帰 — は代表の明示承認が必須であり、みなし承認では発効
-- しない。0007 の kind と decision は互いに独立なため、この制約が無いと
-- (kind='budget', decision='deemed') のような行が書けてしまい、3専決事項の
-- 承認証跡を偽装できる(A-13 の `Approved:` 突合は decisions 行を真とする)。
--
-- kind の対応: live_money → 'budget'、kill_switch_resume → 'breaker_resume'、
-- constitution_amendment → 'constitution'(現 kind 語彙には未登録。将来追加時に
-- 穴が開かないよう先回りで列挙する。未登録の値を列挙しても既存行には無害)。
ALTER TABLE governance.decisions ADD CONSTRAINT decisions_deemed_not_reserved_check
    CHECK (decision <> 'deemed'
           OR kind NOT IN ('breaker_resume', 'budget', 'constitution'));

-- ────────────────────────────────────────────────────────────────────────────
-- 'deemed' の decided_by はシステム主体に限る(独立役員審査 C-4)
-- ────────────────────────────────────────────────────────────────────────────
-- みなし承認は「代表が押した」記録ではなく「通知により自動発効した」記録である。
-- decided_by に代表の Discord ユーザー ID を書くと、明示承認との区別が
-- decision 列だけに依存し、証跡としては代表の作為に見えてしまう。
-- 0012 の killswitch_events.actor が使う 'system:<source>' 表記に合わせる。
ALTER TABLE governance.decisions ADD CONSTRAINT decisions_deemed_system_actor_check
    CHECK (decision <> 'deemed' OR decided_by LIKE 'system:%');

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント(0007・0012 の記述を実態に合わせて更新)
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON COLUMN governance.decisions.decision IS
    'approve|reject|question|deemed。approve=代表の明示承認(定款第3条の3専決事項)、'
    'deemed=みなし承認(#承認 への通知と同時に発効 — 定款 v0.4 第3条。decided_by は '
    'system:<source>、3専決の kind には付けられない)。両者を区別することで監査部門が '
    'deemed_ratio(形骸化アラート)を計算できる。'
    '注意: 定款が定める事後否認は本テーブルには記録できない — proposal_ref の UNIQUE で '
    '1提案=1行に固定されており、追記オンリーの否認記録テーブル '
    '(governance.decision_vetoes・未実装)の導入までは否認の証跡を残す場所が無い。';

-- 0012 で 'frozen_exception_trade' を追加した際にカタログコメントが更新されず
-- stale になっていた(独立役員審査 C-7)。
COMMENT ON COLUMN governance.decisions.kind IS
    'pr|strategy_promotion|breaker_resume|budget|frozen_exception_trade|other。'
    'breaker_resume・budget は定款第3条の3専決事項に対応し、decision=''deemed'' を'
    '付けられない(decisions_deemed_not_reserved_check)。';
