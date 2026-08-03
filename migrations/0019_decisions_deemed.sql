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
-- データカタログ用コメント(0007 の記述を更新)
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON COLUMN governance.decisions.decision IS
    'approve|reject|question|deemed。approve=代表の明示承認(定款第3条の3専決事項)、'
    'deemed=みなし承認(#承認 への通知と同時に発効・事後否認可 — 定款 v0.4 第3条)。'
    '両者を区別することで監査部門が deemed_ratio(形骸化アラート)を計算できる。';
