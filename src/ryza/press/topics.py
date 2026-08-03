"""topics — トピック候補生成＋報道価値スコアリング（30-press §2）。

候補は「market_view の変化点・triage_queue の高スコア文書・カレンダーイベント」から作り、
**報道価値 = 新規性 × 影響度 × 確度** で採点する（政策・地政学に定常加点）。**採点根拠も保存**
する（監査可能性・§2）。採点（``score_candidate`` / ``rank_candidates``）は純関数。DB 収集
（``collect_candidates``）は point-in-time（as_of までの素材のみ）。

新規性の実装上の注意: 設計は「既報との埋め込み距離」を掲げるが、market_view 変化点や
カレンダーイベントには候補ごとの埋め込みが無い。ここでは**既報（直近の morning_press/flash）が
参照した refs との重なり**で新規性を近似する（重なれば既報として減衰）。埋め込み距離への差し替えは
``novelty`` の算出関数を置き換えるだけでよい（フック化してある）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ryza.press.config import TopicsConfig

# 一次情報とみなす source_type（確度を底上げ・§2「一次情報か」）。
_PRIMARY_SOURCE_TYPES = frozenset({"filing", "gov", "policy", "court"})

# 既報と重なったときに残す新規性（0-1）。重なりが無ければ 1.0。
_REPORTED_NOVELTY = 0.3


@dataclass(frozen=True)
class TopicCandidate:
    """1 トピック候補。3 因子（0-1）と素材・refs を持つ。"""

    key: str  # 重複抑止・既報判定の一意キー
    title: str
    source_kind: str  # market_view|document|calendar
    category: str | None
    refs: list[int]  # doc_id 等（リネージ・出典）
    novelty: float
    impact: float
    confidence: float
    material: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoredCandidate:
    """採点済み候補。``rationale`` は採点根拠（監査用に保存する・§2）。"""

    candidate: TopicCandidate
    score: float
    rationale: dict[str, Any]


# ── 採点（純関数）──────────────────────────────────────────────────────────────
def score_candidate(c: TopicCandidate, cfg: TopicsConfig) -> ScoredCandidate:
    """報道価値 = 新規性×影響度×確度（+ 政策/地政学の定常加点）。採点根拠も返す。"""
    base = c.novelty * c.impact * c.confidence
    is_policy_geo = c.category in cfg.policy_geo_categories
    bonus = cfg.policy_geo_bonus if is_policy_geo else 0.0
    score = base + bonus
    rationale = {
        "formula": "novelty * impact * confidence + policy_geo_bonus",
        "novelty": round(c.novelty, 4),
        "impact": round(c.impact, 4),
        "confidence": round(c.confidence, 4),
        "base": round(base, 4),
        "policy_geo": is_policy_geo,
        "policy_geo_bonus": bonus,
        "score": round(score, 4),
        "source_kind": c.source_kind,
        "category": c.category,
    }
    return ScoredCandidate(candidate=c, score=score, rationale=rationale)


def rank_candidates(
    candidates: list[TopicCandidate], cfg: TopicsConfig
) -> list[ScoredCandidate]:
    """全候補を採点し降順ソート（同点は key で安定化）。"""
    scored = [score_candidate(c, cfg) for c in candidates]
    scored.sort(key=lambda s: (-s.score, s.candidate.key))
    return scored


def select_top(
    candidates: list[TopicCandidate], cfg: TopicsConfig
) -> list[ScoredCandidate]:
    """報道価値上位 ``max_topics`` 件を選ぶ（§2「上位最大5件」）。"""
    return rank_candidates(candidates, cfg)[: cfg.max_topics]


# ── DB 収集（point-in-time）───────────────────────────────────────────────────
def _watchlist_ids(conn: psycopg.Connection) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id FROM market.watchlist")
        return {int(r[0]) for r in cur.fetchall()}


def _reported_refs_since(conn: psycopg.Connection, since: datetime) -> set[int]:
    """直近の朝刊/速報が参照した doc_id 集合（新規性の減衰に使う）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT input_refs FROM docs.research_reports
            WHERE agent = 'press' AND report_type IN ('morning_press', 'flash')
              AND as_of >= %s
            """,
            (since,),
        )
        refs: set[int] = set()
        for (input_refs,) in cur.fetchall():
            for d in (input_refs or {}).get("doc_ids", []):
                refs.add(int(d))
    return refs


def _novelty(refs: list[int], reported: set[int]) -> float:
    return _REPORTED_NOVELTY if any(r in reported for r in refs) else 1.0


def _from_market_view(
    conn: psycopg.Connection, reported: set[int]
) -> list[TopicCandidate]:
    """最新 market_view の適用済み変化点を候補化する（§2「market_view の変化点」）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT view_id, changes, basis_refs FROM docs.market_view "
            "ORDER BY view_id DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None or not row[1]:
        return []
    view_id, changes, basis_refs = row
    basis = [int(x) for x in (basis_refs or [])]
    out: list[TopicCandidate] = []
    for i, ch in enumerate(changes.get("applied", []) or []):
        detail = ch.get("detail", {})
        refs = [int(x) for x in (detail.get("refs") or basis)]
        magnitude = float(ch.get("magnitude", 0.0))
        sources = int(detail.get("accumulated_sources", 1) or 1)
        subject = detail.get("dimension") or detail.get("risk_id") or ch.get("kind") or i
        out.append(
            TopicCandidate(
                key=f"mv:{view_id}:{ch.get('kind')}:{subject}",
                title=f"市場観の変化: {subject}",
                source_kind="market_view",
                category=None,
                refs=refs,
                novelty=_novelty(refs, reported),
                impact=min(1.0, magnitude),
                confidence=min(1.0, 0.4 + 0.2 * sources),
                material={"kind": ch.get("kind"), "detail": detail, "view_id": view_id},
            )
        )
    return out


