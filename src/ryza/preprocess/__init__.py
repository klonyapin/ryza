"""preprocess — 階層0前処理（非 LLM・限界費用ゼロ）。

到着文書（``docs.documents``）に対する非 LLM 前処理パイプライン（設計 20-research §3）:

- ``lang``: 言語判定（依存ゼロ・文字種）。
- ``embed``: 軽量ローカル埋め込み（差し替え可能。本番は sentence-transformers 小型多言語）。
- ``dedup``: 重複排除（content_hash 完全一致 + 埋め込み近傍の準重複）。
- ``classify``: 開示種別・ニュースカテゴリ分類（辞書・正規表現。学習分類器は器のみ）。
- ``tagger``: 銘柄・エンティティタグ（``market.instruments`` から辞書生成）。
- ``importance``: 一次重要度スコア（``config/importance.yaml``・決定論）。
- ``runner``: 未処理文書の検出 → 一括処理 → ``documents.meta`` 更新・``embeddings`` 書き込み・
  リネージ登録。冪等（``preprocess_version`` マーカー）。

結果と判定根拠は ``docs.documents.meta`` に格納し、重要度別キュー（``docs`` のビュー・
migration 0009）で下流に振り分ける。LLM は一切呼ばない（階層0）。
"""

from __future__ import annotations

from ryza.preprocess.classify import ClassifyResult, RuleClassifier, classify
from ryza.preprocess.dedup import DedupResult, classify_duplicate
from ryza.preprocess.embed import (
    DEFAULT_MODEL,
    STORAGE_DIM,
    Embedder,
    HashingEmbedder,
    SentenceTransformerEmbedder,
    to_storage_vector,
    write_embedding,
)
from ryza.preprocess.importance import ImportanceConfig, ImportanceResult, score_importance
from ryza.preprocess.lang import detect_lang
from ryza.preprocess.runner import (
    PREPROCESS_VERSION,
    DocRow,
    PreprocessOutcome,
    find_unprocessed,
    preprocess_document,
    run_preprocess,
)
from ryza.preprocess.tagger import InstrumentDict, TagResult, build_dictionary, tag

__all__ = [
    "DEFAULT_MODEL",
    "PREPROCESS_VERSION",
    "STORAGE_DIM",
    "ClassifyResult",
    "DedupResult",
    "DocRow",
    "Embedder",
    "HashingEmbedder",
    "ImportanceConfig",
    "ImportanceResult",
    "InstrumentDict",
    "PreprocessOutcome",
    "RuleClassifier",
    "SentenceTransformerEmbedder",
    "TagResult",
    "build_dictionary",
    "classify",
    "classify_duplicate",
    "detect_lang",
    "find_unprocessed",
    "preprocess_document",
    "run_preprocess",
    "score_importance",
    "tag",
    "to_storage_vector",
    "write_embedding",
]
