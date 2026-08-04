"""ゲート規則 G-0〜G-10 の pass/block 境界(純ロジック — DB 不要)。

判定境界の値はすべて config/ips.yaml・config/mandates/*.yaml の**発効実値**から導出する
(ハードコード検知)。加えて保護領域のリグレッション検知として、発効値そのものを
test_ips_effective_values で固定する(IPS v1.3 の承認値が変わればここで露見する)。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ryza.gate.compliance import LimitsState, PositionState, evaluate
from ryza.ips import Mandate

from .conftest import fresh_limits, jp_stock_proposal, make_state, rules_of


def _dec(x) -> Decimal:
    return Decimal(str(x))


# ── 保護領域リグレッション検知: 発効値の固定 ─────────────────────────────────
def test_ips_effective_values(ips):
    """IPS v1.3 の承認値(80-ips.md)が config から変わっていないこと。"""
    assert ips.hard_limits.issuer_concentration_nav_max == 0.20
    assert ips.hard_limits.daily_turnover_nav_max == 0.30
    assert ips.hard_limits.gross_leverage_max == 2.0
    assert ips.guardrails.single_asset_class_gross_nav_max == 0.70
    assert ips.guardrails.cash_nav_min == 0.05
    assert ips.guardrails.crypto_dormant is True
    assert ips.unit_lot_exception.max_units == 1
    assert ips.unit_lot_exception.unit_cost_nav_max == 0.35
    assert ips.unit_lot_exception.margin_buy_allowed is False
    assert ips.short_allowed is True
    assert ips.short_single_name_nav_max == 0.10
    assert ips.products_default == "deny"


def test_baseline_pass(ips, mandates):
    """基準形(Ben の日本株現物買い)は全12規則を評価して pass。"""
    result = evaluate(jp_stock_proposal(), make_state(), ips, mandates)
    assert result.verdict == "pass"
    assert result.reasons == ()
    assert result.checked_rules == (
        "G-F", "G-0", "G-1", "G-2", "G-3", "G-4", "G-5",
        "G-6", "G-7", "G-8", "G-9", "G-10",
    )


# ── G-F fail-closed ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "state_kw",
    [
        {"nav": None},
        {"cash": None},
        {"positions": None},
        {"daily_turnover": None},
        {"limits": None},
    ],
)
def test_fail_closed_missing_inputs(ips, mandates, state_kw):
    result = evaluate(jp_stock_proposal(), make_state(**state_kw), ips, mandates)
    assert result.verdict == "block"
    assert rules_of(result) == {"G-F"}
    assert result.checked_rules == ("G-F",)  # 入力不足時は他規則を評価しない


def test_fail_closed_missing_ref_price(ips, mandates):
    result = evaluate(jp_stock_proposal(ref_price=None), make_state(), ips, mandates)
    assert result.verdict == "block"
    assert rules_of(result) == {"G-F"}


def test_fail_closed_unknown_asset_class(ips, mandates):
    result = evaluate(
        jp_stock_proposal(asset_class="mystery"), make_state(), ips, mandates
    )
    assert result.verdict == "block"


def test_fail_closed_missing_position_price(ips, mandates):
    """保有銘柄(発注銘柄以外)の時価欠落は avg_cost 代用ではなく block(審査条件6)。"""
    positions = (PositionState("jim", 99, "equity_us", Decimal(100), Decimal(1000)),)
    result = evaluate(
        jp_stock_proposal(),
        make_state(positions=positions, auto_prices=False),
        ips,
        mandates,
    )
    assert result.verdict == "block"
    assert rules_of(result) == {"G-F"}
    assert any("時価欠落" in r.message for r in result.reasons)


# ── G-0 取引状態 ─────────────────────────────────────────────────────────────
def test_g0_blocks_when_state_missing(ips, mandates):
    """行欠落(未初期化)も block — 状態が測定できないことを normal と主張しない。"""
    result = evaluate(
        jp_stock_proposal(), make_state(trading_state=None), ips, mandates
    )
    assert result.verdict == "block"
    assert rules_of(result) == {"G-0"}
    assert any("未初期化" in r.message for r in result.reasons)
    assert "G-10" in result.checked_rules  # 入力は揃っているため他規則の評価は継続


@pytest.mark.parametrize("state", ["frozen", "winding_down", "flattening", "flattened"])
def test_g0_blocks_when_not_normal(ips, mandates, state):
    result = evaluate(jp_stock_proposal(), make_state(trading_state=state), ips, mandates)
    assert result.verdict == "block"
    assert "G-0" in rules_of(result)


# ── G-1 商品許可 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("product", ["options", "otc_derivatives", "unknown_product"])
def test_g1_blocks_disallowed_product(ips, mandates, product):
    result = evaluate(jp_stock_proposal(product=product), make_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-1" in rules_of(result)


@pytest.mark.parametrize(
    "flag", ["leveraged_etf", "inverse_etf", "supervised_or_delisting_stock"]
)
def test_g1_blocks_prohibited_instrument_flags(ips, mandates, flag):
    result = evaluate(
        jp_stock_proposal(instrument_flags=(flag,)), make_state(), ips, mandates
    )
    assert result.verdict == "block"
    assert "G-1" in rules_of(result)


# ── G-2 ユニバース・マンデート禁じ手 ─────────────────────────────────────────
def test_g2_blocks_outside_universe(ips, mandates):
    """Ben のユニバースは日本株・米国株現物のみ — ETF は block。"""
    result = evaluate(
        jp_stock_proposal(product="etf", asset_class="equity_jp", universe_tags=("etf",),
                          is_single_name=False),
        make_state(),
        ips,
        mandates,
    )
    assert result.verdict == "block"
    assert "G-2" in rules_of(result)


def test_g2_blocks_unknown_fm(ips, mandates):
    result = evaluate(jp_stock_proposal(fm="nobody"), make_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-2" in rules_of(result)


def test_g2_blocks_empty_universe_tags(ips, mandates):
    result = evaluate(jp_stock_proposal(universe_tags=()), make_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-2" in rules_of(result)


def test_g2_blocks_margin_for_ben(ips, mandates):
    """Ben の追加禁じ手: 信用取引。"""
    result = evaluate(jp_stock_proposal(is_margin=True), make_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-2" in rules_of(result)


def test_g2_blocks_single_name_for_stan(ips, mandates):
    """Stan の追加禁じ手: 個別株。"""
    result = evaluate(
        jp_stock_proposal(fm="stan"), make_state(), ips, mandates
    )
    assert result.verdict == "block"
    assert "G-2" in rules_of(result)


def test_g2_blocks_unclassified_single_name_for_stan(ips, mandates):
    """分類不能(is_single_name=None)は個別株禁止の判定不能として block(審査条件7)。"""
    result = evaluate(
        jp_stock_proposal(fm="stan", is_single_name=None), make_state(), ips, mandates
    )
    assert result.verdict == "block"
    assert "G-2" in rules_of(result)
    assert any("分類不能" in r.message for r in result.reasons)


def test_g2_requires_signal_for_jim(ips, mandates):
    """Jim の追加禁じ手: シグナル外売買(C-13 — signal_id 必須)。"""
    etf = jp_stock_proposal(
        fm="jim", product="etf", asset_class="equity_jp",
        universe_tags=("etf", "liquid_equity"), is_single_name=False,
    )
    blocked = evaluate(etf, make_state(), ips, mandates)
    assert blocked.verdict == "block"
    assert "G-2" in rules_of(blocked)

    passed = evaluate(replace(etf, signal_ids=(1,)), make_state(), ips, mandates)
    assert passed.verdict == "pass"


# ── G-3 発行体集中度 ─────────────────────────────────────────────────────────
def test_g3_fund_concentration_boundary(ips, mandates):
    """全ポッド合算で NAV×issuer_concentration_nav_max が境界(以下は pass・超は block)。"""
    nav = Decimal(10_000_000)
    limit = _dec(ips.hard_limits.issuer_concentration_nav_max) * nav
    price = Decimal(1000)
    # 他ポッド(jim)が同一銘柄を保有 → ben の買いで合算が境界を跨ぐ。
    pre_qty = (limit - Decimal(100_000)) / price  # 合算前 = 上限 − 10万円
    positions = (PositionState("jim", 1, "equity_jp", pre_qty, price),)
    at_limit = evaluate(
        jp_stock_proposal(qty=Decimal(100)),  # +10万円 → ちょうど上限
        make_state(nav=nav, positions=positions),
        ips,
        mandates,
    )
    assert at_limit.verdict == "pass"
    over = evaluate(
        jp_stock_proposal(qty=Decimal(101)),
        make_state(nav=nav, positions=positions),
        ips,
        mandates,
    )
    assert over.verdict == "block"
    assert "G-3" in rules_of(over)


def test_g3_pod_concentration_boundary(ips, mandates):
    """ポッド内集中度(仮想資本×マンデート上限)も判定する(81 §3)。"""
    ben = mandates["ben"]
    pod_limit = _dec(ben.pod_concentration_limit) * _dec(ben.capital_jpy)
    price = Decimal(1000)
    at_limit = evaluate(
        jp_stock_proposal(qty=pod_limit / price), make_state(), ips, mandates
    )
    assert at_limit.verdict == "pass"
    over = evaluate(
        jp_stock_proposal(qty=pod_limit / price + 10), make_state(), ips, mandates
    )
    assert over.verdict == "block"
    assert "G-3" in rules_of(over)
    assert any("ポッド内集中度" in r.message for r in over.reasons)


# ── 単元例外の4象限(1単元/2単元 × 取得価額 35% 以下/超)──────────────────────
def _unit_state(nav=Decimal(2_000_000)):
    return make_state(nav=nav, cash=Decimal(1_500_000))


def _unit_proposal(qty, price, **kw):
    """Peter の日本個別株(追加禁じ手に信用が無いポッド)。unit_size=100。"""
    return jp_stock_proposal(
        fm="peter", instrument_id=2, universe_tags=("jp_equity_midcap_cash",),
        qty=Decimal(qty), ref_price=Decimal(price), unit_size=Decimal(100), **kw
    )


def test_unit_lot_1unit_within_35pct_passes(ips, mandates):
    """1単元・取得価額 ≤ NAV の 35% → 集中度 20% 超でも許容。"""
    # NAV 200万: 集中度上限 40万 < 1単元 50万 ≤ 35% 上限 70万 → 例外適用で pass。
    result = evaluate(_unit_proposal(100, 5000), _unit_state(), ips, mandates)
    assert result.verdict == "pass"


def test_unit_lot_1unit_over_35pct_blocks(ips, mandates):
    """1単元でも取得価額 > NAV の 35% は例外不適用 → block。"""
    result = evaluate(_unit_proposal(100, 7100), _unit_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-3" in rules_of(result)


def test_unit_lot_2units_within_35pct_blocks(ips, mandates):
    """2単元目は通常判定 → block(単価は 35% 以下でも)。"""
    result = evaluate(_unit_proposal(200, 5000), _unit_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-3" in rules_of(result)


def test_unit_lot_2units_over_35pct_blocks(ips, mandates):
    result = evaluate(_unit_proposal(200, 7100), _unit_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-3" in rules_of(result)


def test_unit_lot_margin_buy_not_eligible(ips, mandates):
    """信用買いは単元例外の適用不可(margin_buy_allowed=false)。"""
    result = evaluate(_unit_proposal(100, 5000, is_margin=True), _unit_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-3" in rules_of(result)


def test_unit_lot_exception_not_applied_to_pod_limit(ips, mandates):
    """単元例外はファンド集中度のみ — ポッド集中度は緩めない(審査条件4・narrow_only)。"""
    # NAV 300万: 1単元 90万は 35% 上限(105万)以下でファンド例外は成立するが、
    # Ben のポッド上限(仮想資本 200万×40% = 80万)は例外なしで判定され block。
    result = evaluate(
        jp_stock_proposal(qty=Decimal(100), ref_price=Decimal(9000), unit_size=Decimal(100)),
        make_state(nav=Decimal(3_000_000), cash=Decimal(2_000_000)),
        ips,
        mandates,
    )
    assert result.verdict == "block"
    assert any("ポッド内集中度" in r.message for r in result.reasons)
    # ファンド集中度そのものは単元例外で通っている(reason に発行体集中度が無い)。
    assert not any(r.message.startswith("発行体集中度") for r in result.reasons)


# ── G-4 資産クラス ───────────────────────────────────────────────────────────
def test_g4_asset_class_gross_boundary(ips, mandates):
    nav = Decimal(10_000_000)
    limit = _dec(ips.guardrails.single_asset_class_gross_nav_max) * nav
    price = Decimal(1000)
    # equity_jp のグロス = 上限 − 10万円(銘柄あたり集中度は上限以下に分散)。
    per_inst = limit / 4 - Decimal(100_000)
    positions = tuple(
        PositionState("jim", 10 + i, "equity_jp", per_inst / price, price) for i in range(4)
    )
    room = limit - per_inst * 4  # 残枠 40万円
    at_limit = evaluate(
        jp_stock_proposal(instrument_id=20, qty=room / price),
        make_state(nav=nav, positions=positions),
        ips,
        mandates,
    )
    assert at_limit.verdict == "pass"
    over = evaluate(
        jp_stock_proposal(instrument_id=20, qty=room / price + 10),
        make_state(nav=nav, positions=positions),
        ips,
        mandates,
    )
    assert over.verdict == "block"
    assert "G-4" in rules_of(over)


# ── G-5 暗号資産休眠 ─────────────────────────────────────────────────────────
def test_g5_crypto_dormant_blocks(ips, mandates):
    result = evaluate(
        jp_stock_proposal(asset_class="crypto", product="listed_equity_cash",
                          universe_tags=("jp_equity_cash",), is_single_name=False),
        make_state(),
        ips,
        mandates,
    )
    assert result.verdict == "block"
    assert "G-5" in rules_of(result)


# ── G-6 現金下限 ─────────────────────────────────────────────────────────────
def test_g6_cash_floor_boundary(ips, mandates):
    nav = Decimal(10_000_000)
    floor = _dec(ips.guardrails.cash_nav_min) * nav
    cash = floor + Decimal(100_000)
    at_floor = evaluate(
        jp_stock_proposal(qty=Decimal(100)),  # −10万円 → ちょうど下限
        make_state(nav=nav, cash=cash),
        ips,
        mandates,
    )
    assert at_floor.verdict == "pass"
    below = evaluate(
        jp_stock_proposal(qty=Decimal(101)),
        make_state(nav=nav, cash=cash),
        ips,
        mandates,
    )
    assert below.verdict == "block"
    assert "G-6" in rules_of(below)


def test_g6_cash_raising_sell_passes_below_floor(ips, mandates):
    """現金が下限未満でも、現金が増える売り注文は G-6 で止めない。"""
    positions = (PositionState("ben", 1, "equity_jp", Decimal(500), Decimal(1000)),)
    result = evaluate(
        jp_stock_proposal(side="sell", qty=Decimal(100)),
        make_state(cash=Decimal(400_000), positions=positions),
        ips,
        mandates,
    )
    assert result.verdict == "pass"


# ── G-7 売買代金 ─────────────────────────────────────────────────────────────
def test_g7_daily_turnover_boundary(ips, mandates):
    nav = Decimal(10_000_000)
    limit = _dec(ips.hard_limits.daily_turnover_nav_max) * nav
    at_limit = evaluate(
        jp_stock_proposal(qty=Decimal(100)),  # +10万円
        make_state(nav=nav, daily_turnover=limit - Decimal(100_000)),
        ips,
        mandates,
    )
    assert at_limit.verdict == "pass"
    over = evaluate(
        jp_stock_proposal(qty=Decimal(101)),
        make_state(nav=nav, daily_turnover=limit - Decimal(100_000)),
        ips,
        mandates,
    )
    assert over.verdict == "block"
    assert "G-7" in rules_of(over)


def test_g7_dd_soft_halves_new_build_allowance(ips, mandates):
    """dd_soft 中の新規建ては当日売買代金枠を半減して評価(+G-10 warn)。"""
    nav = Decimal(10_000_000)
    half = _dec(ips.hard_limits.daily_turnover_nav_max) * nav / 2
    state = make_state(
        nav=nav, daily_turnover=half - Decimal(50_000), limits=fresh_limits(dd_soft=True)
    )
    over = evaluate(jp_stock_proposal(qty=Decimal(100)), state, ips, mandates)  # +10万円
    assert over.verdict == "block"
    assert "G-7" in rules_of(over, "block")
    assert "G-10" in rules_of(over, "warn")

    within = evaluate(jp_stock_proposal(qty=Decimal(40)), state, ips, mandates)  # +4万円
    assert within.verdict == "warn"  # dd_soft の新規建ては warn 付きで通る
    assert rules_of(within) == {"G-10"}


# ── G-8 レバレッジ ───────────────────────────────────────────────────────────
def test_g8_pod_gross_leverage_boundary(ips, mandates):
    """Ben のポッド・グロス上限(仮想資本×1.0)。"""
    ben = mandates["ben"]
    pod_max = _dec(ben.pod_gross_leverage_limit) * _dec(ben.capital_jpy)
    price = Decimal(1000)
    positions = (
        PositionState("ben", 1, "equity_jp", Decimal(750), price),
        PositionState("ben", 2, "equity_jp", Decimal(750), price),
        PositionState("ben", 3, "equity_jp", (pod_max - Decimal(1_600_000)) / price, price),
    )  # ポッド・グロス = 上限 − 10万円
    at_limit = evaluate(
        jp_stock_proposal(instrument_id=3, qty=Decimal(100)),
        make_state(positions=positions),
        ips,
        mandates,
    )
    assert at_limit.verdict == "pass"
    over = evaluate(
        jp_stock_proposal(instrument_id=3, qty=Decimal(110)),
        make_state(positions=positions),
        ips,
        mandates,
    )
    assert over.verdict == "block"
    assert "G-8" in rules_of(over)


def test_g8_fund_gross_wins_over_loose_mandate(ips, mandates):
    """narrow-only: マンデートが IPS より緩くても IPS のファンド上限が勝つ。"""
    loose = Mandate(
        fm="loose", version="test", approved_at="2026-08-03",
        universe=("jp_equity_cash",), capital_jpy=10_000_000,
        pod_sigma_budget=0.5, pod_gross_leverage_limit=10.0, pod_dd_limit=0.5,
        pod_concentration_limit=0.99, additional_prohibitions=(), short=True,
        benchmark="none",
    )
    nav = Decimal(10_000_000)
    fund_max = _dec(ips.hard_limits.gross_leverage_max) * nav
    price = Decimal(1000)
    # 分散保有でグロス = ファンド上限 − 10万円(集中度・クラス上限には触れない)。
    per_inst = Decimal(1_990_000)
    positions = tuple(
        PositionState("loose", 100 + i, cls, per_inst / price, price)
        for i, cls in enumerate(
            ["equity_jp", "equity_jp", "equity_jp", "equity_us", "equity_us",
             "equity_us", "bond", "bond", "fx", "commodity_futures"]
        )
    )  # 10 銘柄 × 199万 = 1990万 = 上限 − 10万
    proposal = jp_stock_proposal(fm="loose", instrument_id=100, qty=Decimal(110))
    over = evaluate(
        proposal,
        make_state(nav=nav, cash=Decimal(25_000_000), positions=positions),
        ips,
        {**mandates, "loose": loose},
    )
    assert over.verdict == "block"
    assert "G-8" in rules_of(over)
    assert fund_max == Decimal(20_000_000)  # aggressive 既定値の再確認


def test_g3_fund_concentration_wins_over_loose_mandate(ips, mandates):
    """narrow-only: ポッド集中度が緩くても IPS の NAV×20% が勝つ。"""
    loose = Mandate(
        fm="loose", version="test", approved_at="2026-08-03",
        universe=("jp_equity_cash",), capital_jpy=10_000_000,
        pod_sigma_budget=0.5, pod_gross_leverage_limit=10.0, pod_dd_limit=0.5,
        pod_concentration_limit=0.99, additional_prohibitions=(), short=True,
        benchmark="none",
    )
    result = evaluate(
        jp_stock_proposal(fm="loose", qty=Decimal(2500), ref_price=Decimal(1000)),
        make_state(cash=Decimal(5_000_000)),
        ips,
        {**mandates, "loose": loose},
    )
    assert result.verdict == "block"
    assert "G-3" in rules_of(result)


# ── G-9 ショート ─────────────────────────────────────────────────────────────
def test_g9_short_blocked_for_ben(ips, mandates):
    result = evaluate(jp_stock_proposal(side="short"), make_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-9" in rules_of(result)


def test_g9_jim_short_futures_only(ips, mandates):
    """Jim のショートは先物のみ可 — 先物以外の商品でのショートは block。"""
    etf_short = jp_stock_proposal(
        fm="jim", side="short", product="etf", asset_class="equity_jp",
        universe_tags=("etf", "liquid_equity"), is_single_name=False, signal_ids=(1,),
    )
    blocked = evaluate(etf_short, make_state(), ips, mandates)
    assert blocked.verdict == "block"
    assert "G-9" in rules_of(blocked)


def _jim_futures_short(qty):
    return jp_stock_proposal(
        fm="jim", side="short", product="listed_futures_index", asset_class="equity_jp",
        universe_tags=("index_futures",), is_single_name=False, signal_ids=(1,),
        qty=Decimal(qty), ref_price=Decimal(1000), instrument_id=6,
    )


def test_g9_jim_naked_futures_short_blocks(ips, mandates):
    """ヘッジ対象の現物ロングを持たない先物ショートは裸ショートとして block(審査条件5)。"""
    result = evaluate(_jim_futures_short(100), make_state(), ips, mandates)
    assert result.verdict == "block"
    assert "G-9" in rules_of(result)
    assert any("裸ショート" in r.message for r in result.reasons)


def test_g9_jim_hedge_within_long_exposure_passes(ips, mandates):
    """同一資産クラスの現物ロングの範囲内の先物ショートはヘッジとして pass。"""
    # Jim が equity_jp の ETF を 20万円分ロング → 先物ショート 10万円はヘッジ。
    positions = (PositionState("jim", 5, "equity_jp", Decimal(200), Decimal(1000)),)
    within = evaluate(
        _jim_futures_short(100), make_state(positions=positions), ips, mandates
    )
    assert within.verdict == "pass"
    # 相殺量(20万円)を超えるショート 30万円は block。
    over = evaluate(
        _jim_futures_short(300), make_state(positions=positions), ips, mandates
    )
    assert over.verdict == "block"
    assert "G-9" in rules_of(over)


def test_g9_single_name_short_cap(ips, mandates):
    """個別銘柄ショートは NAV×short_single_name_nav_max まで(IPS §5)。"""
    loose = Mandate(
        fm="loose", version="test", approved_at="2026-08-03",
        universe=("jp_equity_cash",), capital_jpy=10_000_000,
        pod_sigma_budget=0.5, pod_gross_leverage_limit=10.0, pod_dd_limit=0.5,
        pod_concentration_limit=0.99, additional_prohibitions=(), short=True,
        benchmark="none",
    )
    nav = Decimal(10_000_000)
    cap = _dec(ips.short_single_name_nav_max) * nav
    price = Decimal(1000)
    at_cap = evaluate(
        jp_stock_proposal(fm="loose", side="short", qty=cap / price),
        make_state(nav=nav),
        ips,
        {**mandates, "loose": loose},
    )
    assert at_cap.verdict == "pass"
    over = evaluate(
        jp_stock_proposal(fm="loose", side="short", qty=cap / price + 10),
        make_state(nav=nav),
        ips,
        {**mandates, "loose": loose},
    )
    assert over.verdict == "block"
    assert "G-9" in rules_of(over)


# ── G-10 リスク状態 ──────────────────────────────────────────────────────────
def test_g10_dd_hard_blocks_everything(ips, mandates):
    """dd_hard は縮小方向(売り)も含め全注文を止める(全新規発注停止)。"""
    positions = (PositionState("ben", 1, "equity_jp", Decimal(500), Decimal(1000)),)
    result = evaluate(
        jp_stock_proposal(side="sell", qty=Decimal(100)),
        make_state(positions=positions, limits=fresh_limits(dd_hard=True)),
        ips,
        mandates,
    )
    assert result.verdict == "block"
    assert "G-10" in rules_of(result)


@pytest.mark.parametrize("flag", ["vol_exceeded", "es_exceeded"])
def test_g10_vol_es_block_new_build_only(ips, mandates, flag):
    limits = fresh_limits(**{flag: True})
    new_build = evaluate(
        jp_stock_proposal(), make_state(limits=limits), ips, mandates
    )
    assert new_build.verdict == "block"
    assert "G-10" in rules_of(new_build)

    positions = (PositionState("ben", 1, "equity_jp", Decimal(500), Decimal(1000)),)
    closing = evaluate(
        jp_stock_proposal(side="sell", qty=Decimal(100)),
        make_state(positions=positions, limits=limits),
        ips,
        mandates,
    )
    assert closing.verdict == "pass"


# ── G-10 限度状態鮮度検査(独立役員審査 2026-08-03 T-015 統合条件)────────────────
# 判定時刻 _NOW = 2026-08-04(火)10:00 UTC / 19:00 JST → JST 判定日 = Aug 4 (Tue)。
# 経過営業日は「as_of 翌日〜判定日」に含まれる JP 営業日の数(``business_days_between``)。
# 「> 2 で block」= ちょうど 2 は pass(境界の下)、3 は block(境界の直上)。
_G10_NOW = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)


def _limits_at(as_of: datetime, **flags) -> LimitsState:
    """as_of と任意のフラグを持つ ``LimitsState`` を作る補助。"""
    return LimitsState(as_of=as_of, **flags)


def test_g10_freshness_at_now_passes(ips, mandates):
    """as_of == 判定時点(経過ゼロ営業日)は pass。"""
    state = make_state(now=_G10_NOW, limits=_limits_at(_G10_NOW))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "pass"
    assert "G-10" not in rules_of(result)


def test_g10_freshness_exactly_two_business_days_passes(ips, mandates):
    """境界: 経過 2 営業日ちょうど(as_of=Jul 31 Fri)は pass(> 2 で block のため境界の下)。

    Fri Jul 31 → Sat/Sun 非営業 → Mon Aug 3 (1) → Tue Aug 4 (2)。合計 2 → pass。
    """
    as_of = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)  # Fri
    state = make_state(now=_G10_NOW, limits=_limits_at(as_of))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "pass", result.reasons
    assert "G-10" not in rules_of(result, "block")


def test_g10_freshness_three_business_days_blocks(ips, mandates):
    """境界: 経過 3 営業日(as_of=Jul 30 Thu)は block(> 2 の直上)。

    Thu Jul 30 → Fri Jul 31 (1) → Mon Aug 3 (2) → Tue Aug 4 (3)。合計 3 → block。
    """
    as_of = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    state = make_state(now=_G10_NOW, limits=_limits_at(as_of))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "block"
    assert "G-10" in rules_of(result, "block")
    assert any(
        "古い" in r.message and "経過 3 営業日" in r.message
        for r in result.reasons
    ), result.reasons


def test_g10_freshness_as_of_none_blocks(ips, mandates):
    """as_of=NULL(限度状態行はあるが as_of が未記録)は fail-closed で block。"""
    state = make_state(now=_G10_NOW, limits=LimitsState(as_of=None))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "block"
    assert "G-10" in rules_of(result, "block")
    assert any(
        "as_of が NULL" in r.message and "fail-closed" in r.message
        for r in result.reasons
    )


def test_g10_freshness_no_row_blocks_via_gf(ips, mandates):
    """限度状態の**行が無い**(``limits=None``)は G-F(入力不足)で block。

    行不存在時は G-F 段階で止まり G-10 まで進まない。この二段構造は「行が無い」と
    「行はあるが as_of が古い/NULL」の**区別**を監査ログに残すため — 前者は engine
    が動いていない(初期化前)、後者は engine が止まっている(運用問題)。
    """
    result = evaluate(jp_stock_proposal(), make_state(now=_G10_NOW, limits=None),
                      ips, mandates)
    assert result.verdict == "block"
    assert rules_of(result) == {"G-F"}
    assert result.checked_rules == ("G-F",)


def test_g10_freshness_future_as_of_blocks(ips, mandates):
    """未来 as_of(時計ずれ等)も判定不能として block(fail-closed)。"""
    future = _G10_NOW + timedelta(days=1)
    state = make_state(now=_G10_NOW, limits=_limits_at(future))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "block"
    assert "G-10" in rules_of(result, "block")
    assert any("未来" in r.message for r in result.reasons)


def test_g10_freshness_weekend_crossing_two_bdays_passes(ips, mandates):
    """週末跨ぎ: as_of=Fri Jul 31 → 判定=Tue Aug 4。経過 = 2 営業日 → pass(境界の下)。

    注記: UTC の 22:00 以降は JST 日付が翌日にずれる — Fri 10:00 UTC = Fri 19:00 JST に固定。
    """
    as_of = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)  # Fri 19:00 JST
    state = make_state(now=_G10_NOW, limits=_limits_at(as_of))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "pass"


def test_g10_freshness_weekend_crossing_three_bdays_blocks(ips, mandates):
    """週末跨ぎ: as_of=Fri Jul 31 → 判定=Wed Aug 5。経過 = 3 営業日(Aug 3/4/5)→ block。"""
    now = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)  # Wed
    as_of = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)  # Fri
    state = make_state(now=now, limits=_limits_at(as_of))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "block"
    assert "G-10" in rules_of(result, "block")


def test_g10_freshness_holiday_crossing_passes(ips, mandates):
    """JP 祝日跨ぎ: 山の日(Tue Aug 11 2026)を挟んで判定 = Wed Aug 12。

    as_of=Fri Aug 7 の場合:
      翌日 Sat/Sun(非営業)→ Mon Aug 10 (1) → Tue Aug 11 (祝日・非営業) → Wed Aug 12 (2)。
    経過 = 2 営業日 → pass(祝日を「非営業」として数えたことで境界の下に留まる)。
    祝日テーブルが効かなければ経過 = 3 となり block に反転する — ミューテーション観点。
    """
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)  # Wed after 山の日
    as_of = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)  # Fri
    state = make_state(now=now, limits=_limits_at(as_of))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "pass", result.reasons


def test_g10_freshness_holiday_crossing_boundary_blocks(ips, mandates):
    """祝日跨ぎでも判定日を1日進めると block(経過 = 3 営業日)— 境界を落とすミューテーション検知。

    as_of=Fri Aug 7 → 判定=Thu Aug 13: Mon 10 (1) → Tue 11 (祝日) → Wed 12 (2) → Thu 13 (3)。
    """
    now = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)  # Thu
    as_of = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)  # Fri
    state = make_state(now=now, limits=_limits_at(as_of))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "block"
    assert "G-10" in rules_of(result, "block")


def test_g10_freshness_and_flags_are_independent(ips, mandates):
    """dd_hard=True でも as_of が古ければ理由が**両方**乗る(監査再現性)。

    dd_hard は「全注文停止」で既に block だが、鮮度検査を外すミューテーションが
    dd_hard に隠れて素通しにならないよう、鮮度違反の Reason が独立に残る。
    """
    as_of = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)  # 過去(> 2 営業日)
    state = make_state(now=_G10_NOW, limits=_limits_at(as_of, dd_hard=True))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "block"
    messages = [r.message for r in result.reasons if r.rule == "G-10"]
    assert any("古い" in m for m in messages)
    assert any("DD ハード" in m for m in messages)


def test_g10_freshness_now_missing_blocks_via_gf(ips, mandates):
    """判定時刻 ``state.now`` を渡さないと G-F で block(fail-closed)。

    「時刻が測定できない」を「新鮮」と主張しない — 呼び出し側が now を渡さないだけで
    鮮度検査を素通しにできる経路を潰す(独立役員審査 2026-08-03 T-015 統合条件と同姿勢)。
    """
    state = make_state(now=None, limits=_limits_at(_G10_NOW))
    result = evaluate(jp_stock_proposal(), state, ips, mandates)
    assert result.verdict == "block"
    assert rules_of(result) == {"G-F"}
    assert any("now" in r.message for r in result.reasons)


# ── 注文案そのものの検証 ─────────────────────────────────────────────────────
def test_invalid_proposal_raises():
    with pytest.raises(ValueError):
        jp_stock_proposal(side="hold")
    with pytest.raises(ValueError):
        jp_stock_proposal(qty=Decimal(0))
    with pytest.raises(ValueError):
        jp_stock_proposal(order_type="limit")  # limit なのに limit_price なし
