"""morning — 朝刊パイプライン（30-press §2）。

素材 → トピック候補 → 報道価値採点 → 上位≤5 → 執筆 → 文体リンター（再生成≤2）→
embed 組立 → ``press.outbox`` 投入 → ``research_reports`` 保存（素材→記事のリネージ）。

**決定論の境界**: 採否・落板・レート制御は決定論コードが行う。LLM（``writer``）は記事案を出すだけ。
リンター2回失敗のトピックは落として次点を繰り上げ、**失敗原文は研究素材として保存**（§2）。
投入は Bot と共有の ``press.outbox``（``ryza.bot.outbox.enqueue`` を生成側として利用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ryza.bot.outbox import enqueue
from ryza.press import embeds, topics
from ryza.press.config import PressConfig
from ryza.press.images import ImageResult
from ryza.press.linter import LintReport, Topic, lint_topic
from ryza.press.writer import WriteResult, write_topic
from ryza.provenance import Run, record
from ryza.research.llm import StructuredLLM

AGENT = "press"


@dataclass
class TopicOutcome:
    """1 候補の処理結果（採用/落板と理由）。"""

    scored: topics.ScoredCandidate
    accepted: bool
    topic: Topic | None = None
    report_id: int | None = None
    attempts: int = 0
    lint: LintReport | None = None
    reason: str = ""


@dataclass
class MorningResult:
    """朝刊 1 回の結果。"""

    outbox_id: int | None
    accepted: list[TopicOutcome] = field(default_factory=list)
    rejected: list[TopicOutcome] = field(default_factory=list)
    report_ids: list[int] = field(default_factory=list)


def _build_material(sc: topics.ScoredCandidate) -> dict[str, Any]:
    c = sc.candidate
    return {
        "title": c.title,
        "source_kind": c.source_kind,
        "category": c.category,
        "refs": c.refs,
        "detail": c.material,
        "newsworthiness": sc.rationale,
    }


def _write_and_lint(
    llm: StructuredLLM,
    sc: topics.ScoredCandidate,
    cfg: PressConfig,
    *,
    model: str,
) -> tuple[WriteResult, LintReport, int]:
    """執筆→リンター→（不合格なら理由付き再生成 ≤max_regens）。最終結果を返す。"""
    material = _build_material(sc)
    refs = sc.candidate.refs
    valid_ids: set[int] | None = set(refs) if refs else None
    feedback: str | None = None
    wr: WriteResult | None = None
    report: LintReport | None = None
    attempts = 0
    for _ in range(cfg.topics.max_regens + 1):
        attempts += 1
        wr = write_topic(llm, material, model=model, feedback=feedback)
        report = lint_topic(
            wr.topic, mode="morning", valid_source_ids=valid_ids,
            min_chars=cfg.topics.min_chars, max_chars=cfg.topics.max_chars,
            min_sentences=cfg.topics.min_sentences, max_sentences=cfg.topics.max_sentences,
        )
        if report.ok:
            break
        feedback = report.reasons()
    assert wr is not None and report is not None
    return wr, report, attempts


def _save_report(
    conn: psycopg.Connection,
    run: Run,
    sc: topics.ScoredCandidate,
    wr: WriteResult,
    *,
    as_of: datetime,
    report_type: str,
) -> int:
    """朝刊トピックを research_reports に保存し、素材→記事のリネージを張る。

    ``base.save_report`` は input_refs 必須だが、カレンダー由来など refs 空の候補もあるため
    ここでは直接 INSERT する（refs があればリネージ辺も張る）。
    """
    scores = {
        "argument": wr.topic.argument,
        "sentences": [
            {"text": s.text, "level": s.level, "source_ids": s.source_ids}
            for s in wr.topic.sentences
        ],
        "trade_implication": (
            {
                "action": wr.topic.trade_implication.action,
                "target": wr.topic.trade_implication.target,
                "condition": wr.topic.trade_implication.condition,
            }
            if wr.topic.trade_implication
            else None
        ),
        "newsworthiness": sc.rationale,
        "candidate_key": sc.candidate.key,
    }
    refs = sorted(set(sc.candidate.refs))
    input_refs = {"doc_ids": refs, "candidate_key": sc.candidate.key}
    body_md = wr.topic.argument + "\n" + "".join(s.text for s in wr.topic.sentences)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.research_reports
                (agent, report_type, scores, body_md, input_refs, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING report_id
            """,
            (AGENT, report_type, Jsonb(scores), body_md, Jsonb(input_refs), as_of, run.run_id),
        )
        report_id = cur.fetchone()[0]
    if refs:
        record(conn, run, [("research_reports", report_id)], [("documents", d) for d in refs])
    return report_id


def run_morning(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    *,
    cfg: PressConfig | None = None,
    model: str = "mid-default",
    as_of: datetime | None = None,
    image: ImageResult | None = None,
    nav: dict[str, Any] | None = None,
    channel: str = "press",
) -> MorningResult:
    """朝刊を 1 回生成して outbox に投入する。

    - 候補収集 → 採点 → 上位≤5。各トピックを執筆・リンター（再生成≤2）。
    - 合格分で 1 本の朝刊 embed を組み、``press.outbox`` に投入（channel=press・非緊急）。
    - 各トピックを research_reports に保存。**落板トピックの失敗原文も研究素材として保存**（§2）。
    """
    cfg = cfg or PressConfig.load()
    as_of = as_of or datetime.now(UTC)

    candidates = topics.collect_candidates(conn, cfg.topics, as_of=as_of)
    top = topics.select_top(candidates, cfg.topics)

    accepted: list[TopicOutcome] = []
    rejected: list[TopicOutcome] = []
    report_ids: list[int] = []

    for sc in top:
        wr, report, attempts = _write_and_lint(llm, sc, cfg, model=model)
        if report.ok:
            report_id = _save_report(conn, run, sc, wr, as_of=as_of, report_type="morning_press")
            report_ids.append(report_id)
            accepted.append(
                TopicOutcome(scored=sc, accepted=True, topic=wr.topic,
                             report_id=report_id, attempts=attempts, lint=report)
            )
        else:
            # 落板: 失敗原文を研究素材として保存（§2）。
            fail_id = _save_report(
                conn, run, sc, wr, as_of=as_of, report_type="morning_press_rejected"
            )
            rejected.append(
                TopicOutcome(scored=sc, accepted=False, topic=wr.topic,
                             report_id=fail_id, attempts=attempts, lint=report,
                             reason=report.reasons())
            )

    outbox_id: int | None = None
    if accepted:
        embed = embeds.build_morning_embed(
            [o.topic for o in accepted if o.topic is not None],
            image=image, nav=nav, timestamp=as_of.isoformat(),
        )
        outbox_id = enqueue(conn, channel, embed, run.run_id, urgent=False)

    return MorningResult(
        outbox_id=outbox_id, accepted=accepted, rejected=rejected, report_ids=report_ids
    )
