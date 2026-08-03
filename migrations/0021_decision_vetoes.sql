-- 0021: governance.decision_vetoes(承認決定の事後否認・追記オンリー)
--       + governance.current_decisions(否認を反映した「現決定」view)
--
-- 根拠: 定款 v0.4 第3条(docs/design/06-constitution.md L31 前後)
--   「通知と同時に発効する。代表は**いつでも否認でき**、否認された変更は遅滞なく
--     取り消す(git revert・設定巻き戻し)。否認までの間に生じた派生効果(その規則の
--     下で実行された取引・生成物)は取消不能な場合があるため、執行側は否認受領後
--     すみやかに派生効果の一覧を `#運営` へ報告する」
-- 機械可読版: config/governance.yaml の deemed_approval.veto = anytime。
-- 独立役員審査: docs/reviews/0019-decisions-deemed-independent-review.md C-1(重大)。
--
-- ── なぜ別テーブルなのか(C-1 の是正が 0007 の UPDATE では成立しない理由)──────
-- governance.decisions は 0007 の UNIQUE(proposal_ref) で 1提案=1行に固定されている。
-- そのため否認を「新しい decisions 行」として INSERT できない。一方、既存行を UPDATE
-- すると、保護領域コミットの `Approved: <decisions の ID>` トレーラ(定款第5条・
-- 06-constitution.md L61)が指す承認記録の意味が**遡及的に**書き換わり、監査 A-13-1 の
-- 突合が過去に遡って壊れる(「承認済み」と記録されていた事実そのものが消える)。
-- 否認は承認の取り消しではなく**承認の後に起きた別の事実**であるため、追記オンリーの
-- 別テーブルに記録し、両者の合成(= 現在の効力)は view で導出するのが正しい形になる。
--
-- 結果として、監査部門が追う deemed_ratio(否認ゼロの長期継続 = 形骸化アラート —
-- config/governance.yaml deemed_approval.audit_metric)が初めて計算可能になる。
-- 否認を記録する場所が無い間、このアラートは構造的に発火不能だった。
--
-- ── 適用範囲: 全 decision に否認を許す(明示承認も含む)────────────────────
-- 定款第3条の否認権は「みなし承認」の文脈で定められている。しかしスキーマ側で
-- decision='deemed' の行だけに否認を限定する積極的理由は無い:
--   1. 代表が自分の明示承認(approve)を後から撤回する事態は現実に起こりうる。
--      そのときスキーマが記録を拒むと、証跡が DB の外(Discord ログ)へ逃げる
--   2. 逆に「明示承認は否認できない」という制約は、定款のどこにも書かれていない
--      規範をスキーマが新設することになる(下層は上層に反する定めを置けない —
--      定款第4条。ここでは「上層に無い禁止」を勝手に作らない側に倒す)
--   3. 一般化しても統制は緩まない。3専決の**発効**を偽装できるのは
--      decisions 側の decision='deemed' であり、否認は常に効力を**弱める**方向にしか
--      働かないため、否認可能範囲を広げることで新しい抜け道は生まれない
-- したがって FK は governance.decisions 全体に張り、decision の値では絞らない。
--
-- ── 1決定に複数の否認行を許す理由 ────────────────────────────────────────
-- 追記オンリーのため、否認時点では未確定の情報(取消コミット revert_commit・
-- 取消不能な派生効果の一覧 derived_effects_ref)を後から UPDATE で埋められない。
-- そこで「否認の記録」と「取消完了の記録」を**同一 decision_id への追記**で表現する:
--   1行目: 否認(reason のみ。revert_commit は NULL = 取消未完了)
--   2行目: 取消完了(同 reason 要旨 + revert_commit + derived_effects_ref)
-- current_decisions view は最新行を返すため、現決定には常に最も新しい状態が映る。
-- UNIQUE(decision_id) を張るとこの表現ができなくなるので張らない。
--
-- 冪等: CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE(0013 の流儀)。

