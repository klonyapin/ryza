"""dedup（重複排除）の DB テスト。"""

from __future__ import annotations

import hashlib

from ryza.preprocess.dedup import (
    classify_duplicate,
    find_exact_duplicate,
    find_near_duplicate,
)
from ryza.preprocess.embed import embed_text, to_storage_vector, write_embedding


def test_exact_duplicate_across_sources(conn, insert_doc):
    # 別ソースが同一本文を配信 → content_hash 一致で横断検出（先着が代表）。
    digest = hashlib.sha256("同一の通信社記事本文".encode()).digest()
    older = insert_doc(source_name="Reuters", body="同一の通信社記事本文", content_hash=digest)
    newer = insert_doc(source_name="日経", body="同一の通信社記事本文", content_hash=digest)
    assert find_exact_duplicate(conn, digest, exclude_doc_id=newer) == older
    # 先着（older）から見ると自分より若い先着はいない。
    assert find_exact_duplicate(conn, digest, exclude_doc_id=older) is None


def test_near_duplicate_by_embedding(conn, insert_doc, embedder):
    text_a = "the fed held interest rates steady at the march meeting"
    text_b = "the fed held interest rates steady at the meeting"  # ほぼ同義
    a = insert_doc(source_name="A", body=text_a)
    b = insert_doc(source_name="B", body=text_b)
    for doc_id, text in [(a, text_a), (b, text_b)]:
        write_embedding(conn, doc_id, embedder.model_name,
                        to_storage_vector(embed_text(embedder, text)))
    hit = find_near_duplicate(conn, to_storage_vector(embed_text(embedder, text_b)),
                              exclude_doc_id=b, threshold=0.5)
    assert hit is not None
    assert hit[0] == a


def test_no_false_positive_for_distinct_docs(conn, insert_doc, embedder):
    text_a = "toyota raised its full year earnings guidance sharply"
    text_b = "the bank of japan kept monetary policy unchanged"
    a = insert_doc(source_name="A", body=text_a)
    b = insert_doc(source_name="B", body=text_b)
    for doc_id, text in [(a, text_a), (b, text_b)]:
        write_embedding(conn, doc_id, embedder.model_name,
                        to_storage_vector(embed_text(embedder, text)))
    # 厳しめ閾値なら別内容は準重複にならない。
    result = classify_duplicate(
        conn, doc_id=b, content_hash=hashlib.sha256(text_b.encode()).digest(),
        storage_vec=to_storage_vector(embed_text(embedder, text_b)), threshold=0.08,
    )
    assert result.is_duplicate is False


def test_classify_duplicate_prefers_exact(conn, insert_doc, embedder):
    digest = hashlib.sha256("転載記事".encode()).digest()
    older = insert_doc(source_name="A", body="転載記事", content_hash=digest)
    newer = insert_doc(source_name="B", body="転載記事", content_hash=digest)
    write_embedding(conn, older, embedder.model_name,
                    to_storage_vector(embed_text(embedder, "転載記事")))
    result = classify_duplicate(
        conn, doc_id=newer, content_hash=digest,
        storage_vec=to_storage_vector(embed_text(embedder, "転載記事")),
    )
    assert result.is_duplicate is True
    assert result.kind == "exact"
    assert result.duplicate_of == older
    assert result.distance == 0.0
