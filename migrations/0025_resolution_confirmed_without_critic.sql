-- 0025: governance.minute_resolutions に confirmed_without_critic を追加
--       (独立役員の批判を経ずに通した決議の永続証跡・三値)
--
-- 根拠: docs/design/05-governance.md §3(批判義務)・§4(決議のみが発効する)、
--       docs/reviews/boardroom-meeting-independent-review.md 再確認記録「新規懸念A」と
--       決議精緻化審査(2026-08-03)懸念1・懸念5、ops/reminders.yaml:
--       resolution-critic-recency。
--       §6-5(形骸化の防止)が明文で挙げるのは「懸念ゼロ回答の連続」「付議なし期間」で
--       あり本指標そのものではない。本列と監査はその**趣旨に連なる新設統制**である
--       (条文に無い統制を条文の根拠として引かない — A-18 の条文⇔実装突合が狂う)。
--
-- ── 何が漏れていたか ────────────────────────────────────────────────────────
-- 決議の決定論チェック(src/ryza/governance/boardroom.mark_resolution)は、批判を
-- 経ていない決議を検出して代表に明示確認を求める(``confirmed_without_critic=True``)。
-- しかし確認して通した事実は**どこにも残っていなかった**。摩擦は1回ごとには効くが、
-- 「毎回チェックを外す」運用に退化しても DB からは検出できず、形骸化の監査が成立しない。
-- 決議は発効する決定そのものであり(§4)、その決議が批判を経たかどうかは決議自体と
-- 同じ強さの証跡である。
--
-- ── 意味論(三値。NULL を「判定不能」に使う)─────────────────────────────────
--   false … 通常経路。決議時点の議事録で「最後の代表発言より後に独立役員が発言して
--            いる」ことをコードが確認して通した決議(既定)
--   true  … 鮮度が**無いと分かった上で**代表が明示確認して通した決議。決議権は代表に
--            残る(定款第3条)ため禁止ではないが、連続・累積は形骸化アラートの対象
--   NULL  … **判定不能**。議事録本文が会議形式(transcript_markdown)でなく話者列を
--            復元できないため鮮度を検証できないまま、明示確認で通した決議
--
-- NULL を独立の値に立てるのは、判定不能を false(=鮮度確認済み)へ丸めると
-- **fail-open** になるためである(決議精緻化審査 懸念1 が実測: 自由記述の議事録+
-- 出席者に独立役員、で摩擦ゼロの決議が成立していた)。出席者配列は「その場に居た」
-- ことしか意味せず、最新の代表発言が批判に晒された証拠にならない。0013 の UPDATE 禁止に
-- より後からの訂正は不能なので、区別は書き込み時点で付けるしかない。
--
-- 「批判の鮮度」を採るのは、会議冒頭で独立役員が無関係な話題に1度発言していれば以後の
-- 決議が無批判で通る経路(再確認審査 懸念A の実証)を塞ぐため。
--
-- ── 既存行の扱い ────────────────────────────────────────────────────────────
-- 既存行は DEFAULT false で据え置く。本 migration 適用時点の本番 DB の
-- minute_resolutions は 0 行であり(2026-08-03 実測)、遡及ラベルを要する行が無い。
--
-- ── 0013 の追記オンリー方針との両立 ────────────────────────────────────────
-- minute_resolutions は UPDATE/DELETE 禁止トリガ(0013 governance.forbid_mutation)を
-- 持つが、本変更は DDL であり行トリガを発火させない。PostgreSQL 11 以降の
-- ``ADD COLUMN ... DEFAULT`` はテーブルを書き換えない(既存行の物理 UPDATE が起きない)。
-- 0022(stances.source)と同じ手法で、追記オンリー制約に触れずに列を足す。
--
-- ── 索引を張らない理由(決議精緻化審査 懸念5)──────────────────────────────
-- 形骸化の監査(boardroom.resolution_confirmation_stats)は「直近 N 件」を
-- ``ORDER BY resolution_id DESC LIMIT N`` で読み、``WHERE`` を持たない。したがって
-- 部分索引は原理的に使われない(審査の実測 EXPLAIN は Seq Scan + Sort)。主キーの逆順
-- 走査で足り、決議は人手でマークする低頻度データである。**未検証の性能主張を保護領域の
-- migration に残さない**ため、索引は張らない。
--
-- 冪等: ADD COLUMN IF NOT EXISTS(NOT NULL 制約は付けない — NULL が意味を持つ)。

ALTER TABLE governance.minute_resolutions
    ADD COLUMN IF NOT EXISTS confirmed_without_critic boolean DEFAULT false;

COMMENT ON COLUMN governance.minute_resolutions.confirmed_without_critic IS
    '批判の鮮度(=最後の代表発言より後の独立役員の発言)の検証結果。'
    'false=鮮度を確認して通した(既定)/ true=鮮度が無いと分かった上で代表が明示確認して'
    '通した / NULL=議事録本文が会議形式でなく鮮度を判定できないまま明示確認して通した。'
    'true と NULL はいずれも「批判を経ていない決議」として監査の対象(連続・累積)。'
    '決議権は代表に残る(定款第3条)ため禁止ではない。書き手は '
    'src/ryza/governance/boardroom.mark_resolution。';

COMMENT ON TABLE governance.minute_resolutions IS
    '決議マーク。議事録中で明示的に決議とされた項目のみが発効する(雑談が政策にならない境界)。'
    '批判を経ずに通した決議は confirmed_without_critic が true(鮮度なし)または NULL'
    '(判定不能)で残る(0025)。';
