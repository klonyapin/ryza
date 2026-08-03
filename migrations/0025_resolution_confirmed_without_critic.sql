-- 0025: governance.minute_resolutions に confirmed_without_critic を追加
--       (独立役員の批判を経ずに通した決議の永続証跡)
--
-- 根拠: docs/design/05-governance.md §3(批判義務)・§6-5(形骸化の防止)、
--       docs/reviews/boardroom-meeting-independent-review.md 再確認記録「新規懸念A」、
--       ops/reminders.yaml: resolution-critic-recency。
--
-- ── 何が漏れていたか ────────────────────────────────────────────────────────
-- 決議の決定論チェック(src/ryza/governance/boardroom.mark_resolution)は、批判を
-- 経ていない決議を検出して代表に明示確認を求める(``confirmed_without_critic=True``)。
-- しかし確認して通した事実は**どこにも残っていなかった**。摩擦は1回ごとには効くが、
-- 「毎回チェックを外す」運用に退化しても DB からは検出できず、§6-5 が求める形骸化の
-- 監査(懸念ゼロ回答の連続と同型の指標)が成立しない。決議は発効する決定そのもので
-- あり(§4)、その決議が批判を経たかどうかは決議自体と同じ強さの証跡である。
--
-- ── 意味論 ──────────────────────────────────────────────────────────────────
--   false … 通常経路。決議時点の議事録で「最後の代表発言より後に独立役員が発言して
--            いる」ことをコードが確認して通した決議(既定)
--   true  … 代表が明示確認して通した決議。決議時点で独立役員の批判が最新の代表発言に
--            及んでいなかった(批判の**鮮度**が無い)。決議権は代表に残る(定款第3条)
--            ため禁止ではないが、連続すると §6-5 の形骸化アラートの対象になる
--
-- 「批判の鮮度」を採るのは、会議冒頭で独立役員が無関係な話題に1度発言していれば以後の
-- 決議が無批判で通る経路(再確認審査 懸念A の実証)を塞ぐため。判定は議事録本文
-- (0013 minutes.body_md)の話者行から決定論的に復元する。
--
-- ── 既存行の扱い ────────────────────────────────────────────────────────────
-- 既存行は DEFAULT false で据え置く。本 migration 適用時点の本番 DB の
-- minute_resolutions は 0 行であり(2026-08-03 実測)、遡及ラベルを要する行が無い。
-- 仮に既存行があっても false(=通常経路)は「アラートを鳴らさない側」ではなく
-- 「監査対象に数えない側」であり、確認付き決議を false と誤記する危険は
-- 適用時点の 0 行によって排除される。
--
-- ── 0013 の追記オンリー方針との両立 ────────────────────────────────────────
-- minute_resolutions は UPDATE/DELETE 禁止トリガ(0013 governance.forbid_mutation)を
-- 持つが、本変更は DDL であり行トリガを発火させない。PostgreSQL 11 以降の
-- ``ADD COLUMN ... DEFAULT`` はテーブルを書き換えない(既存行の物理 UPDATE が起きない)。
-- 0022(stances.source)と同じ手法で、追記オンリー制約に触れずに列を足す。
--
-- 冪等: ADD COLUMN IF NOT EXISTS。

ALTER TABLE governance.minute_resolutions
    ADD COLUMN IF NOT EXISTS confirmed_without_critic boolean NOT NULL DEFAULT false;

-- 形骸化の監査(§6-5)は「直近 N 件のうち何件が確認付きか」「新しい順に何件連続で
-- 確認付きか」を読む。resolution_id の降順スキャンに条件が乗るため部分索引を張る。
CREATE INDEX IF NOT EXISTS minute_resolutions_confirmed_idx
    ON governance.minute_resolutions (resolution_id DESC)
    WHERE confirmed_without_critic;

COMMENT ON COLUMN governance.minute_resolutions.confirmed_without_critic IS
    '独立役員の批判(鮮度あり=最後の代表発言より後の発言)を経ずに、代表の明示確認で '
    '通した決議なら true。既定 false。決議権は代表に残る(定款第3条)ため禁止ではないが、'
    '連続は形骸化アラートの対象(05-governance §6-5)。書き手は '
    'src/ryza/governance/boardroom.mark_resolution。';

COMMENT ON TABLE governance.minute_resolutions IS
    '決議マーク。議事録中で明示的に決議とされた項目のみが発効する(雑談が政策にならない境界)。'
    '批判を経ずに通した決議は confirmed_without_critic=true で残る(0025)。';
