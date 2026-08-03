"""flash — 速報エンジン（30-press §3）。

トリガ（market_view の magnitude 閾値イベント = ``docs.flash_triggers`` / 階層0 異常 / ルール
マッチ）→ 軽量 LLM の一次判定（報道価値 0-100）→ 中位モデルが速報テンプレで執筆 → 短縮リンター →
``press.outbox``（緊急フラグ）。予兆速報（速報②）は ``press.predictions`` に確度・検証期限つきで
登録し、**期限到来で的中判定**する（外れの隠蔽を構造的に防ぐ・§3）。

**決定論の境界**: 採否閾値・レート上限（3本/時・12本/日）・まとめ速報への統合・的中判定は
すべて決定論コードが行う。LLM は報道価値スコアと記事案を出すだけ。

**データ境界**（reminders ``press-material-fence``）: トリガの ``summary`` / ``source`` は
外部由来になり得る（editor が書いた市場観の変化の記述、異常検知・ルール側の文字列）。
一次判定・執筆とも、これらは ``writer`` と同じフェンスの内側にだけ載せる。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ryza.bot.outbox import enqueue
from ryza.press import embeds
from ryza.press.config import PressConfig
from ryza.press.images import ImageResult
from ryza.press.linter import Topic, lint_topic
from ryza.press.writer import FENCE_NOTICE, WriteResult, write_flash
from ryza.provenance import Run, record
from ryza.research.llm import StructuredLLM
from ryza.research.prompting import fenced_json

AGENT = "press"

# 一次判定（軽量 LLM）の出力スキーマ: 報道価値 0-100 と種別。
TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["newsworthiness", "kind"],
    "additionalProperties": True,
    "properties": {
        "newsworthiness": {"type": "number", "minimum": 0, "maximum": 100},
        "kind": {"type": "string", "enum": ["fact", "prediction"]},
        "reason": {"type": "string"},
    },
}


# ── トリガ ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FlashTrigger:
    """速報トリガ 1 件。``source`` は market_view|anomaly|rule。"""

    key: str
    source: str
    summary: str
    refs: list[int]
    magnitude: float
    payload: dict[str, Any] = field(default_factory=dict)


def collect_triggers(
    conn: psycopg.Connection, *, since: datetime, limit: int = 50
) -> list[FlashTrigger]:
    """``docs.flash_triggers``（T-011）から未処理トリガを取る（point-in-time）。

    既に速報化済み（press flash レポートの scores.trigger_key に載る）トリガは除外する。
    """
    processed = _processed_trigger_keys(conn, since)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trigger_id, view_id, magnitude, reason, as_of
            FROM docs.flash_triggers
            WHERE as_of >= %s
            ORDER BY trigger_id
            LIMIT %s
            """,
            (since, limit),
        )
        rows = cur.fetchall()
    out: list[FlashTrigger] = []
    for trigger_id, view_id, magnitude, reason, _as_of in rows:
        key = f"mv:{trigger_id}"
        if key in processed:
            continue
        reason = reason or {}
        applied = reason.get("applied", []) if isinstance(reason, dict) else []
        refs: list[int] = []
        for ch in applied:
            refs += [int(x) for x in (ch.get("detail", {}).get("refs") or [])]
        out.append(
            FlashTrigger(
                key=key,
                source="market_view",
                summary=f"市場観の急変（view {view_id}, magnitude {float(magnitude):.2f}）",
                refs=sorted(set(refs)),
                magnitude=float(magnitude),
                payload={"view_id": view_id, "reason": reason},
            )
        )
    return out


