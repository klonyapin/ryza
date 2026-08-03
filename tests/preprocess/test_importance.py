"""importance（一次重要度スコア）の単体テスト（DB 非依存・config は実ファイル）。"""

from __future__ import annotations

from ryza.preprocess.importance import ImportanceConfig, score_importance, tier_of


def test_config_loads():
    cfg = ImportanceConfig.load()
    assert cfg.version == "1"
    assert cfg.category_weights["filing_guidance_revision"] == 0.80
    assert cfg.tiers["high"] == 0.66


def test_high_tier_for_guidance_revision_on_held(config):
    # 業績予想の修正（0.80）+ 保有加点（0.30）→ クランプ 1.0 → high。
    r = score_importance(
        config, category="filing_guidance_revision",
        instrument_ids=[10], held_ids={10}, watchlist_ids=set(),
    )
    assert r.score == 1.0
    assert r.tier == "high"
    factors = {x["factor"] for x in r.reasons}
    assert "held_instrument" in factors


def test_low_tier_for_unknown(config):
    r = score_importance(config, category="unknown", instrument_ids=[])
    assert r.tier == "low"
    assert r.score < config.tiers["mid"]


def test_watchlist_and_anomaly_bonuses(config):
    # 決算短信(0.60) + watchlist(0.20) + anomaly(0.20) = 1.0 → high。
    r = score_importance(
        config, category="filing_earnings", instrument_ids=[5],
        watchlist_ids={5}, statistical_anomaly=True,
    )
    assert r.tier == "high"
    factors = {x["factor"] for x in r.reasons}
    assert {"watchlist_instrument", "statistical_anomaly"} <= factors


def test_multiple_instruments_bonus(config):
    r_single = score_importance(config, category="news_fx", instrument_ids=[1])
    r_multi = score_importance(config, category="news_fx", instrument_ids=[1, 2])
    assert r_multi.score > r_single.score


def test_mid_tier_boundary(config):
    # 単独 news_fx(0.40) は mid（0.33 <= 0.40 < 0.66）。
    r = score_importance(config, category="news_fx", instrument_ids=[])
    assert r.tier == "mid"


def test_tier_of_thresholds(config):
    assert tier_of(config, 0.66) == "high"
    assert tier_of(config, 0.65) == "mid"
    assert tier_of(config, 0.33) == "mid"
    assert tier_of(config, 0.32) == "low"
