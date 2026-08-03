"""engine の数値検証(手計算固定値)とフラグ境界(T-015 受け入れ基準)。

判定境界はすべて **config/ips.yaml の実値**(dd_soft 15% / dd_hard 25% / 実現ボラ 15% /
ES95 3% / EWMA 20日)に対して検証する — 値のハードコード検知を兼ねる。
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from ryza.risk.engine import (
    Exclusion,
    NavPoint,
    RiskPosition,
    book_returns,
    drawdown,
    es95,
    evaluate,
    ewma_vol,
    guardrail_usage,
)

from .conftest import constant_growth_series, nav_series


# ── DD(手計算固定値・境界)────────────────────────────────────────────────────
def test_drawdown_since_inception_peak():
    dd, peak = drawdown(nav_series([100, 120, 90]))
    assert peak == Decimal(120)
    assert dd == Decimal(30) / Decimal(120)  # 25%(設定来ピーク比・連続測定)


def test_drawdown_day_one_is_zero():
    dd, peak = drawdown(nav_series([100]))
    assert dd == 0 and peak == Decimal(100)


def test_dd_soft_boundary_at_reach(ips):
    # 到達(≥)で発動: ピーク 100 → 85 は DD ちょうど 15% = dd_soft_limit。
    assert ips.hard_limits.dd_soft_limit == 0.15
    state = evaluate(nav_series([100, 85]), [], {}, ips)
    assert state.dd_soft and not state.dd_hard
    state = evaluate(nav_series([100, Decimal("85.01")]), [], {}, ips)
    assert not state.dd_soft


def test_dd_hard_boundary_at_reach(ips):
    assert ips.hard_limits.dd_hard_limit == 0.25
    state = evaluate(nav_series([100, 75]), [], {}, ips)
    assert state.dd_hard and state.dd_soft
    state = evaluate(nav_series([100, Decimal("75.01")]), [], {}, ips)
    assert not state.dd_hard


def test_dd_valid_from_day_one_even_with_insufficient_data(ips):
    # データ1日目から DD は有効(指示書)— リターン1件でも dd_soft は立つ。
    state = evaluate(nav_series([100, 80]), [], {}, ips)
    assert state.n_returns == 1 and not state.sufficient
    assert state.dd_soft


# ── リターン(フロー調整)──────────────────────────────────────────────────────
def test_book_returns_flow_adjusted():
    # ¥1,000,000 → 追加出資 ¥9,000,000 で NAV ¥10,000,000: リターンは 0(損益なし)。
    series = nav_series([1_000_000, 10_000_000], flows={1: 9_000_000})
    assert book_returns(series) == [0.0]


def test_book_returns_simple():
    assert book_returns(nav_series([100, 102])) == pytest.approx([0.02])


# ── BOP/EOP 分離(独立審査 2026-08-03 重要-1 のシナリオを真値で固定)──────────────
def test_book_returns_bop_inflow_matches_true_return():
    """シナリオ B: V₀=100万・期中 +50万・市場 +5% → 真値 +5.0%。

    期末フロー一律仮定(旧式 `(nav − flow − nav₀)/nav₀`)だと +7.5% になる
    (誤差 250bp)。期中に入った 50 万はその区間の運用元本なので分母に入れる。
    """
    series = nav_series([1_000_000, 1_575_000], bop_flows={1: 500_000})
    assert book_returns(series) == pytest.approx([0.05])
    # 旧式との差を明示(この値に戻ったら回帰)。
    old = (1_575_000 - 500_000 - 1_000_000) / 1_000_000
    assert old == pytest.approx(0.075)


def test_book_returns_bop_outflow_matches_true_return():
    """シナリオ C: V₀=100万・期中 −30万(払戻)・市場 +5% → 真値 +5.0%。"""
    series = nav_series([1_000_000, 735_000], bop_flows={1: -300_000})
    assert book_returns(series) == pytest.approx([0.05])


def test_book_returns_bop_doubling_capital_with_loss():
    """シナリオ H: V₀=100万・期中 +100万・市場 −3% → 真値 −3.0%。"""
    series = nav_series([1_000_000, 1_940_000], bop_flows={1: 1_000_000})
    assert book_returns(series) == pytest.approx([-0.03])


def test_book_returns_degenerates_without_bop_flow():
    """BOP フローが無い日は従来式に一致する(退化 — 定義変更の副作用が無いこと)。"""
    series = nav_series([1_000_000, 1_100_000], flows={1: 50_000})
    assert book_returns(series) == pytest.approx([(1_100_000 - 50_000 - 1_000_000) / 1_000_000])


def test_book_returns_measurable_after_full_withdrawal():
    """全額払戻で NAV 0 → 再出資した区間は分母 = flow_bop で測れる(除外しない)。"""
    series = nav_series([1_000_000, 0, 1_050_000], flows={1: -1_000_000}, bop_flows={2: 1_000_000})
    assert book_returns(series) == pytest.approx([0.0, 0.05])


# ── EWMA 実現ボラ(手計算固定値)──────────────────────────────────────────────
def test_ewma_vol_fixed_value():
    # α=2/21、σ²₁=r₁²、σ²₃ = (19/21)((19/21)·1e-4 + (2/21)·4e-4) + (2/21)·2.25e-4
    #      = 1.377551e-4 → √(·252) = 0.1863177…
    vol = ewma_vol([0.01, -0.02, 0.015], days=20)
    assert vol == pytest.approx(0.1863177, rel=1e-5)


def test_ewma_vol_constant_returns_closed_form():
    # 一定リターンでは EWMA 分散は r² のまま → 年率 |r|·√252(解析形)。
    assert ewma_vol([0.01] * 30, days=20) == pytest.approx(0.01 * math.sqrt(252))


def test_ewma_vol_empty_is_none():
    assert ewma_vol([], days=20) is None


# ── ES95(手計算固定値)────────────────────────────────────────────────────────
def _returns_map(instrument_id, values, *, start=date(2029, 1, 1)):
    from datetime import timedelta

    return {instrument_id: {start + timedelta(days=i): v for i, v in enumerate(values)}}


def test_es95_no_positions_is_zero():
    result = es95([], Decimal(10_000_000), {}, min_obs=20)
    assert result.adopted == 0.0 and result.n_obs == 0
    assert not result.deferred and result.excluded == ()


def test_es95_historical_fixed_value():
    # w=0.5、銘柄リターン 40 日(38×0、-0.04、-0.02)→ ポート系列の下位2件平均
    # = (0.02+0.01)/2 = 1.5%。パラメトリック(σ=0.003455·2.0627=0.71%)より大きい。
    positions = [RiskPosition(1, "equity_jp", Decimal(5_000_000))]
    rets = _returns_map(1, [0.0] * 38 + [-0.04, -0.02])
    result = es95(positions, Decimal(10_000_000), rets, min_obs=20)
    assert result.n_obs == 40
    assert result.historical == pytest.approx(0.015)
    assert result.parametric == pytest.approx(0.007127, rel=1e-3)
    assert result.adopted == pytest.approx(0.015)


def test_es95_parametric_dominates():
    # ±1% 交互(σ=1%)→ param = 0.01·φ(z95)/0.05 = 2.0627% > hist 1%。大きい方を採用。
    positions = [RiskPosition(1, "equity_jp", Decimal(10_000_000))]
    rets = _returns_map(1, [0.01, -0.01] * 20)
    result = es95(positions, Decimal(10_000_000), rets, min_obs=20)
    assert result.historical == pytest.approx(0.01)
    assert result.parametric == pytest.approx(0.0206271, rel=1e-4)
    assert result.adopted == result.parametric


def test_es95_excludes_short_series_and_measures_rest():
    # 短系列(10 < 20)の銘柄は除外し、残部(30 観測)で測定する(審査条件2の縮退)。
    positions = [
        RiskPosition(1, "equity_jp", Decimal(5_000_000)),
        RiskPosition(2, "equity_us", Decimal(5_000_000)),
    ]
    rets = _returns_map(1, [0.0] * 30)
    rets.update(_returns_map(2, [0.0] * 10))
    result = es95(positions, Decimal(10_000_000), rets, min_obs=20)
    assert result.excluded == (2,)
    assert result.n_obs == 30  # 除外により全体の判定保留化を防ぐ
    assert not result.deferred  # 除外は 2 銘柄中 1 = 過半ではない


def test_es95_deferred_when_excluded_majority():
    # 除外が過半(1/1)→ 判定保留。
    positions = [RiskPosition(1, "equity_jp", Decimal(5_000_000))]
    rets = _returns_map(1, [0.0] * 10)
    result = es95(positions, Decimal(10_000_000), rets, min_obs=20)
    assert result.excluded == (1,) and result.deferred
    assert result.n_obs == 0 and result.adopted == 0.0


def test_es95_deferred_when_holdings_but_no_returns():
    # 保有ありでリターン系列なし → 0 を「リスクなし」と読ませない(判定保留)。
    positions = [RiskPosition(1, "equity_jp", Decimal(5_000_000))]
    result = es95(positions, Decimal(10_000_000), {}, min_obs=20)
    assert result.deferred and result.n_obs == 0


def test_es95_common_dates_within_included_only():
    # 測定対象銘柄同士では共通日で測る(20+20 観測・共通 15 日 → n_obs=15)。
    from datetime import date as _date

    positions = [
        RiskPosition(1, "equity_jp", Decimal(5_000_000)),
        RiskPosition(2, "equity_us", Decimal(5_000_000)),
    ]
    rets = _returns_map(1, [0.0] * 20, start=_date(2029, 1, 1))
    rets.update(_returns_map(2, [0.0] * 20, start=_date(2029, 1, 6)))
    result = es95(positions, Decimal(10_000_000), rets, min_obs=20)
    assert result.excluded == ()
    assert result.n_obs == 15


# ── evaluate のフラグ境界(vol / es)──────────────────────────────────────────
def test_vol_exceeded_boundary(ips):
    # 一定 1% 日次(24 リターン ≥ 20)→ vol = 0.1587 > 上限 0.15。
    assert ips.hard_limits.realized_vol_limit == 0.15
    state = evaluate(constant_growth_series(25, rate="1.01"), [], {}, ips)
    assert state.sufficient and state.vol_exceeded
    # 一定 0.9% → vol = 0.1429 ≤ 上限。
    state = evaluate(constant_growth_series(25, rate="1.009"), [], {}, ips)
    assert not state.vol_exceeded


def test_vol_flag_suppressed_when_insufficient(ips):
    # 10 リターンで年率 79%(一定 5%)でもデータ不足中はフラグを立てない(fail-safe)。
    state = evaluate(constant_growth_series(11, rate="1.05"), [], {}, ips)
    assert not state.sufficient
    assert not state.vol_exceeded
    assert any("データ不足 10/20営業日" in n for n in state.notes)


def test_es_exceeded_boundary(ips):
    assert ips.hard_limits.daily_es95_nav_max == 0.03
    series = constant_growth_series(25, rate="1.001")  # 帳簿系列は十分・ボラ低
    positions = [RiskPosition(1, "equity_jp", Decimal(1_000_000))]
    nav = series[-1].nav
    w = float(Decimal(1_000_000) / nav)
    # ポート ES(hist)= w·(0.04+0.02)/2 … ではなく下位1件強で構成: 単純に大損2日。
    rets = _returns_map(1, [0.0] * 38 + [-0.9, -0.7])
    state = evaluate(series, positions, rets, ips)
    # hist = w·(0.9+0.7)/2 = 0.78w > 3%(w ≈ 0.097)。
    assert state.es95.adopted > 0.03 * 0.9  # 数値は下で厳密に
    assert state.es95.historical == pytest.approx(w * 0.8, rel=1e-6)
    assert state.es_exceeded


def test_es_flag_suppressed_and_deferred_when_series_short(ips):
    # 唯一の保有銘柄が短系列(10 < 20)→ 除外=過半 → 判定保留+urgent 注記。
    series = constant_growth_series(25, rate="1.001")
    positions = [RiskPosition(1, "equity_jp", Decimal(1_000_000))]
    rets = _returns_map(1, [-0.9] * 10)
    state = evaluate(series, positions, rets, ips)
    assert state.es95.deferred and not state.es_exceeded
    assert any("除外" in n for n in state.notes)
    assert any("【要確認】" in n for n in state.notes)


def test_es_note_required_when_holdings_but_no_returns(ips):
    # (審査条件2a)n_obs=0 かつ保有あり → 沈黙せず必ず注記+判定保留。
    series = constant_growth_series(25, rate="1.001")
    positions = [RiskPosition(1, "equity_jp", Decimal(1_000_000))]
    state = evaluate(series, positions, {}, ips)
    assert state.es95.deferred and not state.es_exceeded
    assert any("ES 測定不能" in n for n in state.notes)


def test_es_partial_exclusion_still_flags_on_remainder(ips):
    # 除外が過半でなければ残部で測定しフラグは有効(全体の判定保留化を防ぐ)。
    series = constant_growth_series(25, rate="1.001")
    nav = series[-1].nav
    positions = [
        RiskPosition(1, "equity_jp", Decimal(1_000_000)),
        RiskPosition(2, "equity_us", Decimal(100_000)),
    ]
    rets = _returns_map(1, [0.0] * 38 + [-0.9, -0.7])
    rets.update(_returns_map(2, [0.0] * 5))  # 短系列 → 除外(1/2 = 過半ではない)
    state = evaluate(series, positions, rets, ips)
    assert state.es95.excluded == (2,)
    assert not state.es95.deferred
    w = float(Decimal(1_000_000) / nav)
    assert state.es95.historical == pytest.approx(w * 0.8, rel=1e-6)
    assert state.es_exceeded  # 残部の測定でフラグ有効
    assert any("除外" in n for n in state.notes)


def test_no_positions_no_es_flag(ips):
    state = evaluate(constant_growth_series(25, rate="1.001"), [], {}, ips)
    assert state.es95.adopted == 0.0 and not state.es_exceeded


# ── 判定保留・除外の機械可読化(独立役員審査 T-018 重大-3 の恒久対応)──────────
def test_deferred_is_empty_when_everything_is_measurable(ips):
    """十分な系列・保有なしなら保留も除外もゼロ(「保留なし」を空で表現する)。"""
    state = evaluate(constant_growth_series(25, rate="1.001"), [], {}, ips)
    assert state.deferred == () and state.excluded == ()
    assert state.deferred_metrics() == frozenset()


def test_insufficient_returns_defers_both_vol_and_es(ips):
    """帳簿リターン不足は vol と ES の**両方**を保留する(es_exceeded も sufficient 条件)。"""
    state = evaluate(constant_growth_series(11, rate="1.05"), [], {}, ips)
    assert state.deferred_metrics() == {"realized_vol", "es95"}
    assert all(d.reason == "insufficient_returns" for d in state.deferred)
    assert {(d.observed, d.required) for d in state.deferred} == {(10, 20)}


def test_es_deferral_reason_distinguishes_no_observations(ips):
    """保有ありで観測ゼロは「観測不足」ではなく測定空白として区別できる(重大-3)。"""
    series = constant_growth_series(25, rate="1.001")
    positions = [RiskPosition(1, "equity_jp", Decimal(1_000_000))]
    state = evaluate(series, positions, {}, ips)
    assert state.deferred_metrics() == {"es95"}  # 帳簿系列は十分なので vol は有効
    (d,) = state.deferred
    assert d.reason == "no_observations" and d.observed == 0


def test_es_deferral_reason_distinguishes_majority_excluded(ips):
    """除外過半の保留は、除外された銘柄まで含めて理由を復元できる。"""
    series = constant_growth_series(25, rate="1.001")
    positions = [RiskPosition(1, "equity_jp", Decimal(1_000_000))]
    state = evaluate(series, positions, _returns_map(1, [-0.9] * 10), ips)
    (d,) = state.deferred
    assert d.reason == "majority_excluded"
    assert [(e.instrument_id, e.measure, e.reason, e.observed, e.required)
            for e in state.excluded] == [(1, "es95", "short_series", 10, 20)]


def test_partial_exclusion_is_recorded_without_deferring(ips):
    """残部で測れる除外は「保留ではないが採用値に含まれない銘柄」として残る。"""
    series = constant_growth_series(25, rate="1.001")
    positions = [
        RiskPosition(1, "equity_jp", Decimal(1_000_000)),
        RiskPosition(2, "equity_us", Decimal(100_000)),
    ]
    rets = _returns_map(1, [0.0] * 38 + [-0.9, -0.7])
    rets.update(_returns_map(2, [0.0] * 5))
    state = evaluate(series, positions, rets, ips)
    assert state.deferred == ()  # 判定は有効
    assert [(e.instrument_id, e.reason) for e in state.excluded] == [(2, "short_series")]


def test_extra_exclusions_are_carried_through(ips):
    """エンジン到達前の除外(時価欠測など)も同じ列に載る — 何を測っていないかを 1 か所で。"""
    missing = Exclusion(instrument_id=9, measure="valuation", reason="missing_price")
    state = evaluate(
        constant_growth_series(25, rate="1.001"), [], {}, ips,
        extra_exclusions=[missing],
    )
    assert state.excluded == (missing,)


def test_es_insufficient_obs_is_its_own_reason(ips):
    """観測が min_obs 未満でも除外過半でない場合は「ES 観測不足」として保留する。"""
    series = constant_growth_series(25, rate="1.001")
    positions = [
        RiskPosition(1, "equity_jp", Decimal(1_000_000)),
        RiskPosition(2, "equity_us", Decimal(1_000_000)),
    ]
    # 双方 20 観測(除外なし)だが共通日は 15 日 → n_obs=15 < 20。
    rets = _returns_map(1, [0.0] * 20, start=date(2029, 1, 1))
    rets.update(_returns_map(2, [0.0] * 20, start=date(2029, 1, 6)))
    state = evaluate(series, positions, rets, ips)
    assert state.excluded == () and not state.es95.deferred
    (d,) = state.deferred
    assert (d.metric, d.reason, d.observed, d.required) == ("es95", "insufficient_obs", 15, 20)
    assert not state.es_exceeded


# ── ガードレール消費率 ─────────────────────────────────────────────────────────
def test_guardrail_usage(ips):
    nav = Decimal(10_000_000)
    positions = [
        RiskPosition(1, "equity_jp", Decimal(1_500_000)),
        RiskPosition(2, "equity_jp", Decimal(1_000_000)),
        RiskPosition(3, "equity_us", Decimal(-500_000)),  # ショートもグロスに数える
    ]
    usage = guardrail_usage(positions, nav, Decimal(2_000_000), ips)
    assert usage["issuer_concentration"]["value"] == pytest.approx(0.15)
    assert usage["issuer_concentration"]["limit"] == 0.20
    assert usage["single_asset_class_gross"]["value"] == pytest.approx(0.25)
    assert usage["single_asset_class_gross"]["class"] == "equity_jp"
    assert usage["gross_leverage"]["value"] == pytest.approx(0.30)
    assert usage["cash_floor"]["value"] == pytest.approx(0.20)
    assert usage["cash_floor"]["limit"] == 0.05


def test_evaluate_empty_series_raises(ips):
    with pytest.raises(ValueError):
        evaluate([], [], {}, ips)


def test_navpoint_defaults():
    p = NavPoint(day=date(2030, 1, 1), nav=Decimal(100))
    assert p.net_flow == 0