CREATE SCHEMA IF NOT EXISTS governance;

CREATE TABLE IF NOT EXISTS governance.decision_vetoes (
    veto_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id bigint NOT NULL REFERENCES governance.decisions (id),
                -- 否認対象の承認記録(0007 の PK は `id`)。存在しない決定は FK が拒否する
    vetoed_by   text NOT NULL CHECK (btrim(vetoed_by) <> ''),
                -- 否認者。decisions.decided_by と同じ表記(オーナー検証済みの Discord
                -- ユーザー ID)。DB 側でオーナー ID を強制できないのは 0007 と同じ制約
                -- (オーナー ID は config/secrets 側にあり、検証はアプリ層 —
                --  src/ryza/governance/decisions.py・src/ryza/bot/approvals.is_owner)
    reason      text NOT NULL CHECK (btrim(reason) <> ''),
                -- 否認理由。定款第3条は否認に取消義務を課すため、理由の無い否認は
                -- 執行側が何を巻き戻すべきか判断できない。空文字も拒否する
    revert_commit      text,
                -- 取消(git revert・設定巻き戻し)のコミット SHA。否認時点では未確定の
                -- ことがあるため NULL 可。確定後は同 decision_id への追記で記録する
    derived_effects_ref text,
                -- 取消不能な派生効果の一覧の参照(#運営 への報告メッセージ ID・
                -- レポート URL 等 — 定款第3条の報告義務)。派生効果が無ければ NULL
    run_id      bigint REFERENCES meta.runs (run_id),
                -- 記録したジョブ実行(リネージ — 不変原則3)。NULL 可なのは意図的:
                -- 否認は Discord ボタン等、ジョブ Run を持たない経路からも行われる。
                -- NOT NULL にすると「Run が無いから否認を記録できない」状況が生まれ、
                -- 定款第3条の「いつでも否認できる」を実装が妨げることになる
    vetoed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS decision_vetoes_decision_idx
    ON governance.decision_vetoes (decision_id, vetoed_at DESC, veto_id DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 追記オンリーの強制(0013 の minutes/stances と同型)
-- ────────────────────────────────────────────────────────────────────────────
-- governance.forbid_mutation() は 0013 が定義済み(メッセージは議事録向けの文面だが、
-- 本表も「証憑としての追記オンリー」で同じ性質のため再利用する)。
CREATE OR REPLACE TRIGGER decision_vetoes_no_mutation
    BEFORE UPDATE OR DELETE ON governance.decision_vetoes
    FOR EACH ROW EXECUTE FUNCTION governance.forbid_mutation();

REVOKE UPDATE, DELETE ON governance.decision_vetoes FROM PUBLIC;

-- **TRUNCATE は行トリガを迂回する**(0015 の独立役員審査で実証・0018 が標準化)。
-- 行トリガだけでは `TRUNCATE governance.decision_vetoes` の一撃で否認証跡が全て消え、
-- 「否認ゼロ」という監査上もっとも都合のよい状態を作れてしまう。文トリガ+REVOKE で塞ぐ。
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

REVOKE TRUNCATE ON governance.decision_vetoes FROM PUBLIC;

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

REVOKE TRUNCATE ON governance.minutes FROM PUBLIC;
REVOKE TRUNCATE ON governance.minute_resolutions FROM PUBLIC;
REVOKE TRUNCATE ON governance.stances FROM PUBLIC;
REVOKE TRUNCATE ON governance.decisions FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- 現決定 view: 承認記録 + 最新の否認 = いま効力を持っている状態
-- ────────────────────────────────────────────────────────────────────────────
-- 「decisions を直接読む」コードは、否認された決定を承認済みとして扱ってしまう。
-- 否認の存在を読み飛ばせないよう、現決定は本 view 経由で読むことを標準とする
-- (A-13 の `Approved:` 突合・deemed_ratio 集計の読み口)。
--
-- effective_decision の語彙は decisions.decision の4値 + 'vetoed'。否認された決定は
-- 元の decision(approve/deemed/…)を recorded_decision に残したまま 'vetoed' を返す
-- ため、「何が承認され、それが後に否認された」という時系列が失われない。
CREATE OR REPLACE VIEW governance.current_decisions AS
SELECT
    d.id                                   AS decision_id,
    d.proposal_ref,
    d.kind,
    d.decision                             AS recorded_decision,
    CASE WHEN v.veto_id IS NULL THEN d.decision ELSE 'vetoed' END AS effective_decision,
    (v.veto_id IS NOT NULL)                AS is_vetoed,
    d.decided_by,
    d.note,
    d.channel_msg_id,
    d.decided_at,
    v.veto_id,
    v.vetoed_by,
    v.reason                               AS veto_reason,
    v.revert_commit,
    v.derived_effects_ref,
    v.vetoed_at
FROM governance.decisions d
LEFT JOIN LATERAL (
    SELECT vv.veto_id, vv.vetoed_by, vv.reason, vv.revert_commit,
           vv.derived_effects_ref, vv.vetoed_at
    FROM governance.decision_vetoes vv
    WHERE vv.decision_id = d.id
    ORDER BY vv.vetoed_at DESC, vv.veto_id DESC
    LIMIT 1
) v ON true;

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON TABLE governance.decision_vetoes IS
    '承認決定の事後否認(追記オンリー)。定款 v0.4 第3条「代表はいつでも否認できる」の証跡。'
    'decisions を UPDATE すると Approved トレーラの意味が遡及改変されるため別表とし、'
    '現在の効力は governance.current_decisions view で合成する。'
    '1決定に複数行を許す(否認 → 取消完了 を追記で表現。view は最新行を採る)。';
COMMENT ON COLUMN governance.decision_vetoes.decision_id IS
    '否認対象 governance.decisions.id。deemed に限らず全 decision を否認できる'
    '(否認は効力を弱める方向にしか働かないため一般化しても統制は緩まない)。';
COMMENT ON COLUMN governance.decision_vetoes.vetoed_by IS
    '否認者(オーナー検証済みの Discord ユーザー ID)。検証はアプリ層 — 0007 と同じ制約。';
COMMENT ON COLUMN governance.decision_vetoes.reason IS '否認理由(必須)。執行側の取消対象を特定する。';
COMMENT ON COLUMN governance.decision_vetoes.revert_commit IS
    '取消(git revert・設定巻き戻し)のコミット SHA。否認時点で未確定なら NULL、確定後に追記。';
COMMENT ON COLUMN governance.decision_vetoes.derived_effects_ref IS
    '取消不能な派生効果一覧の参照(#運営 への報告 — 定款第3条の報告義務)。無ければ NULL。';
COMMENT ON COLUMN governance.decision_vetoes.run_id IS
    '記録したジョブ実行(meta.runs)。Discord 経路など Run を持たない否認があるため NULL 可。';

COMMENT ON VIEW governance.current_decisions IS
    '現決定(承認記録 + 最新の否認)。effective_decision は decisions.decision の4値 + '
    '''vetoed''。承認記録を読むコードは decisions ではなく本 view を読む'
    '(否認された決定を承認済みとして扱わないため)。';

-- 0019 のカタログコメントは「否認を記録する場所が無い」と述べていた。本 migration で
-- 解消したため実態に合わせて更新する(stale なカタログを残さない — 0019 C-7 の教訓)。
COMMENT ON COLUMN governance.decisions.decision IS
    'approve|reject|question|deemed。approve=代表の明示承認(定款第3条の3専決事項)、'
    'deemed=みなし承認(#承認 への通知と同時に発効 — 定款 v0.4 第3条。decided_by は '
    'system:<source>、3専決の kind には付けられない)。両者を区別することで監査部門が '
    'deemed_ratio(形骸化アラート)を計算できる。'
    '事後否認は governance.decision_vetoes に追記され、現在の効力は '
    'governance.current_decisions view が返す(0021)。';