def _from_documents(
    conn: psycopg.Connection, watchlist: set[int], reported: set[int], *, limit: int
) -> list[TopicCandidate]:
    """triage_queue の高スコア文書を候補化する（§2「research_reports の高スコア項目」の近似）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_id, source_type, source_name, title, category,
                   importance_tier, importance_score, instrument_ids
            FROM docs.triage_queue
            ORDER BY importance_score DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    out: list[TopicCandidate] = []
    for doc_id, source_type, source_name, title, category, _tier, score, inst_ids in rows:
        instruments = [int(x) for x in (inst_ids or [])]
        held = bool(set(instruments) & watchlist)
        importance = float(score) if score is not None else 0.5
        # 影響度: 保有/ウォッチ銘柄に触れるなら底上げ、そうでなければ重要度スコア。
        impact = max(importance, 0.85) if held else importance
        # 確度: 重要度 + 一次情報ボーナス。
        confidence = min(1.0, importance + (0.15 if source_type in _PRIMARY_SOURCE_TYPES else 0.0))
        out.append(
            TopicCandidate(
                key=f"doc:{doc_id}",
                title=title or f"{source_name} の開示",
                source_kind="document",
                category=category,
                refs=[int(doc_id)],
                novelty=_novelty([int(doc_id)], reported),
                impact=impact,
                confidence=confidence,
                material={
                    "doc_id": int(doc_id),
                    "source": source_name,
                    "title": title,
                    "category": category,
                    "instrument_ids": instruments,
                    "held_or_watched": held,
                },
            )
        )
    return out


def _from_calendar(
    conn: psycopg.Connection, as_of: datetime, *, horizon_hours: int = 24
) -> list[TopicCandidate]:
    """本日のカレンダーイベントを候補化する（§2「カレンダーイベント」）。"""
    window_end = as_of + timedelta(hours=horizon_hours)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id, event_type, title, scheduled_at, importance
            FROM market.calendar_events
            WHERE scheduled_at >= %s AND scheduled_at < %s
            ORDER BY scheduled_at
            """,
            (as_of, window_end),
        )
        rows = cur.fetchall()
    out: list[TopicCandidate] = []
    for event_id, event_type, title, scheduled_at, importance in rows:
        imp = min(1.0, (int(importance) or 1) / 3.0)
        category = "policy" if event_type == "policy" else event_type
        out.append(
            TopicCandidate(
                key=f"cal:{event_id}",
                title=title,
                source_kind="calendar",
                category=category,
                refs=[],  # イベントは doc 参照を持たない（refs は本文側で補う）
                novelty=1.0,  # 予定イベントは常に「本日の新規」
                impact=imp,
                confidence=imp,
                material={
                    "event_type": event_type,
                    "title": title,
                    "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
                },
            )
        )
    return out


def collect_candidates(
    conn: psycopg.Connection,
    cfg: TopicsConfig,
    *,
    as_of: datetime | None = None,
    doc_limit: int = 20,
) -> list[TopicCandidate]:
    """素材（market_view・triage_queue・calendar）から候補を集める（point-in-time）。"""
    as_of = as_of or datetime.now(UTC)
    since = as_of - timedelta(days=cfg.novelty_lookback_days)
    watchlist = _watchlist_ids(conn)
    reported = _reported_refs_since(conn, since)
    candidates: list[TopicCandidate] = []
    candidates += _from_market_view(conn, reported)
    candidates += _from_documents(conn, watchlist, reported, limit=doc_limit)
    candidates += _from_calendar(conn, as_of)
    return candidates
