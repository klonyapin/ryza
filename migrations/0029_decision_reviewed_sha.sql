-- 0029: governance.decisions に審査対象 SHA(reviewed_sha)と審査参照(review_ref)を追加
--
-- 根拠: ops/reminders.yaml `decision-reviewed-sha`(独立役員審査 2026-08-04 重要-3・
--       後続配線審査 後-2)。
--
-- ── 何が足りていなかったか ──────────────────────────────────────────────────
-- Approved トレーラ様式 v2 の `reviewed=<sha40>` は**トレーラの書き手の申告**であり、
-- 「独立審査が実際にその SHA を見た」ことを監査 A-18 は独立に確認できない。現行の機械検査は
-- 「reviewed が当該マージの第2親の祖先であること」までで、`reviewed=<マージ直前のブランチ
-- head>` と書けば被覆は様式 v1 と同じになる(審査 PoC 再現済み)。
-- 同じ穴は CLI 側にもあった: `--deemed-for-pr` / `--kind pr` は `--review <審査の参照>` を
-- 必須にしたが、値は通知本文の文字列として残るだけで**構造化列にはならず**、事後の機械照合が
-- できなかった(後-2)。
--
-- ── 本 migration が入れるもの ────────────────────────────────────────────────
-- 承認記録の側に「この決定が発効した時点で審査対象とされたコミット」と「その審査の参照」を
-- 構造化して持たせる。これにより A-18 は **トレーラの申告(reviewed=)** と **承認記録の申告
-- (reviewed_sha)** という**別経路で書かれた2つの値**を突合できるようになり、片方だけを
-- 書き換えた偽装は不一致として所見に出る。
--
-- ── 残る限界(黙って強い保証に見せない)────────────────────────────────────
-- 両者はどちらも「発効を起票した側」が書く値である。審査エージェント自身の署名は無いため、
-- 起票者が両方に同じ嘘を書けば一致してしまう。本列は「独立審査が実際に見た SHA」への一歩
-- (突合先の新設)であって到達点ではない。到達点は審査エージェントの出力から機械的に
-- 埋まる経路であり、reminders の後続課題として残る。
--
-- ── 追記オンリー原則との関係 ────────────────────────────────────────────────
-- 0021 は governance.decisions を追記オンリーにした(行トリガ decisions_no_mutation)。
-- 本 migration は **列の追加**(DDL)であって行の UPDATE ではないため、その統制に触れない。
-- 既存行は NULL のままで、後から埋め直すこともできない(UPDATE はトリガが拒否する)。
-- したがって本列は「今後の記録に付く」ものであり、過去の決定を遡って審査済みに見せる経路には
-- ならない —— これは意図した性質である。
--
-- 冪等: ADD COLUMN IF NOT EXISTS / 制約は pg_constraint を見て追加 / CREATE OR REPLACE VIEW。

ALTER TABLE governance.decisions
    ADD COLUMN IF NOT EXISTS reviewed_sha text,
    ADD COLUMN IF NOT EXISTS review_ref   text;

-- 40 桁 hex(小文字)の完全 SHA のみ。短縮 SHA を許すと曖昧さが残り、A-18 側の
-- 突合(_FULL_SHA_RE と同じ様式)が「一致とも不一致とも言えない」状態を作る。
-- 大文字を弾くのは、書き手ごとの表記揺れが**不一致の誤検出**になるのを防ぐため
-- (writer 側で lower() に正規化してから渡す)。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'decisions_reviewed_sha_check'
    ) THEN
        ALTER TABLE governance.decisions ADD CONSTRAINT decisions_reviewed_sha_check
            CHECK (reviewed_sha IS NULL OR reviewed_sha ~ '^[0-9a-f]{40}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'decisions_review_ref_check'
    ) THEN
        -- 空白だけの参照は「書いたが中身が無い」= 未記入と区別できないので弾く。
        ALTER TABLE governance.decisions ADD CONSTRAINT decisions_review_ref_check
            CHECK (review_ref IS NULL OR btrim(review_ref) <> '');
    END IF;
