"""市場観ステートの決定論規約テスト: 慣性・magnitude・速報フック・スナップショット。

これらは決定論の単体テスト(LLM を通さない)。受け入れ基準の中核:
- 単一文書での regime 反転が拒否される / 複数日の証拠蓄積で通る。
- magnitude 閾値超で速報トリガのフックが発火する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ryza.research.market_view import (
    KeyRiskOp,
    MarketViewConfig,
    MarketViewDiff,
    RegimeChange,
    apply_update,
    initialize,
    load_current,
    snapshot_daily,
)

_CFG = MarketViewConfig.load()


def _init(conn, run):
    return initialize(
        conn, run, regime={"jp_equity": "risk_on"},
        key_risks=[{"risk_id": "infl", "statement": "インフレ", "confidence": 0.4, "refs": [1]}],
        basis_refs=[1],
    )


# ── 慣性ルール ─────────────────────────────────────────────────────────────────
def test_single_doc_regime_flip_rejected(conn, run):
    _init(conn, run)
    diff = MarketViewDiff(regime_changes=[
        RegimeChange("jp_equity", "risk_off", refs=[10], source_count=1, weight=1.0)
    ])
    result = apply_update(conn, run, diff, config=_CFG)
    # 単一日・単一ソース → 慣性未達で拒否。新版は作られない。
    assert result.view_id is None
    assert result.rejected and result.rejected[0].kind == "regime_flip"
    assert load_current(conn).regime["jp_equity"] == "risk_on"


def test_multi_day_evidence_flip_accepted(conn, run):
    _init(conn, run)
    day1 = datetime(2026, 8, 1, 9, tzinfo=UTC)
    day2 = day1 + timedelta(days=1)
    # Day1: 拒否されるが証拠は蓄積される。
    d1 = MarketViewDiff(regime_changes=[
        RegimeChange("jp_equity", "risk_off", refs=[11], source_count=1, weight=0.6)
    ])
    r1 = apply_update(conn, run, d1, config=_CFG, as_of=day1)
    assert r1.view_id is None
    assert load_current(conn).regime["jp_equity"] == "risk_on"
    # Day2: 別日・別ソースの証拠が積み上がり慣性を満たす → 適用。
    d2 = MarketViewDiff(regime_changes=[
        RegimeChange("jp_equity", "risk_off", refs=[12], source_count=1, weight=0.6)
    ])
    r2 = apply_update(conn, run, d2, config=_CFG, as_of=day2)
    assert r2.view_id is not None
    assert r2.applied[0].kind == "regime_flip"
    assert load_current(conn).regime["jp_equity"] == "risk_off"


def test_new_regime_dimension_added_without_inertia(conn, run):
    _init(conn, run)
    # 存在しない次元は反転ではないので即適用(小 magnitude)。
    diff = MarketViewDiff(regime_changes=[
        RegimeChange("rates", "tightening", refs=[20])
    ])
    result = apply_update(conn, run, diff, config=_CFG)
    assert result.view_id is not None
    assert result.applied[0].kind == "regime_add"
    assert load_current(conn).regime["rates"] == "tightening"


def test_regime_change_without_refs_rejected(conn, run):
    _init(conn, run)
    diff = MarketViewDiff(regime_changes=[RegimeChange("jp_equity", "risk_off", refs=[])])
    with pytest.raises(ValueError, match="根拠 refs"):
        apply_update(conn, run, diff, config=_CFG)


# ── magnitude と速報フック ─────────────────────────────────────────────────────
def test_flash_hook_fires_on_regime_flip(conn, run):
    _init(conn, run)
    fired: list[tuple[int, float, dict]] = []
    day1 = datetime(2026, 8, 1, tzinfo=UTC)
    day2 = day1 + timedelta(days=1)
    ch = RegimeChange("jp_equity", "risk_off", refs=[30], source_count=1, weight=0.6)
    apply_update(conn, run, MarketViewDiff(regime_changes=[ch]), config=_CFG, as_of=day1)
    ch2 = RegimeChange("jp_equity", "risk_off", refs=[31], source_count=1, weight=0.6)
    result = apply_update(
        conn, run, MarketViewDiff(regime_changes=[ch2]), config=_CFG, as_of=day2,
        flash_hook=lambda vid, mag, reason: fired.append((vid, mag, reason)),
    )
    # regime 反転 magnitude 0.7 >= flash_threshold 0.5 → 発火。
    assert result.flash_triggered
    assert result.magnitude == pytest.approx(0.70)
    assert fired and fired[0][0] == result.view_id
    # flash_triggers 台帳にも 1 行。
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.flash_triggers WHERE view_id = %s",
                    (result.view_id,))
        assert cur.fetchone()[0] == 1


def test_small_confidence_tweak_no_flash(conn, run):
    _init(conn, run)
    fired: list = []
    diff = MarketViewDiff(key_risk_ops=[
        KeyRiskOp("update_confidence", "infl", refs=[40], confidence=0.5)
    ])
    result = apply_update(
        conn, run, diff, config=_CFG,
        flash_hook=lambda *a: fired.append(a),
    )
    # 確度 0.4→0.5 の微調整 magnitude = 0.1*0.5 = 0.05 < 0.5。
    assert not result.flash_triggered
    assert result.magnitude == pytest.approx(0.05)
    assert not fired


def test_key_risk_add_and_resolve(conn, run):
    _init(conn, run)
    add = MarketViewDiff(key_risk_ops=[
        KeyRiskOp("add", "fx_shock", refs=[50], confidence=0.6,
                  statement="急激な円高", observable="ドル円が140を割れ")
    ])
    r = apply_update(conn, run, add, config=_CFG)
    assert r.view_id is not None
    risks = {x["risk_id"] for x in load_current(conn).key_risks}
    assert "fx_shock" in risks
    # 解消。
    res = apply_update(conn, run,
                       MarketViewDiff(key_risk_ops=[KeyRiskOp("resolve", "fx_shock", refs=[51])]),
                       config=_CFG)
    assert res.applied[0].kind == "key_risk_resolve"
    assert "fx_shock" not in {x["risk_id"] for x in load_current(conn).key_risks}


# ── 日次スナップショット ────────────────────────────────────────────────────────
def test_daily_snapshot_point_in_time(conn, run):
    view = _init(conn, run)
    d = datetime(2026, 8, 2, tzinfo=UTC)
    snap_id = snapshot_daily(conn, run, as_of=d)
    assert snap_id is not None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT view_id FROM docs.market_view_daily WHERE snapshot_date = %s",
            (d.date(),),
        )
        assert cur.fetchone()[0] == view.view_id


def test_apply_requires_initialized_view(conn, run):
    diff = MarketViewDiff(regime_changes=[RegimeChange("jp_equity", "risk_off", refs=[1])])
    with pytest.raises(RuntimeError, match="未初期化"):
        apply_update(conn, run, diff, config=_CFG)
