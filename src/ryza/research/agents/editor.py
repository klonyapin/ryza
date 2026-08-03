"""agents.editor — 統合・矛盾検出・市場観更新案(diff)・朝刊トピック候補。

入力: macro/micro/sentiment の新規出力 + 現在の市場観。出力(scores): 市場観の更新案
(diff 形式)・矛盾フラグ・朝刊向けトピック候補(設計 20-research §4)。

**境界の厳守**: editor が出すのは提案(diff)にすぎない。市場観ステートを実際に変えるのは
``market_view.apply_update`` の決定論ルールだけ(§5)。本モジュールは editor レポートを保存し、
その scores から ``MarketViewDiff`` を組んで ``apply_update`` に渡すところまでを担う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from ryza.provenance import Run, record
from ryza.research.agents.base import (
    build_system_prompt,
    build_user_prompt,
    fenced_json,
    load_current,
    save_report,
)
from ryza.research.llm import StructuredLLM
from ryza.research.market_view import (
    ApplyResult,
    FlashHook,
    MarketViewConfig,
    MarketViewDiff,
    apply_update,
)
from ryza.research.schemas import EDITOR_SCHEMA

AGENT = "editor"
MODEL_TIER = "mid"


@dataclass(frozen=True)
class AgentReport:
    """統合対象の下流レポート(macro/micro/sentiment の 1 本)。"""

    report_id: int
    agent: str
    scores: dict[str, Any]
    doc_ids: list[int]


def load_recent_reports(
    conn: psycopg.Connection,
    *,
    agents: tuple[str, ...] = ("macro", "micro", "sentiment"),
    since: datetime | None = None,
    limit: int = 20,
) -> list[AgentReport]:
    """統合対象の直近レポートを取る(既定は macro/micro/sentiment の最新群)。"""
    params: list[Any] = [list(agents)]
    since_clause = ""
    if since is not None:
        since_clause = "AND as_of >= %s"
        params.append(since)
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT report_id, agent, scores, input_refs
            FROM docs.research_reports
            WHERE agent = ANY(%s) {since_clause}
            ORDER BY report_id DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    reports: list[AgentReport] = []
    for report_id, agent, scores, input_refs in rows:
        doc_ids = [int(x) for x in ((input_refs or {}).get("doc_ids") or [])]
        reports.append(AgentReport(report_id, agent, dict(scores or {}), doc_ids))
    return reports


def analyze(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    reports: list[AgentReport],
    *,
    model: str = "mid-default",
    as_of: datetime | None = None,
) -> int | None:
    """3系のレポートを統合して editor レポート(更新案)を保存し report_id を返す。

    保存後、editor レポート → 各下流レポートのリネージ辺も張る。
    """
    if not reports:
        return None
    view = load_current(conn)
    input_doc_ids = sorted({d for r in reports for d in r.doc_ids})
    task = "統合・矛盾検出: 更新案(regime_changes/key_risk_ops)・矛盾・朝刊候補を構造化出力せよ。"
    prompt = build_user_prompt(
        task=task,
        view=view, docs=[],
        extra={
            # 下流レポートの scores は**過去の LLM 出力**であり、その文字列値は元をたどれば
            # 取込文書の本文に由来する(注入の再持ち込み経路)。エージェント名・report_id は
            # こちらの決定論データなのでフェンス外、scores だけを囲む(審査 C-13)。
            "agent_reports": [
                {
                    "agent": r.agent,
                    "report_id": r.report_id,
                    "scores": fenced_json(
                        r.scores, tag=f"agent_report report_id={r.report_id}"
                    ),
                }
                for r in reports
            ],
        },
    )
    result = llm.complete(
        system=build_system_prompt(AGENT), user=prompt, schema=EDITOR_SCHEMA,
        task_type="analysis.editor", model_tier=MODEL_TIER, model=model,
    )
    # editor の refs は下流レポートが参照した doc_id の総和を既定にする。
    refs = [int(r) for r in (result.content.get("refs") or [])] or input_doc_ids
    report_id = save_report(
        conn, run, agent=AGENT, report_type="daily", scores=result.content,
        input_refs=refs, view_id=view.view_id if view else None, as_of=as_of,
    )
    # リネージ: editor レポートは各下流レポートに依存する。
    record(
        conn, run, [("research_reports", report_id)],
        [("research_reports", r.report_id) for r in reports],
    )
    return report_id


def apply_report(
    conn: psycopg.Connection,
    run: Run,
    report_id: int,
    *,
    config: MarketViewConfig | None = None,
    as_of: datetime | None = None,
    flash_hook: FlashHook | None = None,
) -> ApplyResult:
    """保存済み editor レポートの scores を diff にして決定論ルールで適用する。

    editor(LLM)は提案を出すだけ。ステートを変えるのは ``apply_update`` の決定論ルール。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scores FROM docs.research_reports WHERE report_id = %s AND agent = %s",
            (report_id, AGENT),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"editor レポート {report_id} が見つからない")
    diff = MarketViewDiff.from_editor_scores(dict(row[0] or {}))
    return apply_update(
        conn, run, diff, config=config, as_of=as_of,
        report_id=report_id, flash_hook=flash_hook,
    )


def run_editor(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    *,
    model: str = "mid-default",
    since: datetime | None = None,
    config: MarketViewConfig | None = None,
    as_of: datetime | None = None,
    flash_hook: FlashHook | None = None,
) -> tuple[int | None, ApplyResult | None]:
    """直近レポートの読み込み → editor 統合 → 決定論適用まで一気に回す便宜関数。"""
    as_of = as_of or datetime.now(UTC)
    reports = load_recent_reports(conn, since=since)
    report_id = analyze(conn, run, llm, reports, model=model, as_of=as_of)
    if report_id is None:
        return None, None
    result = apply_report(
        conn, run, report_id, config=config, as_of=as_of, flash_hook=flash_hook
    )
    return report_id, result
