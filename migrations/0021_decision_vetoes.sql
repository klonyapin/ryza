-- 0021: governance.decision_vetoes(承認決定の事後否認・追記オンリー)
--       + governance.current_decisions(否認を反映した「現決定」view)
--       + governance.decisions 自身の追記オンリー化
--
-- 根拠: 定款 v0.4 第3条(docs/design/06-constitution.md L31 前後)
--   「通知と同時に発効する。代表は**いつでも否認でき**、否認された変更は遅滞なく
--     取り消す(git revert・設定巻き戻し)。否認までの間に生じた派生効果(その規則の
--     下で実行された取引・生成物)は取消不能な場合があるため、執行側は否認受領後
--     すみやかに派生効果の一覧を `#運営` へ報告する」
-- 機械可読版: config/governance.yaml の deemed_approval.veto = anytime。
-- 独立役員審査: docs/reviews/0019-decisions-deemed-independent-review.md C-1、
--               docs/reviews/0021-decision-vetoes-independent-review.md C-1〜C-11。
--
-- ── なぜ別テーブルなのか(0019 C-1 の是正が UPDATE では成立しない理由)──────
-- governance.decisions は 0007 の UNIQUE(proposal_ref) で 1提案=1行に固定されている。
-- そのため否認を「新しい decisions 行」として INSERT できない。一方、既存行を UPDATE
-- すると、保護領域コミットの `Approved: <decisions の ID>` トレーラ(定款第5条・
-- 06-constitution.md L61)が指す承認記録の意味が**遡及的に**書き換わり、監査 A-18-1 の
-- 突合が過去に遡って壊れる(「承認済み」と記録されていた事実そのものが消える)。
-- 否認は承認の取り消しではなく**承認の後に起きた別の事実**であるため、追記オンリーの
-- 別テーブルに記録し、両者の合成(= 現在の効力)は view で導出するのが正しい形になる。
--
-- 結果として、監査部門が追う deemed_ratio(否認ゼロの長期継続 = 形骸化アラート —
-- config/governance.yaml deemed_approval.audit_metric)が初めて計算可能になる。
-- 否認を記録する場所が無い間、このアラートは構造的に発火不能だった。
--
-- 冪等: CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE(0013 の流儀)。

CREATE SCHEMA IF NOT EXISTS governance;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. governance.decisions を追記オンリーにする(独立役員審査 0021 C-1)
-- ════════════════════════════════════════════════════════════════════════════
-- 本 migration の別表化の根拠は「decisions を UPDATE すると Approved トレーラの意味が
-- 遡及改変される」ことにある。しかしその UPDATE 自体は 0007 以来封鎖されておらず、
-- **派生記録(否認)が不変で原本(承認)が可変**という保護の逆転が生じていた。
-- 原本を守らずに派生だけ守っても、承認記録の証跡性は原本側の可変性で決まる。
--
-- 安全性: 現行コードの decisions への書込は INSERT のみ(bot/approvals.record_decision と
-- governance/decisions.record_deemed_approval の2箇所。審査が grep で確認済み)。
-- 決定の訂正は「否認の追記」(decision_vetoes)で表現するのが定款第3条の方式であり、
-- 行の書き換えを要する運用は存在しない。
CREATE OR REPLACE FUNCTION governance.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% は % では禁止(追記オンリー)。承認記録・議事録・決議は証憑(定款第3条・05-governance §4)。訂正は追記で行う',
        TG_OP, TG_TABLE_NAME;
END;
$$;

CREATE OR REPLACE TRIGGER decisions_no_mutation
    BEFORE UPDATE OR DELETE ON governance.decisions
    FOR EACH ROW EXECUTE FUNCTION governance.forbid_mutation();

REVOKE UPDATE, DELETE ON governance.decisions FROM PUBLIC;

