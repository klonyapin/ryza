-- 0009_preprocess.sql
-- 階層0前処理（T-010）の下流振り分けキュー。docs スキーマにビューを追加する。
-- 既存テーブル（0001-0008）は変更しない。前処理は docs.documents.meta（jsonb）に結果を
-- 書き込み、これらのビューが meta を読んで重要度別に振り分ける（設計 20-research §3）。
--
-- ビューのみ・冪等（CREATE OR REPLACE）。前処理の結果格納形は runner.preprocess_document の
-- meta パッチに対応する:
--   meta.preprocess_version, meta.preprocessed_at, meta.lang,
--   meta.importance.{score,tier}, meta.dedup.{is_duplicate,duplicate_of},
--   meta.classification.category, meta.tags.instrument_ids

-- 未処理キュー: まだ現行バージョンで前処理されていない文書（runner の検出条件と一致）。
CREATE OR REPLACE VIEW docs.preprocess_pending AS
SELECT
    doc_id, source_type, source_name, title, published_at, as_of,
    meta->>'preprocess_version' AS preprocess_version
FROM docs.documents
WHERE meta->>'preprocess_version' IS DISTINCT FROM '1'
ORDER BY doc_id ASC;

-- 前処理済み・準重複を除いた「生きた」文書に重要度メタを展開したベースビュー。
CREATE OR REPLACE VIEW docs.documents_enriched AS
SELECT
    d.doc_id,
    d.source_type,
    d.source_name,
    d.title,
    d.lang,
    d.published_at,
    d.as_of,
    d.meta->'classification'->>'category'         AS category,
    d.meta->'importance'->>'tier'                 AS importance_tier,
    (d.meta->'importance'->>'score')::numeric     AS importance_score,
    COALESCE((d.meta->'dedup'->>'is_duplicate')::boolean, false) AS is_duplicate,
    (d.meta->'dedup'->>'duplicate_of')::bigint    AS duplicate_of,
    d.meta->'tags'->'instrument_ids'              AS instrument_ids
FROM docs.documents d
WHERE d.meta->>'preprocessed_at' IS NOT NULL;

-- 重要度別トリアージキュー: 準重複を除外し、mid/high を重要度降順で出す。
-- low=保存のみ / mid=軽量 LLM トリアージ / high=直接 中位分析（設計 §3）。
CREATE OR REPLACE VIEW docs.triage_queue AS
SELECT
    doc_id, source_type, source_name, title, category,
    importance_tier, importance_score, instrument_ids, published_at, as_of
FROM docs.documents_enriched
WHERE is_duplicate = false
  AND importance_tier IN ('mid', 'high')
ORDER BY importance_score DESC, doc_id ASC;

COMMENT ON VIEW docs.preprocess_pending IS
    '階層0前処理が未処理（または旧バージョン）の文書キュー（T-010・設計 §3）。';
COMMENT ON VIEW docs.documents_enriched IS
    '前処理済み文書に meta の重要度・分類・タグを展開したビュー（準重複含む）。';
COMMENT ON VIEW docs.triage_queue IS
    '重要度別トリアージキュー: 準重複除外・mid/high を重要度降順（下流の LLM 振り分け入口）。';
