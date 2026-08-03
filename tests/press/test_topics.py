"""報道価値スコアリングの単体テスト(30-press §2)。採点は純関数。"""

from __future__ import annotations

from ryza.press.config import TopicsConfig
from ryza.press.topics import TopicCandidate, rank_candidates, score_candidate, select_top


def _cand(key: str, *, novelty: float, impact: float, confidence: float,
          category: str | None = None) -> TopicCandidate:
    return TopicCandidate(
        key=key, title=key, source_kind="document", category=category,
        refs=[1], novelty=novelty, impact=impact, confidence=confidence,
    )


def _cfg() -> TopicsConfig:
    return TopicsConfig(policy_geo_bonus=0.15, policy_geo_categories=("policy", "news_geopolitics"))


def test_score_is_product_of_three_factors():
    sc = score_candidate(_cand("a", novelty=0.5, impact=0.8, confidence=0.5), _cfg())
    assert abs(sc.score - 0.2) < 1e-9  # 0.5*0.8*0.5
    assert sc.rationale["formula"] == "novelty * impact * confidence + policy_geo_bonus"
    assert sc.rationale["policy_geo"] is False


def test_policy_geo_gets_bonus():
    base = score_candidate(_cand("a", novelty=0.5, impact=0.5, confidence=0.5), _cfg())
    boosted = score_candidate(
        _cand("b", novelty=0.5, impact=0.5, confidence=0.5, category="policy"), _cfg()
    )
    assert abs(boosted.score - (base.score + 0.15)) < 1e-9
    assert boosted.rationale["policy_geo"] is True


def test_rank_is_descending_and_stable():
    cands = [
        _cand("low", novelty=0.1, impact=0.1, confidence=0.1),
        _cand("high", novelty=0.9, impact=0.9, confidence=0.9),
        _cand("mid", novelty=0.5, impact=0.5, confidence=0.5),
    ]
    ranked = rank_candidates(cands, _cfg())
    assert [r.candidate.key for r in ranked] == ["high", "mid", "low"]


def test_select_top_caps_at_max_topics():
    cfg = TopicsConfig(max_topics=5)
    cands = [_cand(f"c{i}", novelty=1.0, impact=1.0, confidence=1.0 - i * 0.05) for i in range(8)]
    top = select_top(cands, cfg)
    assert len(top) == 5
    # 上位 5 件は confidence 降順の先頭 5。
    assert [t.candidate.key for t in top] == ["c0", "c1", "c2", "c3", "c4"]


def test_rationale_is_persisted_shape():
    sc = score_candidate(_cand("a", novelty=0.4, impact=0.6, confidence=0.7), _cfg())
    for k in ("novelty", "impact", "confidence", "base", "score", "policy_geo_bonus"):
        assert k in sc.rationale