-- ════════════════════════════════════════════════════════════════════════════
-- 2. governance.decision_vetoes(事後否認・追記オンリー)
-- ════════════════════════════════════════════════════════════════════════════
-- ── 行種別 kind を持つ理由(独立役員審査 C-3)────────────────────────────────
-- 否認は人手(代表)の操作であり、誤った decision_id への1行で無関係な承認が
-- 恒久的に「否認済み」に汚染されうる。0007 の UNIQUE(proposal_ref) により提案の
-- 再記録もできないため、撤回の表現が無いと復旧手段が存在しない。追記オンリーを
-- 保ったまま復旧するには、行に種別を持たせて「撤回」を追記できる必要がある:
--   'veto'           … 否認(発効中の決定を止める)
--   'revert_complete'… 否認に伴う取消の完了報告(revert_commit・派生効果一覧)
--   'withdrawal'     … 否認そのものの撤回(誤操作の是正。current_decisions は
--                      これを最新行に持つ決定を「否認されていない」として返す)
--
-- ── 1決定に複数行を許す理由 ──────────────────────────────────────────────
-- 追記オンリーのため、否認時点では未確定の情報(取消コミット・取消不能な派生効果の
-- 一覧)を後から UPDATE で埋められない。同一 decision_id への追記で表現する。
-- UNIQUE(decision_id) を張るとこの表現ができなくなるので張らない。
CREATE TABLE IF NOT EXISTS governance.decision_vetoes (
    veto_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id bigint NOT NULL REFERENCES governance.decisions (id),
                -- 否認対象の承認記録(0007 の PK は `id`)。存在しない決定は FK が拒否する
    kind        text NOT NULL DEFAULT 'veto'
                CHECK (kind IN ('veto', 'revert_complete', 'withdrawal')),
    vetoed_by   text NOT NULL CHECK (btrim(vetoed_by) <> ''),
                -- 否認者。decisions.decided_by と同じ表記(オーナー検証済みの Discord
                -- ユーザー ID)。DB 側でオーナー ID を強制できないのは 0007 と同じ制約
                -- (オーナー ID は config/secrets 側にあり、検証はアプリ層 —
                --  src/ryza/governance/decisions.py が approvals.is_owner で行う)
    reason      text NOT NULL CHECK (btrim(reason) <> ''),
                -- 理由。定款第3条は否認に取消義務を課すため、理由の無い否認は
                -- 執行側が何を巻き戻すべきか判断できない。撤回行も理由を必須にする
                -- (「なぜ否認を取り消したか」が残らないと誤操作と方針変更を区別できない)
    revert_commit      text,
                -- 取消(git revert・設定巻き戻し)のコミット SHA。否認時点では未確定の
                -- ことがあるため NULL 可。確定後は kind='revert_complete' の追記で記録する
    derived_effects_ref text,
                -- 取消不能な派生効果の一覧の参照(#運営 への報告メッセージ ID・
                -- レポート URL 等 — 定款第3条の報告義務)。派生効果が無ければ NULL
    run_id      bigint REFERENCES meta.runs (run_id),
                -- 記録したジョブ実行(meta.runs)。**NULL 可**。0013 の minutes/stances が
                -- run_id を NOT NULL にしているのは、それらが LLM ジョブの産出物であり
                -- 「どの実行が生成したか」がリネージとして必須だからである(不変原則3)。
                -- 否認は代表の作為(意思表示)であってジョブの生成物ではないため、
                -- 生成元 Run という概念が本来的に無い(誰が否認したかは vetoed_by が持つ)。
                -- 記録経路がジョブ内にある場合に限り任意で埋める。否認の**出所**
                -- (Discord ボタン / CLI / ジョブ)を明示する origin 列の導入は
                -- ops/reminders.yaml の veto-origin-column で継続検討する
    vetoed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS decision_vetoes_decision_idx
    ON governance.decision_vetoes (decision_id, veto_id DESC);

-- ── 否認できる決定を approve / deemed に限る(独立役員審査 C-2)──────────────
-- 当初は「否認は効力を弱める方向にしか働かないので全 decision に一般化して損がない」と
-- したが、これは reject / question に対して**偽**である。却下(reject)に否認を1行
-- 付けると current_decisions は effective_decision='vetoed' を返し、「却下されている」
-- という阻止の根拠が消える。将来この view を読んで発効を止める判定は fail-open で
-- 外れる(却下を否認して通す、という抜け道になる)。
-- したがって否認対象は「発効している決定」= approve(明示承認)と deemed(みなし承認)に
-- 限定する。CHECK では他表を参照できないため BEFORE INSERT トリガで強制する。
CREATE OR REPLACE FUNCTION governance.check_veto_target() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_decision text;
BEGIN
    SELECT d.decision INTO target_decision
    FROM governance.decisions d WHERE d.id = NEW.decision_id;
    IF target_decision IS NULL THEN
        RETURN NEW;  -- 存在しない決定は FK が拒否する(ここでは判定しない)
    END IF;
    IF target_decision NOT IN ('approve', 'deemed') THEN
        RAISE EXCEPTION
            '決定 id=% は decision=''%'' であり否認できない(否認できるのは発効している決定 approve / deemed のみ — 定款第3条)。却下・質問を否認可能にすると「却下されている」という阻止の根拠が消える',
            NEW.decision_id, target_decision;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER decision_vetoes_target_check
    BEFORE INSERT ON governance.decision_vetoes
    FOR EACH ROW EXECUTE FUNCTION governance.check_veto_target();

-- ── 追記オンリーの強制(0013 の minutes/stances と同型)──────────────────────
CREATE OR REPLACE TRIGGER decision_vetoes_no_mutation
    BEFORE UPDATE OR DELETE ON governance.decision_vetoes
    FOR EACH ROW EXECUTE FUNCTION governance.forbid_mutation();

REVOKE UPDATE, DELETE ON governance.decision_vetoes FROM PUBLIC;

-- **TRUNCATE は行トリガを迂回する**(0015 の独立役員審査で実証・0018 が標準化)。
-- 行トリガだけでは `TRUNCATE governance.decision_vetoes` の一撃で否認証跡が全て消え、
-- 「否認ゼロ」という監査上もっとも都合のよい状態を作れてしまう。文トリガ+REVOKE で塞ぐ。
-- なお REVOKE は所有者ロール(現構成ではアプリと同一の ryza)には効かないため、実効的な
-- 統制はトリガ側である。ロール分離は ops/reminders.yaml の governance-role-separation
-- (実弾移行前提条件)で扱う。
CREATE OR REPLACE FUNCTION governance.forbid_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% の TRUNCATE は禁止(追記オンリーの承認・否認証跡)。訂正は追記で行う',
        TG_TABLE_NAME;
END;
$$;

CREATE OR REPLACE TRIGGER decision_vetoes_no_truncate
    BEFORE TRUNCATE ON governance.decision_vetoes
    FOR EACH STATEMENT EXECUTE FUNCTION governance.forbid_truncate();

-- 0013 の追記オンリー表(minutes / minute_resolutions / stances)は 0015 以前に
-- 書かれており文トリガを持たない。0018 が 0014 の穴を塞いだのと同じ理由で、
-- ここで governance スキーマ全体を標準へ揃える(否認証跡だけ守っても、議事録が
-- TRUNCATE できるなら統制としては同じ穴が残る)。
CREATE OR REPLACE TRIGGER minutes_no_truncate
    BEFORE TRUNCATE ON governance.minutes
    FOR EACH STATEMENT EXECUTE FUNCTION governance.forbid_truncate();
CREATE OR REPLACE TRIGGER minute_resolutions_no_truncate
    BEFORE TRUNCATE ON governance.minute_resolutions
    FOR EACH STATEMENT EXECUTE FUNCTION governance.forbid_truncate();
CREATE OR REPLACE TRIGGER stances_no_truncate
    BEFORE TRUNCATE ON governance.stances
    FOR EACH STATEMENT EXECUTE FUNCTION governance.forbid_truncate();
CREATE OR REPLACE TRIGGER decisions_no_truncate
    BEFORE TRUNCATE ON governance.decisions
    FOR EACH STATEMENT EXECUTE FUNCTION governance.forbid_truncate();

REVOKE TRUNCATE ON governance.decision_vetoes FROM PUBLIC;
REVOKE TRUNCATE ON governance.minutes FROM PUBLIC;
REVOKE TRUNCATE ON governance.minute_resolutions FROM PUBLIC;
REVOKE TRUNCATE ON governance.stances FROM PUBLIC;
REVOKE TRUNCATE ON governance.decisions FROM PUBLIC;

-- ════════════════════════════════════════════════════════════════════════════
-- 3. 現決定 view: 承認記録 + 否認の履歴 = いま効力を持っている状態
-- ════════════════════════════════════════════════════════════════════════════
-- 「decisions を直接読む」コードは、否認された決定を承認済みとして扱ってしまう。
-- 否認の存在を読み飛ばせないよう、現決定は本 view 経由で読むことを標準とする
-- (A-18 の `Approved:` 突合・deemed_ratio 集計・ダッシュボード表示の読み口)。
--
-- ── 順序は veto_id 単独(独立役員審査 C-10)──────────────────────────────────
-- 当初は (vetoed_at DESC, veto_id DESC) としていたが、vetoed_at は既定値 now() の
-- ほかに呼び出し側が任意の値を渡せる列であり、過去日時を持つ行を後から追記すると
-- 「最新の追記」と「最新の時刻」が食い違う。追記オンリー表で「最後に書かれた行」を
-- 一意に決めるのは IDENTITY である veto_id のみなので、順序は veto_id に統一する。
--
-- ── 列単位の解決(独立役員審査 C-4)────────────────────────────────────────
-- 行単位で最新1行を採ると、revert_commit を持たない追記(例: 派生効果の追加報告)が
-- 既に記録済みの revert_commit を NULL で覆い隠す — 情報の無い追記が既記録を消す。
-- revert_commit / derived_effects_ref は**列ごとに最後に値が入った行**の値を採る。
-- ただし撤回(withdrawal)より前の取消情報は現状を説明しないため、直近の撤回より
-- 後の行だけを対象にする。
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
    resolved.derived_effects_ref
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
        -- 直近の撤回。撤回が無ければ 0(= 全行が対象)。
        SELECT coalesce(max(vw.veto_id), 0) AS since
        FROM governance.decision_vetoes vw
        WHERE vw.decision_id = d.id AND vw.kind = 'withdrawal'
    ) w
) resolved ON true;

