-- 0013_governance_assets.sql
-- 役職資産の永続記憶(05-governance §2・§4): 議事録(minutes)・決議マーク
-- (minute_resolutions)・役職ごとの主張要約(stances)。
--
-- 0007 の governance.decisions(承認記録・最小形)との関係:
--   decisions          … 「承認という行為」の記録。Discord 承認 UI・みなし承認が書く
--                        (1提案=1決定・proposal_ref UNIQUE で冪等)
--   minutes            … 会議体の議事録(全対話を保存 — 05 §4)。それ自体は発効しない
--   minute_resolutions … 議事録のうち「決議」として明示的にマークされた項目のみが発効する
--                        (雑談が政策にならないための境界)。承認事項に紐づく決議は
--                        proposal_ref で decisions と突合できる(監査 A-5 / A-13)
--   stances            … 役職ごとの過去の主張・懸念の要約。次回セッションの着任時に
--                        直近 N 件を読み込む(「前回私はこう懸念した」の引き継ぎ — 05 §2)
--
-- 整合性の要:
--   1. 議事録・決議・stances は証憑・引継記録なのですべて追記オンリー(05 §4・§6-3)。
--      UPDATE / DELETE はトリガで禁止する(0005 の ledger.forbid_mutation と同型)。
--      stances の訂正は撤回行の追記で表現する(kind='retraction' + retracts 参照 — 下記)
--   2. 全テーブル run_id 必須+meta.runs への FK(リネージ — 不変原則3・0001 の慣行)
--
-- 冪等: IF NOT EXISTS / CREATE OR REPLACE。

CREATE SCHEMA IF NOT EXISTS governance;

