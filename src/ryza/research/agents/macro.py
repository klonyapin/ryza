"""agents.macro — マクロ分析エージェント(金利・為替・マクロ指標・中銀)。

入力: 新着マクロ文書群 + 現在の市場観。出力(scores): regime 提案(資産クラス別)・
金利/為替バイアス(-1〜+1)・根拠 refs(設計 20-research §4)。

モデル階層は中位(本分析)。LLM 呼び出しは ``StructuredLLM``(構造化出力・コスト記録)経由。
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
from ryza.research.schemas import MACRO_SCHEMA

AGENT = "macro"
# マクロ担当のスコープ: 中銀・金融政策・為替系のニュース + 政策/官公庁ソース。
CATEGORIES = ["news_monetary_policy", "news_fx"]
SOURCE_TYPES = ["gov", "policy", "news"]
MODEL_TIER = "mid"


def analyze(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    *,
    model: str = "mid-default",
    limit: int = 50,
    as_of: datetime | None = None,
) -> int | None:
    """担当キューを分析し research_report を保存して report_id を返す(対象なしは None)。"""
    docs = fetch_triage_docs(
        conn, categories=CATEGORIES, source_types=SOURCE_TYPES, limit=limit
    )
    if not docs:
        return None
    bodies = load_document_bodies(conn, [d.doc_id for d in docs])
    view = load_current(conn)
    prompt = build_user_prompt(
        task="マクロ分析: 資産クラス別 regime 提案と金利/為替バイアスを構造化出力せよ。",
        view=view, docs=docs, bodies=bodies,
    )
    result = llm.complete(
        system=load_persona(AGENT), user=prompt, schema=MACRO_SCHEMA,
        task_type="analysis.macro", model_tier=MODEL_TIER, model=model,
    )
    input_refs = _resolve_refs(result.content, docs)
    return save_report(
        conn, run, agent=AGENT, report_type="daily", scores=result.content,
        input_refs=input_refs, view_id=view.view_id if view else None, as_of=as_of,
    )


def _resolve_refs(scores: dict, docs) -> list[int]:
    """scores.refs があればそれを、無ければ入力文書 doc_id を input_refs にする。"""
    refs = [int(r) for r in (scores.get("refs") or [])]
    return refs or [d.doc_id for d in docs]
