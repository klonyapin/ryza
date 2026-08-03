"""embed（埋め込み）の単体・DB テスト。実モデルはロードしない（HashingEmbedder）。"""

from __future__ import annotations

import math

from ryza.preprocess.embed import (
    STORAGE_DIM,
    HashingEmbedder,
    embed_text,
    format_vector,
    to_storage_vector,
    write_embedding,
)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def test_to_storage_vector_pads_with_zeros():
    v = [0.1, 0.2, 0.3]
    out = to_storage_vector(v)
    assert len(out) == STORAGE_DIM
    assert out[:3] == v
    assert set(out[3:]) == {0.0}


def test_to_storage_vector_truncates_when_longer():
    v = [1.0] * (STORAGE_DIM + 10)
    assert len(to_storage_vector(v)) == STORAGE_DIM


def test_zero_padding_preserves_cosine():
    # ゼロパディングはコサイン類似度を保存する（dedup の根拠）。
    a = [1.0, 2.0, 3.0, -1.0]
    b = [1.0, 2.0, 2.5, -0.5]
    native = _cosine(a, b)
    padded = _cosine(to_storage_vector(a), to_storage_vector(b))
    assert math.isclose(native, padded, rel_tol=1e-9)


def test_hashing_embedder_deterministic_and_normalized():
    emb = HashingEmbedder(dim=64)
    v1 = embed_text(emb, "toyota guidance revision")
    v2 = embed_text(emb, "toyota guidance revision")
    assert v1 == v2  # 決定論
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0, rel_tol=1e-9)
    assert emb.native_dim == 64
    assert emb.model_name == "hashing-dummy"


def test_hashing_embedder_similar_text_closer():
    emb = HashingEmbedder(dim=128)
    base = embed_text(emb, "the fed held interest rates steady today")
    similar = embed_text(emb, "the fed held interest rates steady")
    different = embed_text(emb, "toyota earnings beat expectations sharply")
    assert _cosine(base, similar) > _cosine(base, different)


def test_empty_text_is_zero_vector():
    emb = HashingEmbedder(dim=32)
    assert embed_text(emb, "   ") == [0.0] * 32


def test_format_vector():
    assert format_vector([1.0, 2.5]) == "[1.0,2.5]"


def test_write_embedding_and_upsert(conn, insert_doc, embedder):
    doc_id = insert_doc(title="トヨタ 決算短信", body="通期業績は増益")
    vec = to_storage_vector(embed_text(embedder, "トヨタ 決算短信"))
    write_embedding(conn, doc_id, embedder.model_name, vec)
    with conn.cursor() as cur:
        cur.execute("SELECT model FROM docs.embeddings WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()[0] == embedder.model_name
    # 再処理で upsert される（例外にならない）。
    write_embedding(conn, doc_id, "other-model", vec)
    with conn.cursor() as cur:
        cur.execute("SELECT model FROM docs.embeddings WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()[0] == "other-model"
