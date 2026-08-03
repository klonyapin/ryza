"""runner — 階層0前処理パイプラインのオーケストレータ。

設計 20-research §3。未処理文書を検出し、1 件ずつ非 LLM 前処理を通して結果を
``docs.documents.meta`` と ``docs.embeddings`` に格納する:

    言語判定 → 埋め込み → 重複排除 → 分類 → 銘柄タグ → 一次重要度

**冪等**: ``meta.preprocessed_at`` + ``meta.preprocess_version`` を処理済みマーカーにする。
バージョンが現行と異なる文書だけを再処理対象にするため、ルール改訂時は
``PREPROCESS_VERSION`` を上げれば全件再処理できる。

**リネージ**: 各文書の埋め込み（``embeddings`` → ``documents``）と、準重複が見つかった場合の
文書間の辺（``documents`` → 代表 ``documents``）を ``meta.lineage_edges`` に登録する。

**Run 経由**: 呼び出し側が ``Run`` を渡す（``run_id`` を meta 更新のリネージに刻む）。DB 書き込みは
渡された ``conn`` のトランザクションに参加し、本モジュールは commit しない。CLI（``main``）だけは
自前の Run と接続を開いて commit する。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ryza.db.conn import connect
from ryza.preprocess import classify as classify_mod
from ryza.preprocess import dedup as dedup_mod
from ryza.preprocess import tagger as tagger_mod
from ryza.preprocess.embed import (
    Embedder,
    HashingEmbedder,
    embed_text,
    to_storage_vector,
    write_embedding,
)
from ryza.preprocess.importance import ImportanceConfig, score_importance
from ryza.preprocess.lang import detect_lang
from ryza.provenance import Run, record, start_run

# 前処理ルール束のバージョン。ルール・モデルの改訂時に上げると全件が再処理対象になる。
PREPROCESS_VERSION = "1"


@dataclass(frozen=True)
class DocRow:
    """前処理対象の文書（``docs.documents`` の必要列のみ）。"""

    doc_id: int
    source_type: str
    source_name: str
    title: str | None
    body: str | None
    content_hash: bytes


@dataclass(frozen=True)
class PreprocessOutcome:
    """1 文書の前処理結果（テスト・集計用の要約。詳細は meta に格納済み）。"""

    doc_id: int
    lang: str
    category: str
    instrument_ids: list[int]
    importance_tier: str
    importance_score: float
    is_duplicate: bool
    duplicate_of: int | None


def find_unprocessed(
    conn: psycopg.Connection,
    *,
    version: str = PREPROCESS_VERSION,
    limit: int = 500,
) -> list[DocRow]:
    """現行バージョンで未処理の文書を若い順に返す。

    ``meta.preprocess_version`` が現行と異なる（未処理 = NULL を含む）文書が対象。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_id, source_type, source_name, title, body, content_hash
            FROM docs.documents
            WHERE meta->>'preprocess_version' IS DISTINCT FROM %s
            ORDER BY doc_id ASC
            LIMIT %s
            """,
            (version, limit),
        )
        rows = cur.fetchall()
    return [
        DocRow(
            doc_id=r[0], source_type=r[1], source_name=r[2],
            title=r[3], body=r[4], content_hash=bytes(r[5]),
        )
        for r in rows
    ]


def load_watchlist_ids(conn: psycopg.Connection) -> set[int]:
    """``market.watchlist`` のウォッチ銘柄 ID 集合（重要度判定が参照）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT instrument_id FROM market.watchlist")
        return {row[0] for row in cur.fetchall()}