def _processed_trigger_keys(conn: psycopg.Connection, since: datetime) -> set[int | str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT scores FROM docs.research_reports
            WHERE agent = 'press' AND report_type IN ('flash', 'flash_skipped')
              AND as_of >= %s
            """,
            (since,),
        )
        keys: set[int | str] = set()
        for (scores,) in cur.fetchall():
            k = (scores or {}).get("trigger_key")
            if k is not None:
                keys.add(k)
    return keys


# ── レート制御（純関数）────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EmissionPlan:
    """発行計画: 個別に出す本数と、まとめ速報へ統合する本数。"""

    individual: int
    digest: int  # >0 なら 1 本の「まとめ速報」に統合される

    @property
    def total_posts(self) -> int:
        return self.individual + (1 if self.digest > 0 else 0)


def plan_emissions(
    ready: int, *, recent_hour: int, recent_day: int, per_hour: int, per_day: int
) -> EmissionPlan:
    """準備できた速報 ``ready`` 本を、上限内は個別・超過分はまとめ速報に振り分ける（§3）。"""
    remaining = max(0, min(per_hour - recent_hour, per_day - recent_day))
    individual = max(0, min(ready, remaining))
    digest = ready - individual
    return EmissionPlan(individual=individual, digest=digest)


def _recent_urgent_counts(
    conn: psycopg.Connection, channel: str, now: datetime
) -> tuple[int, int]:
    """直近1時間・24時間に投入した緊急（速報）outbox 件数を返す。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE created_at >= %s),
              COUNT(*) FILTER (WHERE created_at >= %s)
            FROM press.outbox
            WHERE channel = %s AND urgent = true
            """,
            (now - timedelta(hours=1), now - timedelta(hours=24), channel),
        )
        h, d = cur.fetchone()
    return int(h), int(d)


# ── 執筆・保存 ─────────────────────────────────────────────────────────────────
@dataclass
class FlashOutcome:
    """1 トリガの処理結果。"""

    trigger: FlashTrigger
    published: bool
    is_prediction: bool = False
    newsworthiness: float = 0.0
    outbox_id: int | None = None
    report_id: int | None = None
    prediction_id: int | None = None
    topic: Topic | None = None
    reason: str = ""


@dataclass
class FlashResult:
    outcomes: list[FlashOutcome] = field(default_factory=list)
    digest_outbox_id: int | None = None


def _triage(
    llm: StructuredLLM, trigger: FlashTrigger, *, model: str
) -> tuple[float, str]:
    """軽量 LLM の一次判定（報道価値 0-100・種別）。

    ``summary`` / ``source`` は外部由来になり得る（market_view のトリガは editor が書いた
    変化の記述を、anomaly/rule 由来のトリガは検知側の文字列を運ぶ）ためフェンスに入れる
    （データ境界・reminders ``press-material-fence``）。``magnitude`` / ``refs`` は
    こちらの決定論データ（数値・整数）なので外に置く。閾値判定は決定論コード側にあり、
    ここでの注入は「報道価値を 100 と答えさせる」形で効くため、軽量モデルでも境界は要る。
    """
    import json

    prompt = json.dumps(
        {"task": "この速報トリガの報道価値を 0-100 で採点し種別(fact/prediction)を返せ。",
         "trigger": fenced_json(
             {"summary": trigger.summary, "source": trigger.source},
             tag="flash_trigger",
         ),
         "magnitude": trigger.magnitude,
         "refs": trigger.refs},
        ensure_ascii=False,
    )
    result = llm.complete(
        system=(
            "あなたは報道部の一次トリアージ。事実か予兆かを見分け、報道価値を数値化する。"
            "\n\n" + FENCE_NOTICE
        ),
        user=prompt, schema=TRIAGE_SCHEMA,
        task_type="press.flash.triage", model_tier="light", model=model,
    )
    return float(result.content["newsworthiness"]), str(result.content["kind"])


def _save_flash_report(
    conn: psycopg.Connection,
    run: Run,
    trigger: FlashTrigger,
    topic: Topic | None,
    *,
    as_of: datetime,
    report_type: str,
    newsworthiness: float,
    is_prediction: bool,
) -> int:
    scores: dict[str, Any] = {
        "trigger_key": trigger.key,
        "trigger_source": trigger.source,
        "newsworthiness": newsworthiness,
        "is_prediction": is_prediction,
    }
    body_md = ""
    if topic is not None:
        scores["argument"] = topic.argument
        scores["sentences"] = [
            {"text": s.text, "level": s.level, "source_ids": s.source_ids}
            for s in topic.sentences
        ]
        body_md = topic.argument + "\n" + "".join(s.text for s in topic.sentences)
    input_refs = {"doc_ids": sorted(set(trigger.refs)), "trigger_key": trigger.key}
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
    if trigger.refs:
        record(conn, run, [("research_reports", report_id)],
               [("documents", d) for d in sorted(set(trigger.refs))])
    return report_id


def _register_prediction(
    conn: psycopg.Connection, report_id: int, topic: Topic, *, cfg: PressConfig, as_of: datetime
) -> int:
    """速報②を press.predictions に登録する（§3・確度・検証期限つき）。"""
    pr = topic.prediction
    claim = pr.claim if pr else topic.argument
    confidence = pr.confidence if pr else 0.5
    verify_by = _parse_verify_by(pr.verify_by if pr else "", as_of, cfg.flash.default_verify_hours)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO press.predictions (report_id, claim, confidence, verify_by)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (report_id, claim, confidence, verify_by),
        )
        return cur.fetchone()[0]


def _parse_verify_by(raw: str, as_of: datetime, default_hours: int) -> datetime:
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return as_of + timedelta(hours=default_hours)


def _write_flash_topic(
    llm: StructuredLLM, trigger: FlashTrigger, *, is_prediction: bool, cfg: PressConfig, model: str
) -> tuple[WriteResult, bool]:
    """速報を執筆し短縮リンターに通す（再生成≤max_regens）。合格したかを返す。"""
    material = {"summary": trigger.summary, "source": trigger.source,
                "magnitude": trigger.magnitude, "refs": trigger.refs, "detail": trigger.payload}
    valid_ids = set(trigger.refs) if trigger.refs else None
    feedback: str | None = None
    wr: WriteResult | None = None
    ok = False
    for _ in range(cfg.flash.max_regens + 1):
        wr = write_flash(llm, material, is_prediction=is_prediction, model=model, feedback=feedback)
        report = lint_topic(
            wr.topic, mode="flash", valid_source_ids=valid_ids, is_prediction=is_prediction
        )
        if report.ok:
            ok = True
            break
        feedback = report.reasons()
    assert wr is not None
    return wr, ok


def process_triggers(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    triggers: list[FlashTrigger],
    *,
    cfg: PressConfig | None = None,
    model: str = "mid-default",
    as_of: datetime | None = None,
    image: ImageResult | None = None,
    channel: str = "press",
    mention: str | None = None,
) -> FlashResult:
    """トリガ群を一次判定→執筆→リンター→レート制御→outbox 投入まで処理する。"""
    cfg = cfg or PressConfig.load()
    as_of = as_of or datetime.now(UTC)
    result = FlashResult()

    # 1) 一次判定 + 執筆。閾値未満は「記録のみ」。
    ready: list[FlashOutcome] = []
    for trg in triggers:
        news, kind = _triage(llm, trg, model=model)
        is_pred = kind == "prediction"
        if news < cfg.flash.newsworthiness_threshold:
            rid = _save_flash_report(
                conn, run, trg, None, as_of=as_of, report_type="flash_skipped",
                newsworthiness=news, is_prediction=is_pred,
            )
            result.outcomes.append(
                FlashOutcome(trigger=trg, published=False, is_prediction=is_pred,
                             newsworthiness=news, report_id=rid, reason="報道価値が閾値未満")
            )
            continue
        wr, ok = _write_flash_topic(llm, trg, is_prediction=is_pred, cfg=cfg, model=model)
        if not ok:
            rid = _save_flash_report(
                conn, run, trg, wr.topic, as_of=as_of, report_type="flash_skipped",
                newsworthiness=news, is_prediction=is_pred,
            )
            result.outcomes.append(
                FlashOutcome(trigger=trg, published=False, is_prediction=is_pred,
                             newsworthiness=news, report_id=rid, topic=wr.topic,
                             reason="短縮リンター不合格")
            )
            continue
        ready.append(
            FlashOutcome(trigger=trg, published=False, is_prediction=is_pred,
                         newsworthiness=news, topic=wr.topic)
        )

    # 2) レート制御: 上限内は個別、超過分はまとめ速報へ（§3）。
    recent_h, recent_d = _recent_urgent_counts(conn, channel, as_of)
    plan = plan_emissions(
        len(ready), recent_hour=recent_h, recent_day=recent_d,
        per_hour=cfg.flash.per_hour, per_day=cfg.flash.per_day,
    )
    individual = ready[: plan.individual]
    overflow = ready[plan.individual :]

    for out in individual:
        _publish_individual(conn, run, out, cfg=cfg, as_of=as_of, image=image,
                            channel=channel, mention=mention)
        result.outcomes.append(out)

    if overflow:
        result.digest_outbox_id = _publish_digest(
            conn, run, overflow, cfg=cfg, as_of=as_of, image=image, channel=channel
        )
        result.outcomes.extend(overflow)

    return result


def _publish_individual(
    conn: psycopg.Connection, run: Run, out: FlashOutcome, *,
    cfg: PressConfig, as_of: datetime, image: ImageResult | None, channel: str, mention: str | None,
) -> None:
    assert out.topic is not None
    report_id = _save_flash_report(
        conn, run, out.trigger, out.topic, as_of=as_of,
        report_type="flash", newsworthiness=out.newsworthiness, is_prediction=out.is_prediction,
    )
    embed = embeds.build_flash_embed(
        out.topic, is_prediction=out.is_prediction, image=image,
        mention=mention, timestamp=as_of.isoformat(),
    )
    outbox_id = enqueue(conn, channel, embed, run.run_id, urgent=True)
    out.published = True
    out.outbox_id = outbox_id
    out.report_id = report_id
    if out.is_prediction:
        # report_id は「元の速報②」= 公開された outbox の id（§7 コメント）。
        out.prediction_id = _register_prediction(
            conn, outbox_id, out.topic, cfg=cfg, as_of=as_of
        )


def _publish_digest(
    conn: psycopg.Connection, run: Run, overflow: list[FlashOutcome], *,
    cfg: PressConfig, as_of: datetime, image: ImageResult | None, channel: str,
) -> int:
    """超過分を 1 本のまとめ速報に統合して投入する（§3）。個別 report は残す。"""
    for out in overflow:
        assert out.topic is not None
        report_id = _save_flash_report(
            conn, run, out.trigger, out.topic, as_of=as_of,
            report_type="flash", newsworthiness=out.newsworthiness,
            is_prediction=out.is_prediction,
        )
        out.report_id = report_id
        out.published = True
    embed = embeds.build_digest_embed(
        [o.topic for o in overflow if o.topic is not None], image=image,
        timestamp=as_of.isoformat(),
    )
    digest_id = enqueue(conn, channel, embed, run.run_id, urgent=True)
    for out in overflow:
        out.outbox_id = digest_id
        if out.is_prediction:
            out.prediction_id = _register_prediction(
                conn, digest_id, out.topic, cfg=cfg, as_of=as_of  # type: ignore[arg-type]
            )
    return digest_id


def run_flash(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    *,
    cfg: PressConfig | None = None,
    since: datetime | None = None,
    model: str = "mid-default",
    as_of: datetime | None = None,
    image: ImageResult | None = None,
    channel: str = "press",
    mention: str | None = None,
) -> FlashResult:
    """トリガ収集 → 処理まで一気に回す便宜関数。"""
    cfg = cfg or PressConfig.load()
    as_of = as_of or datetime.now(UTC)
    since = since or (as_of - timedelta(hours=6))
    triggers = collect_triggers(conn, since=since)
    return process_triggers(
        conn, run, llm, triggers, cfg=cfg, model=model, as_of=as_of,
        image=image, channel=channel, mention=mention,
    )


# ── 的中判定（§3）─────────────────────────────────────────────────────────────
# claim を受けて outcome('hit'|'miss'|'void') を返す決定論リゾルバ。
OutcomeResolver = Callable[[str, float, dict[str, Any]], str]


@dataclass
class VerifyResult:
    verified: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)


def verify_due_predictions(
    conn: psycopg.Connection,
    run: Run,
    resolver: OutcomeResolver,
    *,
    as_of: datetime | None = None,
) -> VerifyResult:
    """検証期限が到来した予兆速報を的中判定する（§3・外れの隠蔽を構造的に防ぐ）。

    ``resolver(claim, confidence, meta)`` が 'hit'|'miss'|'void' を返す（決定論・外部データ照合）。
    outcome と verified_at を更新する。判定不能は 'void'。
    """
    as_of = as_of or datetime.now(UTC)
    res = VerifyResult()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, report_id, claim, confidence, verify_by
            FROM press.predictions
            WHERE outcome = 'pending' AND verify_by <= %s
            ORDER BY id
            FOR UPDATE
            """,
            (as_of,),
        )
        rows = cur.fetchall()
    for pred_id, report_id, claim, confidence, verify_by in rows:
        outcome = resolver(
            str(claim), float(confidence),
            {"report_id": report_id, "verify_by": verify_by},
        )
        if outcome not in ("hit", "miss", "void"):
            outcome = "void"
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE press.predictions SET outcome = %s, verified_at = %s "
                "WHERE id = %s AND outcome = 'pending'",
                (outcome, as_of, pred_id),
            )
        res.verified += 1
        res.by_outcome[outcome] = res.by_outcome.get(outcome, 0) + 1
    return res


def prediction_hit_rate(
    conn: psycopg.Connection, *, since: datetime | None = None
) -> dict[str, Any]:
    """的中率の月次品質指標（§3）。hit/(hit+miss) を返す。void は分母から除外。"""
    clause = ""
    params: list[Any] = []
    if since is not None:
        clause = "WHERE verified_at >= %s"
        params.append(since)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT outcome, COUNT(*) FROM press.predictions
            {clause}
            GROUP BY outcome
            """,
            params,
        )
        counts = {str(o): int(c) for o, c in cur.fetchall()}
    hit = counts.get("hit", 0)
    miss = counts.get("miss", 0)
    denom = hit + miss
    return {
        "hit": hit, "miss": miss, "void": counts.get("void", 0),
        "pending": counts.get("pending", 0),
        "hit_rate": (hit / denom) if denom else None,
    }