-- ── 議事録(append-only)──────────────────────────────────────────────────────
-- 会議体は 05 §4 の4つ+役員室チャット(05 §5: 会話は自動で議事録候補として保存)。
CREATE TABLE IF NOT EXISTS governance.minutes (
    minute_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    meeting    text NOT NULL
               CHECK (meeting IN ('investment_committee',   -- 投資委員会(月次)
                                  'management_meeting',     -- 経営会議(月次)
                                  'extraordinary_committee',-- 臨時委員会(トリガ時)
                                  'effectiveness_review',   -- ガバナンス実効性評価(年次)
                                  'office_chat')),          -- 役員室チャット(随時)
    held_at    timestamptz NOT NULL,
    attendees  text[] NOT NULL CHECK (cardinality(attendees) >= 1),
               -- 役職名の配列(representative|cio|independent_officer|audit — governance.yaml roles)
    body_md    text NOT NULL,               -- 全対話(Markdown)。要約でなく全文を残す(05 §4)
    run_id     bigint NOT NULL REFERENCES meta.runs (run_id),  -- 記録したジョブ実行(リネージ)
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS minutes_meeting_idx ON governance.minutes (meeting, held_at);

-- ── 決議マーク(append-only)──────────────────────────────────────────────────
-- 発効する決定は「決議」としてマークされたもののみ(05 §4)。決議ボタンは代表のみ押せる(05 §5)。
CREATE TABLE IF NOT EXISTS governance.minute_resolutions (
    resolution_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    minute_id     bigint NOT NULL REFERENCES governance.minutes (minute_id),
    seq           int NOT NULL,             -- 同一議事録内の決議番号(1 起点)
    title         text NOT NULL,
    resolution_md text NOT NULL,            -- 決議本文(反対意見・却下理由も残す — 05 §6-3)
    proposal_ref  text,                     -- 承認事項なら governance.decisions.proposal_ref と突合
    resolved_by   text NOT NULL             -- 決議者。決議ボタンは代表のみ押せる(05 §5)を CHECK で強制
                  CHECK (resolved_by = 'representative'),  -- governance.yaml roles のキー
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (minute_id, seq)
);

-- 議事録・決議への UPDATE / DELETE を禁止(追記オンリー。訂正は追記で行う)。
CREATE OR REPLACE FUNCTION governance.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% は % では禁止(追記オンリー)。議事録・決議は証憑(05-governance §4)。訂正は追記で行う',
        TG_OP, TG_TABLE_NAME;
END;
$$;

CREATE OR REPLACE TRIGGER minutes_no_mutation
    BEFORE UPDATE OR DELETE ON governance.minutes
    FOR EACH ROW EXECUTE FUNCTION governance.forbid_mutation();

CREATE OR REPLACE TRIGGER minute_resolutions_no_mutation
    BEFORE UPDATE OR DELETE ON governance.minute_resolutions
    FOR EACH ROW EXECUTE FUNCTION governance.forbid_mutation();

REVOKE UPDATE, DELETE ON governance.minutes FROM PUBLIC;
REVOKE UPDATE, DELETE ON governance.minute_resolutions FROM PUBLIC;

-- ── 役職ごとの主張・懸念の要約(着任時読み込み用)────────────────────────────
-- role は CHECK で縛らない: governance.yaml roles が正であり、役職の追加(FM ポッド等)の
-- たびにスキーマ変更を要しないため。妥当性はローダ(src/ryza/governance/personas.py)と
-- 監査 A-13(governance.yaml との突合)が担う。
--
-- 追記オンリー+撤回行方式(独立役員審査 2026-08-03 の是正1): 着任時の引継記録が
-- 事後改変されると「前回の懸念」の証跡性が失われるため、minutes と同様に UPDATE/DELETE を
-- 禁止する。訂正は kind='retraction' の行を追記し retracts で対象行を指す方式を採用
-- (superseded_by 列を対象行に書く方式は、対象行の UPDATE を要するため追記オンリーと
-- 両立しない)。ローダは撤回された行と撤回行自体を除外して読む。
CREATE TABLE IF NOT EXISTS governance.stances (
    stance_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role      text NOT NULL,                -- cio|independent_officer|audit ...(governance.yaml roles)
    kind      text NOT NULL
              CHECK (kind IN ('claim',        -- 主張
                              'concern',      -- 懸念(独立役員は毎回最低1件 — 05 §3)
                              'dissent',      -- 反対意見・少数意見(議論規約2)
                              'retraction')), -- 撤回(訂正の追記表現。summary に理由)
    summary   text NOT NULL,                -- 要約(着任プロンプトに添付される)
    minute_id bigint REFERENCES governance.minutes (minute_id),  -- 出所議事録(あれば)
    retracts  bigint REFERENCES governance.stances (stance_id),  -- 撤回対象(retraction のみ必須)
    stated_at timestamptz NOT NULL DEFAULT now(),
    run_id    bigint NOT NULL REFERENCES meta.runs (run_id),  -- 記録したジョブ実行(リネージ)
    CHECK ((kind = 'retraction') = (retracts IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS stances_role_idx ON governance.stances (role, stated_at DESC);
CREATE INDEX IF NOT EXISTS stances_retracts_idx ON governance.stances (retracts)
    WHERE retracts IS NOT NULL;

CREATE OR REPLACE TRIGGER stances_no_mutation
    BEFORE UPDATE OR DELETE ON governance.stances
    FOR EACH ROW EXECUTE FUNCTION governance.forbid_mutation();

REVOKE UPDATE, DELETE ON governance.stances FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON TABLE governance.minutes IS
    '会議体の議事録(全対話・追記オンリー)。発効する決定は minute_resolutions のみ(05 §4)。';
COMMENT ON COLUMN governance.minutes.meeting IS
    'investment_committee|management_meeting|extraordinary_committee|effectiveness_review|office_chat。';
COMMENT ON COLUMN governance.minutes.attendees IS
    '出席役職(representative|cio|independent_officer|audit)。';
COMMENT ON COLUMN governance.minutes.body_md IS '全対話の Markdown。要約でなく全文(05 §4)。';

COMMENT ON TABLE governance.minute_resolutions IS
    '決議マーク。議事録中で明示的に決議とされた項目のみが発効する(雑談が政策にならない境界)。';
COMMENT ON COLUMN governance.minute_resolutions.proposal_ref IS
    '承認事項の場合 governance.decisions.proposal_ref と突合(A-5/A-13)。';
COMMENT ON COLUMN governance.minute_resolutions.resolved_by IS '決議者。決議ボタンは代表のみ(05 §5)。';

COMMENT ON TABLE governance.stances IS
    '役職ごとの主張・懸念の要約(追記オンリー)。着任時に直近 N 件を読み込む(05 §2 永続記憶)。'
    '正本は minutes。訂正は retraction 行の追記で表現。';
COMMENT ON COLUMN governance.stances.role IS 'governance.yaml roles のキー(cio 等)。';
COMMENT ON COLUMN governance.stances.kind IS
    'claim(主張)|concern(懸念)|dissent(反対・少数意見)|retraction(撤回)。';
COMMENT ON COLUMN governance.stances.minute_id IS '出所議事録(governance.minutes)。無ければ NULL。';
COMMENT ON COLUMN governance.stances.retracts IS
    '撤回対象の stance_id(kind=retraction のみ・CHECK で強制)。撤回された行はローダが除外する。';
