"""ゲート規則 G-0〜G-10 の pass/block 境界(純ロジック — DB 不要)。

判定境界の値はすべて config/ips.yaml・config/mandates/*.yaml の**発効実値**から導出する
(ハードコード検知)。加えて保護領域のリグレッション検知として、発効値そのものを
test_ips_effective_values で固定する(IPS v1.3 の承認値が変わればここで露見する)。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from ryza.gate.compliance import LimitsState, PositionState, evaluate
from ryza.ips import Mandate

from .conftest import jp_stock_proposal, make_state, rules_of


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
        {"trading_state": None},
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


# ── G-0 取引状態 ─────────────────────────────────────────────────────────────
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
        nav=nav, daily_turnover=half - Decimal(50_000), limits=LimitsState(dd_soft=True)
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
    """Jim のショートは先物ヘッジのみ可。ETF ショートは block、指数先物は pass。"""
    etf_short = jp_stock_proposal(
        fm="jim", side="short", product="etf", asset_class="equity_jp",
        universe_tags=("etf", "liquid_equity"), is_single_name=False, signal_ids=(1,),
    )
    blocked = evaluate(etf_short, make_state(), ips, mandates)
    assert blocked.verdict == "block"
    assert "G-9" in rules_of(blocked)

    futures_short = replace(
        etf_short, product="listed_futures_index", universe_tags=("index_futures",)
    )
    assert evaluate(futures_short, make_state(), ips, mandates).verdict == "pass"


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
        make_state(positions=positions, limits=LimitsState(dd_hard=True)),
        ips,
        mandates,
    )
    assert result.verdict == "block"
    assert "G-10" in rules_of(result)


@pytest.mark.parametrize("flag", ["vol_exceeded", "es_exceeded"])
def test_g10_vol_es_block_new_build_only(ips, mandates, flag):
    limits = LimitsState(**{flag: True})
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


# ── 注文案そのものの検証 ─────────────────────────────────────────────────────
def test_invalid_proposal_raises():
    with pytest.raises(ValueError):
        jp_stock_proposal(side="hold")
    with pytest.raises(ValueError):
        jp_stock_proposal(qty=Decimal(0))
    with pytest.raises(ValueError):
        jp_stock_proposal(order_type="limit")  # limit なのに limit_price なし
