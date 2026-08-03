"""runner（前処理パイプライン）の DB 結合テスト — T-010 受け入れ基準。

- フィクスチャ文書（開示・ニュース各種）で分類・タグ・重要度が期待どおり。
- 準重複（同一内容の別ソース記事）が抑制される。
- 埋め込みが embeddings に入り、類似検索が動く（pgvector）。
- 冪等（preprocess_version マーカー）・再処理可能。
"""

from __future__ import annotations

from ryza.preprocess.runner import (
    find_unprocessed,
    load_watchlist_ids,
    preprocess_document,
    run_preprocess,
)


def _fetch_meta(conn, doc_id):
    with conn.cursor() as cur:
        cur.execute("SELECT meta FROM docs.documents WHERE doc_id = %s", (doc_id,))
        return cur.fetchone()[0]


def test_filing_pipeline_classification_tag_importance(
    conn, run, embedder, config, insert_doc, make_instrument
):
    iid = make_instrument("7203.T")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO market.watchlist (instrument_id, added_by) VALUES (%s, 'owner')",
            (iid,),
        )
    doc_id = insert_doc(
        source_type="filing", source_name="TDnet",
        title="通期業績予想の修正に関するお知らせ",
        body="トヨタ自動車（7203）は通期業績予想を上方修正した。",
    )
    outcomes = run_preprocess(conn, run, embedder=embedder, config=config)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.category == "filing_guidance_revision"
    assert iid in o.instrument_ids
    assert o.lang == "ja"
    assert o.importance_tier == "high"  # 0.80 + watchlist 0.20 → 1.0

    meta = _fetch_meta(conn, doc_id)
    assert meta["preprocess_version"] == "1"
    assert meta["classification"]["category"] == "filing_guidance_revision"
    assert meta["tags"]["instrument_ids"] == [iid]
    # 判定根拠が残る（監査 A-13）。
    assert meta["classification"]["rationale"]
    assert any(r["factor"] == "watchlist_instrument" for r in meta["importance"]["reasons"])


def test_low_importance_news_is_low_tier(conn, run, embedder, config, insert_doc):
    insert_doc(source_type="news", source_name="X",
               title="本日の雑感", body="特筆すべき材料はなかった。")
    outcomes = run_preprocess(conn, run, embedder=embedder, config=config)
    assert outcomes[0].category == "unknown"
    assert outcomes[0].importance_tier == "low"


def test_near_duplicate_suppressed(conn, run, embedder, config, insert_doc):
    # 別ソースの同義記事（英語・語彙が近い）→ 後着が準重複として抑制される。
    body_a = "the federal reserve held interest rates steady at the march policy meeting today"
    body_b = "the federal reserve held interest rates steady at the march policy meeting"
    a = insert_doc(source_name="Reuters", source_type="news", body=body_a)
    b = insert_doc(source_name="Nikkei", source_type="news", body=body_b)
    run_preprocess(conn, run, embedder=embedder, config=config, near_threshold=0.15)
    meta_a = _fetch_meta(conn, a)
    meta_b = _fetch_meta(conn, b)
    assert meta_a["dedup"]["is_duplicate"] is False   # 先着は代表
    assert meta_b["dedup"]["is_duplicate"] is True     # 後着が抑制
    assert meta_b["dedup"]["duplicate_of"] == a


def test_embedding_written_and_similarity_search(conn, run, embedder, config, insert_doc):
    doc_id = insert_doc(source_name="X", body="toyota earnings beat expectations")
    run_preprocess(conn, run, embedder=embedder, config=config)
    # embeddings に格納された（格納次元 1024）。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT vector_dims(embedding) FROM docs.embeddings WHERE doc_id = %s",
            (doc_id,),
        )
        assert cur.fetchone()[0] == 1024
    # pgvector コサイン距離での KNN が動く（自分自身が距離 0 で先頭）。
    from ryza.preprocess.embed import embed_text, format_vector, to_storage_vector

    vec = format_vector(
        to_storage_vector(embed_text(embedder, "toyota earnings beat expectations"))
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id FROM docs.embeddings ORDER BY embedding <=> %s::vector LIMIT 1",
            (vec,),
        )
        assert cur.fetchone()[0] == doc_id


def test_idempotent_and_reprocess_on_version_bump(
    conn, run, embedder, config, insert_doc
):
    insert_doc(source_name="X", source_type="news", title="決算発表", body="増益")
    first = run_preprocess(conn, run, embedder=embedder, config=config)
    assert len(first) == 1
    # 同バージョンでは再検出されない（冪等）。
    assert find_unprocessed(conn, version="1") == []
    second = run_preprocess(conn, run, embedder=embedder, config=config)
    assert second == []
    # バージョンを上げると全件が再処理対象になる。
    assert len(find_unprocessed(conn, version="2")) == 1
    third = run_preprocess(conn, run, embedder=embedder, config=config, version="2")
    assert len(third) == 1


def test_triage_queue_view_excludes_low_and_duplicates(
    conn, run, embedder, config, insert_doc, make_instrument
):
    iid = make_instrument("6758.T")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO market.watchlist (instrument_id, added_by) VALUES (%s, 'owner')",
            (iid,),
        )
    high = insert_doc(source_type="filing", source_name="TDnet",
                      title="公開買付け（TOB）の開始について",
                      body="6758 を対象とする公開買付け。")
    low = insert_doc(source_type="news", source_name="X", title="雑感", body="特になし")
    run_preprocess(conn, run, embedder=embedder, config=config)
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id FROM docs.triage_queue")
        queued = {r[0] for r in cur.fetchall()}
    assert high in queued
    assert low not in queued


def test_load_watchlist_ids(conn, make_instrument):
    iid = make_instrument("9999.T")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO market.watchlist (instrument_id, added_by) VALUES (%s, 'FM')",
            (iid,),
        )
    assert iid in load_watchlist_ids(conn)


def test_preprocess_document_records_lineage(
    conn, run, embedder, config, insert_doc
):
    doc_id = insert_doc(source_name="X", body="single doc lineage check")
    from ryza.preprocess.tagger import build_dictionary

    docs = find_unprocessed(conn)
    doc = next(d for d in docs if d.doc_id == doc_id)
    preprocess_document(
        conn, run, doc, embedder=embedder, config=config,
        dictionary=build_dictionary(conn),
    )
    # embeddings → documents のリネージ辺が張られる。
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM meta.lineage_edges
            WHERE from_kind = 'embeddings' AND from_id = %s
              AND to_kind = 'documents' AND to_id = %s
            """,
            (str(doc_id), str(doc_id)),
        )
        assert cur.fetchone() is not None
