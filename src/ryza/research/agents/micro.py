"""agents.micro — 個別銘柄分析エージェント(決算・開示)。

入力: 銘柄タグ付き文書群 + 当該銘柄の保有/ウォッチ状況。出力(scores): 銘柄別 impact
(-1〜+1)・materiality(0-1)・催化剤種別・根拠 refs(設計 20-research §4)。
"""

from __future__ import annotations

from datetime import datetime

import psycopg

from ryza.provenance import Run
from ryza.research.agents.base import (
    build_user_prompt,
    fetch_triage_docs,
    load_current,
    load_document_bodies,
    load_persona,
    save_report,
)
from ryza.research.llm import StructuredLLM
from ryza.research.schemas import MICRO_SCHEMA

AGENT = "micro"
# ミクロ担当: 適時開示・決算系の filing。
SOURCE_TYPES = ["filing"]
MODEL_TIER = "mid"


def _load_watchlist_ids(conn: psycopg.Connection) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT instrument_id FROM market.watchlist")
        return [r[0] for r in cur.fetchall()]


def analyze(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    *,
    model: str = "mid-default",
    held_ids: list[int] | None = None,
    limit: int = 50,
    as_of: datetime | None = None,
) -> int | None:
    """銘柄タグ付き文書を分析し research_report を保存して report_id を返す。"""
    docs = fetch_triage_docs(conn, source_types=SOURCE_TYPES, limit=limit)
    docs = [d for d in docs if d.instrument_ids]  # 銘柄タグの付いた文書のみ
    if not docs:
        return None
    bodies = load_document_bodies(conn, [d.doc_id for d in docs])
    view = load_current(conn)
    watchlist_ids = _load_watchlist_ids(conn)
    prompt = build_user_prompt(
        task="個別銘柄分析: 銘柄別の impact / materiality / 催化剤種別を構造化出力せよ。",
        view=view, docs=docs, bodies=bodies,
        extra={"held_ids": held_ids or [], "watchlist_ids": watchlist_ids},
    )
    result = llm.complete(
        system=load_persona(AGENT), user=prompt, schema=MICRO_SCHEMA,
        task_type="analysis.micro", model_tier=MODEL_TIER, model=model,
    )
    input_refs = _resolve_refs(result.content, docs)
    return save_report(
        conn, run, agent=AGENT, report_type="daily", scores=result.content,
        input_refs=input_refs, view_id=view.view_id if view else None, as_of=as_of,
    )


def _resolve_refs(scores: dict, docs) -> list[int]:
    refs = [int(r) for r in (scores.get("refs") or [])]
    return refs or [d.doc_id for d in docs]