END
$$;

-- ════════════════════════════════════════════════════════════════════════════
-- 現決定 view に2列を通す(承認記録を読むコードは view を読む — 0021)
-- ════════════════════════════════════════════════════════════════════════════
-- A-18 の突合が governance.decisions を直読すると、0021 C-5 と同じ形の穴
-- (否認を読み飛ばす経路)がもう1本増える。新列も view 経由で読ませる。
-- CREATE OR REPLACE VIEW は既存列の順序・型を変えられないため、末尾に追加する。
CREATE OR REPLACE VIEW governance.current_decisions AS
SELECT
    d.id                                   AS decision_id,
    d.proposal_ref,
    d.kind,
    d.decision                             AS recorded_decision,
    CASE
        WHEN latest.veto_id IS NULL OR latest.kind = 'withdrawal' THEN d.decision
        ELSE 'vetoed'
    END                                    AS effective_decision,
    (latest.veto_id IS NOT NULL AND latest.kind <> 'withdrawal') AS is_vetoed,
    d.decided_by,
    d.note,
    d.channel_msg_id,
    d.decided_at,
    latest.veto_id,
    latest.kind                            AS veto_kind,
    latest.vetoed_by,
    latest.reason                          AS veto_reason,
    latest.vetoed_at,
    resolved.revert_commit,
    resolved.derived_effects_ref,
    d.reviewed_sha,
    d.review_ref
FROM governance.decisions d
LEFT JOIN LATERAL (
    SELECT vv.veto_id, vv.kind, vv.vetoed_by, vv.reason, vv.vetoed_at
    FROM governance.decision_vetoes vv
    WHERE vv.decision_id = d.id
    ORDER BY vv.veto_id DESC
    LIMIT 1
) latest ON true
LEFT JOIN LATERAL (
    SELECT
        (SELECT vc.revert_commit
         FROM governance.decision_vetoes vc
         WHERE vc.decision_id = d.id AND vc.revert_commit IS NOT NULL
           AND vc.veto_id > w.since
         ORDER BY vc.veto_id DESC LIMIT 1) AS revert_commit,
        (SELECT vd.derived_effects_ref
         FROM governance.decision_vetoes vd
         WHERE vd.decision_id = d.id AND vd.derived_effects_ref IS NOT NULL
           AND vd.veto_id > w.since
         ORDER BY vd.veto_id DESC LIMIT 1) AS derived_effects_ref
    FROM (
        SELECT coalesce(max(vw.veto_id), 0) AS since
        FROM governance.decision_vetoes vw
        WHERE vw.decision_id = d.id AND vw.kind = 'withdrawal'
    ) w
) resolved ON true;

-- ════════════════════════════════════════════════════════════════════════════
-- データカタログ用コメント
-- ════════════════════════════════════════════════════════════════════════════
COMMENT ON COLUMN governance.decisions.reviewed_sha IS
    'この決定が発効した時点で審査対象とされたコミット(40 桁 hex・小文字)。'
    'Approved トレーラ様式 v2 の reviewed=<sha40> と**別経路で書かれた同じ主張**であり、'
    '監査 A-18-8 が両方ある決定について一致を検査する(不一致は所見)。'
    '既存行と、審査対象を申告せずに記録された決定は NULL(追記オンリーのため後から埋められない)。'
    '**限界**: 値の書き手は発効を起票した側であり、審査エージェント自身の署名ではない。';
COMMENT ON COLUMN governance.decisions.review_ref IS
    '独立役員審査の参照(docs/reviews/... のパス・URL 等)。CLI --review の値をそのまま構造化して'
    '持つ。リポジトリ内パス形式なら writer が実在を検査するが、**不在でも拒否しない**'
    '(過去の審査を遡って登録する経路を塞がないため。警告のみ)。';
