"""agents.base — 分析エージェント共通の入出力基盤。

各エージェント(macro/micro/sentiment/editor)は「入力組立 → プロンプト → scores 検証 →
research_reports 保存 + リネージ」の同じ骨格を持つ純関数的ジョブ(設計 20-research §4)。
共通部分をここに集約する:

- ``load_persona``: ``personas/analyst-<name>/system.md`` を読む(プロンプト資産・PR ゲート対象)。
- ``fetch_triage_docs``: ``docs.triage_queue`` から担当カテゴリの文書を取る(担当キュー)。
- ``load_document_bodies``: プロンプトに載せる本文を取る。
- ``current_view_summary``: 現在の市場観を LLM 入力用に要約する。
- ``save_report``: scores 検証 + input_refs 必須チェック + 保存 + リネージ登録。

**input_refs(参照 doc_id)欠落は保存時に拒否**(リネージ・監査 A-13 の前提・§4)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ryza.provenance import Run, record
from ryza.research.market_view import MarketViewState, load_current
from ryza.research.schemas import SCHEMAS, SchemaError, validate

_PERSONA_ROOT = Path(__file__).resolve().parents[4] / "personas"


def load_persona(name: str) -> str:
    """``personas/analyst-<name>/system.md`` を読む。無ければ空文字。"""
    path = _PERSONA_ROOT / f"analyst-{name}" / "system.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class TriageDoc:
    """トリアージキューの 1 文書(プロンプト入力用の最小情報)。"""

    doc_id: int
    source_type: str
    source_name: str
    title: str | None
    category: str | None
    importance_tier: str | None
    importance_score: float | None
    instrument_ids: list[int]


def fetch_triage_docs(
    conn: psycopg.Connection,
    *,
    categories: list[str] | None = None,
    source_types: list[str] | None = None,
    limit: int = 50,
) -> list[TriageDoc]:
    """``docs.triage_queue``(準重複除外・mid/high・重要度降順)から担当文書を取る。

    ``categories`` / ``source_types`` を渡すと該当のみに絞る(担当エージェントのスコープ)。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if categories:
        clauses.append("category = ANY(%s)")
        params.append(categories)
    if source_types:
        clauses.append("source_type = ANY(%s)")
        params.append(source_types)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT doc_id, source_type, source_name, title, category,
                   importance_tier, importance_score, instrument_ids
            FROM docs.triage_queue
            WHERE true{where}
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    docs: list[TriageDoc] = []
    for r in rows:
        instrument_ids = [int(x) for x in (r[7] or [])]
        docs.append(TriageDoc(
            doc_id=r[0], source_type=r[1], source_name=r[2], title=r[3],
            category=r[4], importance_tier=r[5],
            importance_score=float(r[6]) if r[6] is not None else None,
            instrument_ids=instrument_ids,
        ))
    return docs


def load_document_bodies(
    conn: psycopg.Connection, doc_ids: list[int], *, max_chars: int = 2000
) -> dict[int, dict[str, Any]]:
    """doc_id → {title, body(切り詰め), source_name} を返す(プロンプト本文用)。"""
    if not doc_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id, title, body, source_name FROM docs.documents "
            "WHERE doc_id = ANY(%s)",
            (doc_ids,),
        )
        out: dict[int, dict[str, Any]] = {}
        for doc_id, title, body, source_name in cur.fetchall():
            out[doc_id] = {
                "title": title,
                "body": (body or "")[:max_chars],
                "source_name": source_name,
            }
    return out


def current_view_summary(view: MarketViewState | None) -> dict[str, Any]:
    """市場観を LLM 入力用の JSON 要約にする(未初期化なら空)。"""
    if view is None:
        return {"regime": {}, "key_risks": []}
    return {"regime": view.regime, "key_risks": view.key_risks}


def build_user_prompt(
    *,
    task: str,
    view: MarketViewState | None,
    docs: list[TriageDoc],
    bodies: dict[int, dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """担当キュー + 現在の市場観からユーザープロンプト(決定論の文字列)を組む。

    実 LLM でも構造化出力を要求するため、入力は JSON 風に整形して渡す。
    """
    import json

    bodies = bodies or {}
    doc_payload = []
    for d in docs:
        entry: dict[str, Any] = {
            "doc_id": d.doc_id,
            "source": d.source_name,
            "category": d.category,
            "importance": d.importance_tier,
            "instrument_ids": d.instrument_ids,
            "title": d.title,
        }
        if d.doc_id in bodies:
            entry["body"] = bodies[d.doc_id]["body"]
        doc_payload.append(entry)
    payload = {
        "task": task,
        "current_market_view": current_view_summary(view),
        "documents": doc_payload,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def save_report(
    conn: psycopg.Connection,
    run: Run,
    *,
    agent: str,
    report_type: str,
    scores: dict[str, Any],
    input_refs: list[int],
    body_md: str | None = None,
    view_id: int | None = None,
    as_of: datetime | None = None,
    validate_scores: bool = True,
) -> int:
    """scores を検証し ``docs.research_reports`` に保存してリネージを張る。

    - ``validate_scores``: agent 名の JSON Schema で scores を検証(不適合は ``SchemaError``)。
    - **input_refs 欠落は拒否**(空なら ``ValueError``・§4)。参照 doc_id はリネージの前提。
    - リネージ: report → 各 documents、および指定あれば report → market_view(view_id)。
    """
    if validate_scores:
        schema = SCHEMAS.get(agent)
        if schema is not None:
            errors = validate(scores, schema)
            if errors:
                raise SchemaError(errors)
    if not input_refs:
        raise ValueError(
            f"agent '{agent}' の research_report に input_refs が無い(保存拒否・§4)"
        )
    as_of = as_of or datetime.now(UTC)
    refs_json: dict[str, Any] = {"doc_ids": sorted(set(int(r) for r in input_refs))}
    if view_id is not None:
        refs_json["view_id"] = view_id
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.research_reports
                (agent, report_type, scores, body_md, input_refs, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING report_id
            """,
            (agent, report_type, Jsonb(scores), body_md, Jsonb(refs_json), as_of, run.run_id),
        )
        report_id = cur.fetchone()[0]
    # リネージ: レポートは入力文書に依存する。
    record(conn, run, [("research_reports", report_id)],
           [("documents", d) for d in refs_json["doc_ids"]])
    if view_id is not None:
        record(conn, run, [("research_reports", report_id)], [("market_view", view_id)])
    return report_id


__all__ = [
    "TriageDoc",
    "build_user_prompt",
    "current_view_summary",
    "fetch_triage_docs",
    "load_current",
    "load_document_bodies",
    "load_persona",
    "save_report",
]
