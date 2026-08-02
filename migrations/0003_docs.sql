-- 0003_docs.sql
-- docs スキーマ: 文書・埋め込み・市場観・リサーチレポート。設計書 §3 に完全準拠。

CREATE SCHEMA IF NOT EXISTS docs;

-- pgvector（埋め込み）。既定の public スキーマに vector 型を導入する。
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE docs.documents (
    doc_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_type  text NOT NULL,     -- news|filing|paper|social|gov|court|policy
    source_name  text NOT NULL,     -- 'TDnet', 'EDINET', 'arXiv', 'reddit/r/...', '5ch/...'
    url          text,
    title        text,
    body         text,
    lang         text,
    published_at timestamptz,
    as_of        timestamptz NOT NULL,       -- 取得時点
    content_hash bytea NOT NULL,             -- 重複排除・改竄検知
    raw_ref      text,                       -- 証憑ストア（GCS）の原文 URI
    meta         jsonb,                      -- 発行者・銘柄タグ・分類ラベル（階層0 が付与）
    run_id       bigint NOT NULL,
    UNIQUE (source_name, content_hash)
);

CREATE TABLE docs.embeddings (
    doc_id    bigint PRIMARY KEY REFERENCES docs.documents(doc_id),
    model     text NOT NULL,
    embedding vector(1024) NOT NULL           -- pgvector。HNSW インデックス
);

-- HNSW インデックス（コサイン距離）。
CREATE INDEX embeddings_hnsw ON docs.embeddings
    USING hnsw (embedding vector_cosine_ops);

-- 市場観ステート（リサーチ部門が常時更新する「現在の見解」）
CREATE TABLE docs.market_view (
    view_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts         timestamptz NOT NULL,
    regime     jsonb NOT NULL,        -- {'jp_equity': 'risk_on', 'rates': 'tightening', ...}
    key_risks  jsonb NOT NULL,        -- 注目リスクと確度
    changes    jsonb,                 -- 前版からの差分（速報トリガ判定に使用）
    basis_refs bigint[] NOT NULL,     -- 根拠 doc_id / report_id
    run_id     bigint NOT NULL
);

CREATE TABLE docs.research_reports (
    report_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent       text NOT NULL,         -- macro|micro|sentiment|editor|press
    report_type text NOT NULL,         -- daily|thematic|morning_press|flash
    scores      jsonb,                 -- 構造化スコア（下流はここだけに依存）
    body_md     text,                  -- 人間向け本文（執筆規格準拠、文ごとの抽象度タグ付き）
    input_refs  jsonb NOT NULL,        -- 参照した doc_id / bars 範囲 / view_id
    as_of       timestamptz NOT NULL,
    run_id      bigint NOT NULL
);

-- データカタログの源泉となるコメント。
COMMENT ON SCHEMA docs IS '文書・埋め込み・市場観。データ基盤部・リサーチ部門が書き込み。';

COMMENT ON TABLE docs.documents IS '原文書（ニュース・開示・論文・SNS 等）。';
COMMENT ON COLUMN docs.documents.doc_id IS '文書の一意 ID。';
COMMENT ON COLUMN docs.documents.source_type IS 'news|filing|paper|social|gov|court|policy。';
COMMENT ON COLUMN docs.documents.source_name IS '情報源名（TDnet, EDINET, arXiv ...）。';
COMMENT ON COLUMN docs.documents.url IS '原文 URL。';
COMMENT ON COLUMN docs.documents.title IS 'タイトル。';
COMMENT ON COLUMN docs.documents.body IS '本文。';
COMMENT ON COLUMN docs.documents.lang IS '言語。';
COMMENT ON COLUMN docs.documents.published_at IS '発行時刻。';
COMMENT ON COLUMN docs.documents.as_of IS '取得時点（point-in-time）。';
COMMENT ON COLUMN docs.documents.content_hash IS '本文ハッシュ（重複排除・改竄検知）。';
COMMENT ON COLUMN docs.documents.raw_ref IS '証憑ストア（GCS）の原文 URI。';
COMMENT ON COLUMN docs.documents.meta IS '発行者・銘柄タグ・分類ラベル（階層0 付与）。';
COMMENT ON COLUMN docs.documents.run_id IS '取込ジョブ実行（リネージ）。';

COMMENT ON TABLE docs.embeddings IS '文書の埋め込みベクトル（1024 次元、HNSW）。';
COMMENT ON COLUMN docs.embeddings.doc_id IS '対象文書 ID。';
COMMENT ON COLUMN docs.embeddings.model IS '埋め込みモデル名。';
COMMENT ON COLUMN docs.embeddings.embedding IS '1024 次元ベクトル（pgvector）。';

COMMENT ON TABLE docs.market_view IS 'リサーチ部門の現在の市場観ステート。';
COMMENT ON COLUMN docs.market_view.view_id IS '市場観版の一意 ID。';
COMMENT ON COLUMN docs.market_view.ts IS '版の時刻。';
COMMENT ON COLUMN docs.market_view.regime IS 'レジーム判定（jsonb）。';
COMMENT ON COLUMN docs.market_view.key_risks IS '注目リスクと確度。';
COMMENT ON COLUMN docs.market_view.changes IS '前版からの差分（速報トリガ判定）。';
COMMENT ON COLUMN docs.market_view.basis_refs IS '根拠 doc_id / report_id の配列。';
COMMENT ON COLUMN docs.market_view.run_id IS '生成ジョブ実行（リネージ）。';

COMMENT ON TABLE docs.research_reports IS 'リサーチ・執筆エージェントのレポート。';
COMMENT ON COLUMN docs.research_reports.report_id IS 'レポートの一意 ID。';
COMMENT ON COLUMN docs.research_reports.agent IS 'macro|micro|sentiment|editor|press。';
COMMENT ON COLUMN docs.research_reports.report_type IS 'daily|thematic|morning_press|flash。';
COMMENT ON COLUMN docs.research_reports.scores IS '構造化スコア（下流はここだけに依存）。';
COMMENT ON COLUMN docs.research_reports.body_md IS '人間向け本文（執筆規格準拠）。';
COMMENT ON COLUMN docs.research_reports.input_refs IS '参照した doc_id / bars 範囲 / view_id。';
COMMENT ON COLUMN docs.research_reports.as_of IS '情報が利用可能になった時点。';
COMMENT ON COLUMN docs.research_reports.run_id IS '生成ジョブ実行（リネージ）。';
