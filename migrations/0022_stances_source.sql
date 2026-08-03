-- 0022: governance.stances に出所種別 source を追加(盲検レビューの経路分離)
--
-- 根拠: 議論規約3(CLAUDE.md・docs/design/05-governance.md §6-2)
--   「重要な評価(戦略昇格・IPS 改訂案)では、独立役員はユーザーや起草者の選好を
--     知らされずに評価する(プロンプト分離)」
-- 独立役員審査: docs/reviews/boardroom-meeting-independent-review.md C-3 の後続是正。
--
-- ── 何が漏れていたか ────────────────────────────────────────────────────────
-- 役員室が会議形式になった(2026-08-03)ことで、stances には「会議で代表・他役職の
-- 発言を聞いた文脈で形成された主張」が入るようになった。着任プロンプト
-- (src/ryza/governance/personas.assume_role)は role 単位で直近 N 件を無条件に
-- 読み込むため、盲検レビューで独立役員が着任すると、**自分の過去の stance という
-- 形をとった代表の選好**がそのまま盲検経路へ透過する。role 分離(05 §6-2)は
-- 他役職の記憶の混入を防ぐが、自分の記憶に混ざった他者の影響は防げない。
--
-- boardroom.role_digest_input の決定論フィルタは「他役職の発言を要約入力にしない」
-- ところまでは担保するが、代表の発言は当該 role の文脈として残す設計であり
-- (会議の議事は代表の指示に応答する形で進むため除けない)、会議由来という事実を
-- 行に刻んでおかない限り、後から選り分ける手段が無い。
--
-- ── 語彙(実装済みの書き手と 0013 minutes.meeting の語彙に合わせる)──────────
--   'direct'      … 個別レビュー・単独記録。他役職や代表の発言を聞いていない文脈で
--                   形成された stance(personas.record_stance の既定)
--   'office_chat' … 役員室チャット(会議形式)。0013 minutes.meeting='office_chat' に
--                   対応。boardroom.record_chat_stances が書く
--   'committee'   … 正式会議体(investment_committee / management_meeting /
--                   extraordinary_committee / effectiveness_review)。月次委員会ジョブ
--                   (personas.py docstring 記載の予定実装)の書込先を先に確保する
--
-- 'committee' を書き手より先に列挙するのは、**後から足す側が既定の 'direct' で
-- 済ませてしまう**のを防ぐためである(0019 が未登録 kind='constitution' を禁止側に
-- 先回り列挙したのと同じ動機 — 遅れて開く穴を作らない)。盲検除外集合にも同時に
-- 入れておくので、書き手が現れた時点で自動的に正しく扱われる。
--
-- ── 既存行の扱い ────────────────────────────────────────────────────────────
-- 既存行は DEFAULT 'direct' のまま据え置く。理由は2つ:
--   1. 本 migration 適用時点で governance.stances は本番 DB・テスト DB とも 0 行で
--      あり、誤ラベルが生じる行が存在しない(2026-08-03 実測)
--   2. stances は追記オンリー(0013 の UPDATE 禁止トリガ)であり、由来別バックフィルは
--      トリガの一時無効化を要する。0 行のためにその例外を作るのは割に合わない
-- ADD COLUMN ... DEFAULT は PostgreSQL 11 以降テーブルを書き換えず、行トリガも
-- 発火しないため、追記オンリー制約に触れずに列を足せる。
--
-- 冪等: ADD COLUMN IF NOT EXISTS。CHECK は 0012/0019 と同じく明示名で追加する。

ALTER TABLE governance.stances
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'direct';

ALTER TABLE governance.stances DROP CONSTRAINT IF EXISTS stances_source_check;
ALTER TABLE governance.stances ADD CONSTRAINT stances_source_check
    CHECK (source IN ('direct', 'office_chat', 'committee'));

-- 盲検着任は「role が一致し、かつ出所が会議でない」行だけを読む。role_idx だけでは
-- source の絞り込みがヒープ参照になるため、複合インデックスを張る。
CREATE INDEX IF NOT EXISTS stances_role_source_idx
    ON governance.stances (role, source, stated_at DESC);

COMMENT ON COLUMN governance.stances.source IS
    'direct(個別レビュー・単独記録。既定)|office_chat(役員室チャット)|committee(正式会議体)。'
    '会議由来(office_chat・committee)は代表・他役職の発言を聞いた文脈で形成されるため、'
    '盲検レビューの着任(personas.assume_role(blind=True)— 議論規約3)では読み込まない。';

COMMENT ON TABLE governance.stances IS
    '役職ごとの主張・懸念の要約(追記オンリー)。着任時に直近 N 件を読み込む(05 §2 永続記憶)。'
    '正本は minutes。訂正は retraction 行の追記で表現。'
    'source が会議由来の行は盲検着任から除外される(0022)。';