-- ════════════════════════════════════════════════════════════════════════════
-- データカタログ用コメント
-- ════════════════════════════════════════════════════════════════════════════
COMMENT ON TABLE governance.decision_vetoes IS
    '承認決定の事後否認(追記オンリー)。定款 v0.4 第3条「代表はいつでも否認できる」の証跡。'
    'decisions を UPDATE すると Approved トレーラの意味が遡及改変されるため別表とし、'
    '現在の効力は governance.current_decisions view で合成する。'
    '1決定に複数行を許す(否認 → 取消完了 / 撤回 を追記で表現。view は最新行を採る)。';
COMMENT ON COLUMN governance.decision_vetoes.decision_id IS
    '否認対象 governance.decisions.id。decision が approve / deemed の決定に限る'
    '(check_veto_target トリガ)— 却下・質問を否認可能にすると阻止の根拠が消える。';
COMMENT ON COLUMN governance.decision_vetoes.kind IS
    'veto(否認)|revert_complete(取消完了の報告)|withdrawal(否認そのものの撤回)。'
    'current_decisions は最新行(veto_id 最大)の kind が withdrawal なら否認されていないと返す。';
COMMENT ON COLUMN governance.decision_vetoes.vetoed_by IS
    '否認者(オーナー検証済みの Discord ユーザー ID)。検証はアプリ層 — 0007 と同じ制約。';
