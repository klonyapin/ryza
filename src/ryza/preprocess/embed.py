"""embed — 軽量ローカル埋め込み（階層0・LLM 非依存）。

設計 20-research §3 ⑥「埋め込み生成（軽量埋め込みモデル）→ docs.documents.meta」。
LLM は呼ばず、ローカルの小型多言語モデル（sentence-transformers 系）で埋め込む。

## モデルと次元の整合

``docs.embeddings.embedding`` は ``vector(1024)`` に固定されている（設計 10-data-accounting §3）。
一方、本体依存に torch を持ち込まないための小型多言語モデル
（``paraphrase-multilingual-MiniLM-L12-v2`` = 384 次元）はネイティブ次元が 1024 未満になる。
そこで **ゼロパディングで 1024 次元に揃えて格納する**。ゼロ埋めはコサイン類似度を厳密に
保存する（内積・ノルムに末尾ゼロは寄与しない）ため、準重複判定（dedup）の近傍関係は
モデルのネイティブ空間と一致する。実モデル名とネイティブ次元は ``documents.meta`` に記録する
（要件: 「モデル名・次元を documents.meta に記録」）。

## テスト容易性

``Embedder`` プロトコルに差し替え可能。本番は ``SentenceTransformerEmbedder``
（``sentence-transformers`` を遅延 import。optional group ``[preprocess]``）。テストは
実モデルをロードせず ``HashingEmbedder``（決定論ダミー）や任意のフェイクを注入する。

DB 書き込みは渡された ``conn`` のトランザクションに参加し、本モジュールは commit しない。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

import psycopg

# docs.embeddings.embedding の固定次元（設計 10-data-accounting §3）。
STORAGE_DIM = 1024

# 既定の小型多言語モデル（ネイティブ 384 次元）。本体依存に torch を持ち込まないため
# optional group [preprocess] に隔離し、実行時に遅延 import する。
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@runtime_checkable
class Embedder(Protocol):
    """埋め込み器の最小インターフェース（差し替え可能）。

    ``embed`` はネイティブ次元のベクトル列を返す（格納次元への調整は ``to_storage_vector``
    が行う）。``model_name`` / ``native_dim`` は meta への記録に使う。
    """

    @property
    def model_name(self) -> str: ...

    @property
    def native_dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def to_storage_vector(vec: list[float], dim: int = STORAGE_DIM) -> list[float]:
    """ネイティブ次元のベクトルを格納次元（既定 1024）に揃える。

    短ければ末尾ゼロパディング（コサイン類似度を厳密に保存）、長ければ切り詰める。
    """
    if len(vec) == dim:
        return list(vec)
    if len(vec) < dim:
        return list(vec) + [0.0] * (dim - len(vec))
    return list(vec[:dim])


def format_vector(vec: list[float]) -> str:
    """pgvector のテキスト表現 ``[v1,v2,...]`` に整形する（psycopg で ::vector にキャスト）。"""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class HashingEmbedder:
    """依存ゼロの決定論的ダミー埋め込み器（テスト・オフライン開発用）。

    テキストのトークンを SHA-256 でバケットに写像した bag-of-hashed-tokens を L2 正規化する。
    実モデルではないが「同じ内容は同じベクトル・似た内容は近いベクトル」という最低限の性質を
    持つため、パイプライン結線と準重複ロジックの検証に使える。
    """

    def __init__(self, dim: int = 64, model_name: str = "hashing-dummy") -> None:
        self._dim = dim
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def native_dim(self) -> int:
        return self._dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in text.lower().split():
            h = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self._dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class SentenceTransformerEmbedder:
    """本番用の軽量ローカル埋め込み器。``sentence-transformers`` を遅延 import する。

    optional group ``[preprocess]`` を入れた環境でのみ実体化できる（未導入なら import 時に
    明示エラー）。初回はモデル重みのダウンロードが発生する（CI 対象外・実測は 1 回）。
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # 遅延 import
        except ImportError as exc:  # pragma: no cover - 依存未導入の環境向け
            raise ImportError(
                "SentenceTransformerEmbedder には optional group [preprocess] が必要です"
                "（uv sync --extra preprocess）。"
            ) from exc
        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        # 次元取得メソッドは新旧で名前が異なる（get_embedding_dimension に改名）。両対応。
        get_dim = getattr(self._model, "get_embedding_dimension", None)
        if get_dim is None:
            get_dim = self._model.get_sentence_embedding_dimension
        self._native_dim = int(get_dim())

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def native_dim(self) -> int:
        return self._native_dim

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - 実モデル
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


def embed_text(embedder: Embedder, text: str) -> list[float]:
    """1 テキストをネイティブ次元で埋め込む（空文字は零ベクトル）。"""
    if not text.strip():
        return [0.0] * embedder.native_dim
    return embedder.embed([text])[0]


def write_embedding(
    conn: psycopg.Connection,
    doc_id: int,
    model: str,
    storage_vec: list[float],
) -> None:
    """``docs.embeddings`` に格納次元ベクトルを upsert する（再処理で更新可能）。

    冪等キーは ``doc_id``。ルール改訂・モデル更新での再処理を許すため
    ``ON CONFLICT DO UPDATE`` にする。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.embeddings (doc_id, model, embedding)
            VALUES (%s, %s, %s::vector)
            ON CONFLICT (doc_id) DO UPDATE
                SET model = EXCLUDED.model, embedding = EXCLUDED.embedding
            """,
            (doc_id, model, format_vector(storage_vec)),
        )
