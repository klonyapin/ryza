-- 0001_meta.sql
-- meta スキーマ: リネージ・ジョブ実行・監査。設計書 §6 に完全準拠。
-- 追記オンリー。各ジョブは書き込む全行に run_id を刻み、参照した入力を
-- lineage_edges に登録する。

CREATE SCHEMA IF NOT EXISTS meta;

-- schema_migrations はランナーのブートストラップで作成済みだが、宣言的な源泉と
-- してここでも冪等に定義しておく（コメント付与のため）。
CREATE TABLE IF NOT EXISTS meta.schema_migrations (
    version     text PRIMARY KEY,
    filename    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE meta.runs (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name     text NOT NULL,               -- 'ingest.jquants.daily', 'research.macro', ...
    code_version text NOT NULL,               -- git commit
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz,
    status       text NOT NULL,               -- running|success|failed
    params       jsonb,
    cost         jsonb                        -- LLM トークン・モデル階層別（経営管理部が集計）
);

-- リネージ: 成果物（どのテーブルの行でも）→ 入力への辺
CREATE TABLE meta.lineage_edges (
    from_kind text NOT NULL, from_id text NOT NULL,   -- 例: ('research_reports','123')
    to_kind   text NOT NULL, to_id   text NOT NULL,   -- 例: ('documents','456')
    run_id    bigint NOT NULL REFERENCES meta.runs,
    PRIMARY KEY (from_kind, from_id, to_kind, to_id)
);

CREATE TABLE meta.audit_findings (
    finding_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    audit_item        text NOT NULL,          -- 'A-1'〜'A-10'
    severity          text NOT NULL,          -- info|warn|critical
    detail            jsonb NOT NULL,
    found_at          timestamptz NOT NULL,
    status            text NOT NULL DEFAULT 'open',  -- open|acknowledged|resolved
    resolved_evidence bigint                  -- 解消の証憑
);

-- データカタログの源泉となるコメント。
COMMENT ON SCHEMA meta IS 'リネージ・ジョブ実行・監査。各ジョブが追記のみ。';

COMMENT ON TABLE meta.schema_migrations IS '適用済みマイグレーションの台帳（自作ランナーが記録）。';
COMMENT ON COLUMN meta.schema_migrations.version IS 'マイグレーション連番（例 0001）。';
COMMENT ON COLUMN meta.schema_migrations.filename IS '適用した SQL ファイル名。';
COMMENT ON COLUMN meta.schema_migrations.applied_at IS '適用時刻。';

COMMENT ON TABLE meta.runs IS 'ジョブ実行記録。生成物の run_id 参照先＝リネージの鍵。';
COMMENT ON COLUMN meta.runs.run_id IS 'ジョブ実行の一意 ID。';
COMMENT ON COLUMN meta.runs.job_name IS 'ジョブ名（例 ingest.jquants.daily）。';
COMMENT ON COLUMN meta.runs.code_version IS '実行時の git commit。';
COMMENT ON COLUMN meta.runs.started_at IS '開始時刻。';
COMMENT ON COLUMN meta.runs.finished_at IS '終了時刻（実行中は NULL）。';
COMMENT ON COLUMN meta.runs.status IS 'running|success|failed。';
COMMENT ON COLUMN meta.runs.params IS '実行パラメータ。';
COMMENT ON COLUMN meta.runs.cost IS 'LLM トークン・モデル階層別コスト（経営管理部が集計）。';

COMMENT ON TABLE meta.lineage_edges IS '成果物→入力の有向辺。SQL でリネージを遡れる。';
COMMENT ON COLUMN meta.lineage_edges.from_kind IS '成果物の種別（テーブル名）。';
COMMENT ON COLUMN meta.lineage_edges.from_id IS '成果物の ID。';
COMMENT ON COLUMN meta.lineage_edges.to_kind IS '入力の種別（テーブル名）。';
COMMENT ON COLUMN meta.lineage_edges.to_id IS '入力の ID。';
COMMENT ON COLUMN meta.lineage_edges.run_id IS 'この辺を登録したジョブ実行。';

COMMENT ON TABLE meta.audit_findings IS '監査部門の検出事項（A-1〜A-10）。';
COMMENT ON COLUMN meta.audit_findings.finding_id IS '検出事項の一意 ID。';
COMMENT ON COLUMN meta.audit_findings.audit_item IS '監査項目 A-1〜A-10。';
COMMENT ON COLUMN meta.audit_findings.severity IS 'info|warn|critical。';
COMMENT ON COLUMN meta.audit_findings.detail IS '検出内容の詳細。';
COMMENT ON COLUMN meta.audit_findings.found_at IS '検出時刻。';
COMMENT ON COLUMN meta.audit_findings.status IS 'open|acknowledged|resolved。';
COMMENT ON COLUMN meta.audit_findings.resolved_evidence IS '解消の証憑 evidence_id。';