def preprocess_document(
    conn: psycopg.Connection,
    run: Run,
    doc: DocRow,
    *,
    embedder: Embedder,
    config: ImportanceConfig,
    dictionary: tagger_mod.InstrumentDict,
    classifier: classify_mod.Classifier | None = None,
    watchlist_ids: set[int] | None = None,
    held_ids: set[int] | None = None,
    anomaly_ids: set[int] | None = None,
    near_threshold: float = dedup_mod.DEFAULT_NEAR_THRESHOLD,
    version: str = PREPROCESS_VERSION,
) -> PreprocessOutcome:
    """1 文書を前処理し、meta 更新・埋め込み書き込み・リネージ登録まで行う。

    ``held_ids`` は保有銘柄（将来 trade/ledger のポジションから供給。現状は呼び出し側が渡す。
    未指定は空）。``anomaly_ids`` は統計的異常が観測された銘柄集合（上流が判定して渡す）。
    """
    classifier = classifier or classify_mod.RuleClassifier()
    watchlist_ids = watchlist_ids or set()
    held_ids = held_ids or set()
    anomaly_ids = anomaly_ids or set()

    text = f"{doc.title or ''}\n{doc.body or ''}".strip()

    # ② 言語判定
    lang = detect_lang(text)

    # ⑥ 埋め込み（ネイティブ次元 → 格納次元へパディング）→ docs.embeddings
    native_vec = embed_text(embedder, text)
    storage_vec = to_storage_vector(native_vec)
    write_embedding(conn, doc.doc_id, embedder.model_name, storage_vec)

    # ① 重複排除（content_hash 完全一致 + 埋め込み近傍の準重複）
    dedup = dedup_mod.classify_duplicate(
        conn,
        doc_id=doc.doc_id,
        content_hash=doc.content_hash,
        storage_vec=storage_vec,
        threshold=near_threshold,
    )

    # ③ 分類
    classification = classifier.classify(doc.title, doc.body, doc.source_type)

    # ④ 銘柄タグ
    tags = tagger_mod.tag(text, dictionary)

    # ⑤ 一次重要度
    anomaly = bool(set(tags.instrument_ids) & anomaly_ids)
    importance = score_importance(
        config,
        category=classification.category,
        instrument_ids=tags.instrument_ids,
        held_ids=held_ids,
        watchlist_ids=watchlist_ids,
        statistical_anomaly=anomaly,
    )

    # meta パッチを構築（判定根拠込み・監査 A-13 対象）
    patch: dict[str, Any] = {
        "preprocessed_at": datetime.now(UTC).isoformat(),
        "preprocess_version": version,
        "lang": lang,
        "embedding": {
            "model": embedder.model_name,
            "native_dim": embedder.native_dim,
            "storage_dim": len(storage_vec),
        },
        "dedup": {
            "is_duplicate": dedup.is_duplicate,
            "kind": dedup.kind,
            "duplicate_of": dedup.duplicate_of,
            "distance": dedup.distance,
            "threshold": near_threshold,
        },
        "classification": {
            "category": classification.category,
            "label": classification.label,
            "labels": classification.labels,
            "rationale": classification.rationale,
        },
        "tags": {
            "instrument_ids": tags.instrument_ids,
            "matched": tags.matched,
        },
        "importance": {
            "score": importance.score,
            "tier": importance.tier,
            "reasons": importance.reasons,
        },
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE docs.documents
            SET meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb
            WHERE doc_id = %s
            """,
            (Jsonb(patch), doc.doc_id),
        )

    # リネージ: 埋め込みは文書から生成された。準重複は代表文書に依存する。
    record(conn, run, [("embeddings", doc.doc_id)], [("documents", doc.doc_id)])
    if dedup.duplicate_of is not None:
        record(conn, run, [("documents", doc.doc_id)],
               [("documents", dedup.duplicate_of)])

    return PreprocessOutcome(
        doc_id=doc.doc_id,
        lang=lang,
        category=classification.category,
        instrument_ids=tags.instrument_ids,
        importance_tier=importance.tier,
        importance_score=importance.score,
        is_duplicate=dedup.is_duplicate,
        duplicate_of=dedup.duplicate_of,
    )


def run_preprocess(
    conn: psycopg.Connection,
    run: Run,
    *,
    embedder: Embedder,
    config: ImportanceConfig | None = None,
    name_map: dict[str, str] | None = None,
    held_ids: set[int] | None = None,
    anomaly_ids: set[int] | None = None,
    near_threshold: float = dedup_mod.DEFAULT_NEAR_THRESHOLD,
    limit: int = 500,
    version: str = PREPROCESS_VERSION,
) -> list[PreprocessOutcome]:
    """未処理文書を一括前処理する。処理した文書の結果一覧を返す。

    辞書・ウォッチリストはバッチ開始時に 1 度だけ読む。文書は若い順に処理するため、準重複は
    「先に取り込まれた側」が代表になる。
    """
    config = config or ImportanceConfig.load()
    dictionary = tagger_mod.build_dictionary(conn, name_map=name_map)
    watchlist_ids = load_watchlist_ids(conn)
    classifier = classify_mod.RuleClassifier()

    docs = find_unprocessed(conn, version=version, limit=limit)
    outcomes: list[PreprocessOutcome] = []
    for doc in docs:
        outcomes.append(
            preprocess_document(
                conn, run, doc,
                embedder=embedder,
                config=config,
                dictionary=dictionary,
                classifier=classifier,
                watchlist_ids=watchlist_ids,
                held_ids=held_ids,
                anomaly_ids=anomaly_ids,
                near_threshold=near_threshold,
                version=version,
            )
        )
    return outcomes


def main() -> int:  # pragma: no cover - CLI 実行パス
    """CLI: 未処理文書を前処理する。``uv run python -m ryza.preprocess.runner``

    既定の埋め込み器は実モデル（``SentenceTransformerEmbedder``）。``[preprocess]`` 未導入なら
    依存不足の明示エラーになる。開発・オフラインで実モデルを避けたい場合は本関数を使わず
    ``run_preprocess`` に ``HashingEmbedder`` を渡すこと。
    """
    from ryza.preprocess.embed import SentenceTransformerEmbedder

    try:
        embedder: Embedder = SentenceTransformerEmbedder()
    except ImportError as exc:
        print(f"埋め込みモデルを初期化できません: {exc}", file=sys.stderr)
        print("開発用にダミー埋め込みで続行します（HashingEmbedder）。", file=sys.stderr)
        embedder = HashingEmbedder()

    # start_run は自前の autocommit 接続で running 行を即時永続化する（作業用 conn とは別）。
    run = start_run("preprocess.hier0", {"version": PREPROCESS_VERSION})
    conn = connect()
    try:
        outcomes = run_preprocess(conn, run, embedder=embedder)
        conn.commit()
        run.finish("success")
    except Exception:
        conn.rollback()
        run.finish("failed")
        raise
    finally:
        conn.close()
    print(f"前処理 {len(outcomes)} 件完了", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
