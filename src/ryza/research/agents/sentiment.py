"""agents.sentiment — センチメント分析エージェント(ニュースの集計的心理)。

入力: 前処理済み文書群の統計 + サンプル本文。出力(scores): センチメントスコア
(資産クラス別・銘柄別)・異常度・根拠 refs(設計 20-research §4)。
"""

from __future__ import annotations

from datetime import datetime

import psycopg

from ryza.provenance import Run
from ryza.research.agents.base import (
    build_system_prompt,
    build_user_prompt,
    fetch_triage_docs,
    load_current,
    load_document_bodies,
    save_report,
)
from ryza.research.llm import StructuredLLM
from ryza.research.schemas import SENTIMENT_SCHEMA

AGENT = "sentiment"
# センチメント担当: ニュース系。
SOURCE_TYPES = ["news", "social"]
MODEL_TIER = "mid"


def analyze(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    *,
    model: str = "mid-default",
    limit: int = 100,
    as_of: datetime | None = None,
) -> int | None:
    """ニュース群のセンチメントを分析し research_report を保存して report_id を返す。"""
    docs = fetch_triage_docs(conn, source_types=SOURCE_TYPES, limit=limit)
    if not docs:
        return None
    # センチメントは集計が主なので本文はサンプルのみ(先頭の数件)を載せる。
    sample_ids = [d.doc_id for d in docs[:10]]
    bodies = load_document_bodies(conn, sample_ids, max_chars=800)
    view = load_current(conn)
    stats = _aggregate_stats(docs)
    prompt = build_user_prompt(
        task="センチメント分析: 資産クラス別・銘柄別のセンチメントと異常度を構造化出力せよ。",
        view=view, docs=docs, bodies=bodies, extra={"stats": stats},
    )
    result = llm.complete(
        system=build_system_prompt(AGENT), user=prompt, schema=SENTIMENT_SCHEMA,
        task_type="analysis.sentiment", model_tier=MODEL_TIER, model=model,
    )
    input_refs = _resolve_refs(result.content, docs)
    return save_report(
        conn, run, agent=AGENT, report_type="daily", scores=result.content,
        input_refs=input_refs, view_id=view.view_id if view else None, as_of=as_of,
    )


def _aggregate_stats(docs) -> dict:
    """文書数・カテゴリ分布などの決定論的集計(LLM への補助情報)。"""
    by_category: dict[str, int] = {}
    for d in docs:
        key = d.category or "unknown"
        by_category[key] = by_category.get(key, 0) + 1
    return {"count": len(docs), "by_category": by_category}


def _resolve_refs(scores: dict, docs) -> list[int]:
    refs = [int(r) for r in (scores.get("refs") or [])]
    return refs or [d.doc_id for d in docs]
