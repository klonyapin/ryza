"""dedup — 重複排除（階層0）。

設計 20-research §3 ①「重複排除（content_hash 完全一致 + 埋め込み近傍の準重複）」。

2 段構え:

1. **完全一致**: ``docs.documents.content_hash`` の一致。取込側は
   ``UNIQUE(source_name, content_hash)`` で同一ソース内の重複を弾くが、**別ソースが同一
   本文を配信した場合（例: 同じ通信社記事を複数媒体が転載）** はソース名が違うため取込では
   残る。ここで content_hash 一致を横断的に検出する。
2. **準重複**: 埋め込みのコサイン近傍。文面が異なるが実質同一内容（言い換え・要約）を、
   ``docs.embeddings`` に対する pgvector コサイン距離 ``<=>`` の近傍探索で検出する。閾値は
   config（既定 ``DEFAULT_NEAR_THRESHOLD``）。

判定は「抑制フラグ + 代表 doc_id + 距離」を返すだけで、行は消さない（追記オンリー原則）。
下流のキュー（重要度ビュー）が ``is_near_duplicate`` を見て準重複を除外する。

DB 書き込みはしない（読み取りのみ）。渡された ``conn`` を使う。
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ryza.preprocess.embed import format_vector

# 準重複とみなすコサイン距離の既定閾値（距離 = 1 - コサイン類似度）。
# 0.08 ≒ 類似度 0.92 以上を準重複とする。誤抑制を避けるため保守的に近い側だけを対象にする。
DEFAULT_NEAR_THRESHOLD = 0.08


@dataclass(frozen=True)
class DedupResult:
    """重複判定の結果。

    - ``is_duplicate``: 完全一致 or 準重複のいずれかで抑制対象。
    - ``kind``: ``'exact'`` | ``'near'`` | ``None``。
    - ``duplicate_of``: 代表（先に取り込まれた）文書の doc_id。
    - ``distance``: 準重複時のコサイン距離（完全一致は 0.0）。
    """

    is_duplicate: bool
    kind: str | None
    duplicate_of: int | None
    distance: float | None


def find_exact_duplicate(
    conn: psycopg.Connection,
    content_hash: bytes,
    *,
    exclude_doc_id: int,
) -> int | None:
    """同一 ``content_hash`` を持つ**より若い**（先に取り込まれた）文書の doc_id を返す。

    自分自身（``exclude_doc_id``）は除外し、doc_id が小さい＝先着を代表とする。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_id FROM docs.documents
            WHERE content_hash = %s AND doc_id <> %s AND doc_id < %s
            ORDER BY doc_id ASC LIMIT 1
            """,
            (content_hash, exclude_doc_id, exclude_doc_id),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def find_near_duplicate(
    conn: psycopg.Connection,
    storage_vec: list[float],
    *,
    exclude_doc_id: int,
    threshold: float = DEFAULT_NEAR_THRESHOLD,
) -> tuple[int, float] | None:
    """埋め込みコサイン近傍で準重複を探す。``(doc_id, distance)`` か ``None``。

    ``docs.embeddings`` の既存ベクトルに対し ``<=>``（コサイン距離）で最近傍を引き、
    距離が ``threshold`` 以下かつ自分より若い（先着）文書があれば準重複とする。
    """
    vec = format_vector(storage_vec)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.doc_id, (e.embedding <=> %s::vector) AS distance
            FROM docs.embeddings e
            WHERE e.doc_id <> %s AND e.doc_id < %s
            ORDER BY e.embedding <=> %s::vector ASC
            LIMIT 1
            """,
            (vec, exclude_doc_id, exclude_doc_id, vec),
        )
        row = cur.fetchone()
    if row is None:
        return None
    doc_id, distance = row[0], float(row[1])
    if distance <= threshold:
        return doc_id, distance
    return None


def classify_duplicate(
    conn: psycopg.Connection,
    *,
    doc_id: int,
    content_hash: bytes,
    storage_vec: list[float],
    threshold: float = DEFAULT_NEAR_THRESHOLD,
) -> DedupResult:
    """完全一致 → 準重複の順で判定し ``DedupResult`` を返す。

    完全一致が優先（距離 0）。無ければ埋め込み近傍で準重複を判定する。
    """
    exact = find_exact_duplicate(conn, content_hash, exclude_doc_id=doc_id)
    if exact is not None:
        return DedupResult(True, "exact", exact, 0.0)
    near = find_near_duplicate(conn, storage_vec, exclude_doc_id=doc_id, threshold=threshold)
    if near is not None:
        return DedupResult(True, "near", near[0], near[1])
    return DedupResult(False, None, None, None)