COMMENT ON COLUMN governance.decision_vetoes.reason IS '理由(必須)。執行側の取消対象を特定する。';
COMMENT ON COLUMN governance.decision_vetoes.revert_commit IS
    '取消(git revert・設定巻き戻し)のコミット SHA。否認時点で未確定なら NULL、'
    '確定後に kind=revert_complete の行で追記する。view は列単位で最新の非 NULL 値を採る。';
COMMENT ON COLUMN governance.decision_vetoes.derived_effects_ref IS
    '取消不能な派生効果一覧の参照(#運営 への報告 — 定款第3条の報告義務)。無ければ NULL。';
COMMENT ON COLUMN governance.decision_vetoes.run_id IS
    '記録したジョブ実行(meta.runs)。否認は代表の作為でありジョブの生成物ではないため NULL 可'
    '(0013 の NOT NULL はジョブ産出物ゆえ)。出所を表す origin 列は継続検討中。';

COMMENT ON VIEW governance.current_decisions IS
    '現決定(承認記録 + 否認履歴の合成)。effective_decision は decisions.decision の4値 + '
    '''vetoed''。承認記録を読むコードは decisions ではなく本 view を読む'
    '(否認された決定を承認済みとして扱わないため)。'
    'revert_commit / derived_effects_ref は列単位で最新の非 NULL 値(直近の撤回より後)。';

COMMENT ON TABLE governance.decisions IS
    '承認フローの決定記録(追記オンリー — 0021)。押下者のオーナー検証済み(30-press-discord §5)。'
    '訂正・撤回は governance.decision_vetoes への追記で表現し、現在の効力は '
    'governance.current_decisions view が返す。';

-- 0019 のカタログコメントは「否認を記録する場所が無い」と述べていた。本 migration で
-- 解消したため実態に合わせて更新する(stale なカタログを残さない — 0019 C-7 の教訓)。
COMMENT ON COLUMN governance.decisions.decision IS
    'approve|reject|question|deemed。approve=代表の明示承認(定款第3条の3専決事項)、'
    'deemed=みなし承認(#承認 への通知と同時に発効 — 定款 v0.4 第3条。decided_by は '
    'system:<source>、3専決の kind には付けられない)。両者を区別することで監査部門が '
    'deemed_ratio(形骸化アラート)を計算できる。'
    '事後否認は governance.decision_vetoes に追記され(対象は approve / deemed のみ)、'
    '現在の効力は governance.current_decisions view が返す(0021)。';
