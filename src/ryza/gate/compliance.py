"""compliance — コンプライアンスゲート本体(T-014。保護領域 — 定款第5条)。

純決定論。入力は「注文案(``OrderProposal``)+現在状態(``PortfolioState``)+設定
(``IPSConfig``・mandates)」、出力は ``GateResult(verdict, reasons, checked_rules)``。
**判定順序は IPS → マンデート**(定款第4条: マンデートは狭める方向のみ)。判定値はすべて
``config/ips.yaml``・``config/mandates/*.yaml`` の発効値から取る(ハードコード禁止)。

規則(各規則は独立の小関数+規則 ID を持ち、評価した規則は全て ``checked_rules`` に残す):

- G-F 入力完全性(fail-closed): 判定に必要な入力(NAV・現金・positions・参照価格・
  当日売買代金・リスク状態)が欠けていれば pass ではなく block(reason=入力不足)
- G-0 取引状態: ``ops.trading_state`` が normal 以外なら block(凍結中の例外取引の
  承認経路は T-016。ここでは block+reason で足りる)
- G-1 商品許可: products.default=deny — allowed に無い商品種別は block。
  prohibitions.instruments(レバ/インバース ETF・監理銘柄)は block
- G-2 ユニバース(マンデート): 銘柄のユニバース分類が当該 FM の universe に含まれるか。
  additional_prohibitions(derivatives/short_selling/margin/single_name_equity/
  discretionary_trades_outside_signals)もここで評価
- G-3 発行体集中度: 約定後想定ポジションが NAV の 20% 超なら block。単元例外(§7-1)。
  ポッド内集中度(対仮想資本)も判定(81 §3「ゲートは両方を判定する」)
- G-4 資産クラス: 約定後の単一資産クラスグロスが NAV の 70% 超なら block
- G-5 暗号資産: crypto_dormant=true の間 crypto は block(解除後も crypto ≤ NAV 5%)
- G-6 現金下限: 約定後の現金が NAV の 5% を下回るなら block(現金が増える注文は除く)
- G-7 売買代金: 当日累計+本注文が NAV の 30% 超なら block(dd_soft 中の新規建ては枠半減)
- G-8 レバレッジ: 約定後グロス/NAV が 2.0 超なら block。ポッド別レバ上限も評価
- G-9 ショート: 個別銘柄ショートは NAV の 10% まで。マンデートで short 禁止の FM は block
- G-10 リスク状態: dd_hard は全注文 block。vol/es 超過は新規建て block。
  dd_soft は新規建てに warn(枠半減の実施は G-7)

実装上の判断(T-014 報告に列挙。レビュー対象):

- ユニバース判定は注文案が持つ ``universe_tags``(銘柄マスタ由来の決定論分類)と
  マンデート universe の共通部分で行う。タグ空は fail-closed で block
- 単元例外はファンド集中度(IPS)とポッド集中度(マンデート)の両方に適用する
  (§7-1「全帳簿共通(E8 小規模帳簿を含む)」— E8 単一ポッド帳簿で例外が機能するため)
- dd_soft の「新規建て枠半減」は G-7 の当日売買代金枠を新規建て注文に対して半減と解釈
- 「新規建て」= 当該 FM の建玉の絶対量が増える注文(|約定後| > |約定前|)
- ``short: hedge_futures_only``(Jim)は先物商品のショートのみ許可と解釈
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from ryza.ips import IPSConfig, Mandate

_SIDES = ("buy", "sell", "short", "cover")
_ORDER_TYPES = ("market", "limit")

# products 語彙(config/ips.yaml §8.2)のうちデリバティブ/先物に当たる集合。
# 値のハードコードではなく語彙の意味論(マンデート additional_prohibitions の機械判定)。
_DERIVATIVE_PRODUCTS = frozenset(
    {
        "listed_futures_index",
        "listed_futures_rates",
        "listed_futures_commodity",
        "otc_derivatives",
        "options",
    }
)
_FUTURES_PRODUCTS = frozenset(
    {"listed_futures_index", "listed_futures_rates", "listed_futures_commodity"}
)


def _dec(value: float | int | str | Decimal) -> Decimal:
    """設定値(float)を Decimal に安全変換する(str 経由で2進誤差を持ち込まない)。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ── 入力 dataclass 群 ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrderProposal:
    """注文案(FM・PM 層の決定論コードが組み立てる。T-015 で配線)。

    ``product``/``asset_class``/``universe_tags``/``is_single_name`` は銘柄マスタ
    (market.instruments)由来の決定論分類。LLM の出力をここに直接入れてはならない。
    """

    book_id: str
    fm: str
    instrument_id: int
    side: str  # buy|sell|short|cover
    qty: Decimal
    order_type: str  # market|limit
    limit_price: Decimal | None = None
    ref_price: Decimal | None = None  # 判定用参照価格(成行でも必須 — fail-closed)
    product: str = ""  # ips.products の語彙(listed_equity_cash 等)
    asset_class: str = ""  # ips.asset_class_taxonomy の語彙(デリバは原資産分類)
    universe_tags: tuple[str, ...] = ()  # マンデート universe との照合タグ
    instrument_flags: tuple[str, ...] = ()  # leveraged_etf 等(prohibitions.instruments 照合)
    is_single_name: bool = False  # 個別銘柄か(集中度・ショート上限・単元例外)
    is_margin: bool = False  # 信用取引か(単元例外の適用不可・マンデート禁じ手)
    unit_size: Decimal | None = None  # 日本個別株の1単元株数(単元例外判定)
    signal_ids: tuple[int, ...] = ()  # 根拠シグナル(C-13: シグナル外売買の機械判定)

    def __post_init__(self) -> None:
        if self.side not in _SIDES:
            raise ValueError(f"side は {_SIDES} のいずれか: {self.side!r}")
        if self.order_type not in _ORDER_TYPES:
            raise ValueError(f"order_type は {_ORDER_TYPES} のいずれか: {self.order_type!r}")
        if self.qty <= 0:
            raise ValueError(f"qty は正であるべき: {self.qty}")
        if (self.order_type == "limit") != (self.limit_price is not None):
            raise ValueError("limit 注文は limit_price 必須、market 注文は limit_price 不可")


@dataclass(frozen=True)
class PositionState:
    """現在ポジション1件(trading.positions の行に対応)。qty は符号付き(負=ショート)。"""

    fm: str
    instrument_id: int
    asset_class: str
    qty: Decimal
    avg_cost: Decimal


@dataclass(frozen=True)
class LimitsState:
    """リスク状態(risk.limits_state の行。算出は T-015 リスクエンジン)。"""

    dd_soft: bool = False
    dd_hard: bool = False
    vol_exceeded: bool = False
    es_exceeded: bool = False


@dataclass(frozen=True)
class PortfolioState:
    """判定時点の現在状態。欠けている入力は fail-closed(G-F)で block になる。"""

    trading_state: str | None  # ops.trading_state(normal|frozen|...)
    nav: Decimal | None  # 帳簿 NAV(JPY)
    cash: Decimal | None  # 現金(JPY)
    positions: tuple[PositionState, ...] | None  # 当該帳簿の全ポジション
    daily_turnover: Decimal | None  # 当日累計売買代金(JPY)
    limits: LimitsState | None  # リスク状態(行が無ければ None → fail-closed)
    prices: Mapping[int, Decimal] = field(default_factory=dict)  # instrument_id → 時価


@dataclass(frozen=True)
class Reason:
    """違反・警告1件。"""

    rule: str  # G-0 〜 G-10 / G-F
    severity: str  # block|warn
    message: str


@dataclass(frozen=True)
class GateResult:
    """ゲート判定の結果。reasons 空 = pass。"""

    verdict: str  # pass|warn|block
    reasons: tuple[Reason, ...]
    checked_rules: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"


def mandates_hash(mandates: Mapping[str, Mandate]) -> str:
    """判定に使ったマンデート集合の決定論ハッシュ(gate_log.mandates_hash)。"""
    canonical = json.dumps(
        {fm: asdict(m) for fm, m in sorted(mandates.items())},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── 判定コンテキスト(各規則が共有する導出値)────────────────────────────────
@dataclass(frozen=True)
class _Ctx:
    proposal: OrderProposal
    state: PortfolioState
    ips: IPSConfig
    mandate: Mandate | None
    price: Decimal  # 判定用の1株(1単位)価格
    delta: Decimal  # 符号付き数量変化(buy/cover:+、sell/short:-)
    pre_pod_qty: Decimal  # 当該 FM の約定前数量
    post_pod_qty: Decimal
    pre_fund_qty: Decimal  # 全ポッド合算の約定前数量
    post_fund_qty: Decimal
    notional: Decimal  # |qty|×price(売買代金)

    @property
    def new_build(self) -> bool:
        """新規建て(当該 FM の建玉の絶対量が増える)か。"""
        return abs(self.post_pod_qty) > abs(self.pre_pod_qty)


def _instrument_price(ctx_prices: Mapping[int, Decimal], pos: PositionState) -> Decimal:
    """ポジション評価に使う価格。時価が無ければ avg_cost で代用(T-015 で時価配線)。"""
    return ctx_prices.get(pos.instrument_id, pos.avg_cost)


def _pod_gross_post(ctx: _Ctx) -> Decimal:
    """当該 FM のポッド・グロス(約定後)。"""
    total = Decimal(0)
    for pos in ctx.state.positions or ():
        if pos.fm != ctx.proposal.fm:
            continue
        if pos.instrument_id == ctx.proposal.instrument_id:
            continue  # 発注銘柄は下で約定後値を足す
        total += abs(pos.qty) * _instrument_price(ctx.state.prices, pos)
    return total + abs(ctx.post_pod_qty) * ctx.price


def _class_gross_post(ctx: _Ctx) -> dict[str, Decimal]:
    """資産クラス別グロス(約定後)。発注銘柄は約定後数量で評価する。"""
    gross: dict[str, Decimal] = {}
    for pos in ctx.state.positions or ():
        if pos.instrument_id == ctx.proposal.instrument_id:
            continue
        value = abs(pos.qty) * _instrument_price(ctx.state.prices, pos)
        gross[pos.asset_class] = gross.get(pos.asset_class, Decimal(0)) + value
    inst_value = abs(ctx.post_fund_qty) * ctx.price
    cls = ctx.proposal.asset_class
    gross[cls] = gross.get(cls, Decimal(0)) + inst_value
    return gross


# ── 規則(独立の小関数。返り値は Reason のリスト)──────────────────────────────
def _g0_trading_state(ctx: _Ctx) -> list[Reason]:
    """G-0 取引状態: normal 以外は block(Kill Switch — IPS §5)。"""
    if ctx.state.trading_state != "normal":
        return [
            Reason(
                "G-0",
                "block",
                f"取引状態が normal ではない: {ctx.state.trading_state}"
                "(凍結中の例外取引は承認フロー経由 — T-016)",
            )
        ]
    return []


def _g1_products(ctx: _Ctx) -> list[Reason]:
    """G-1 商品許可: default=deny(IPS §8.2)+禁止商品(IPS §5)。"""
    reasons = []
    if ctx.proposal.product not in ctx.ips.products_allowed:
        reasons.append(
            Reason(
                "G-1",
                "block",
                f"商品種別 {ctx.proposal.product!r} は許可リストに無い"
                "(products.default=deny — IPS §8.2)",
            )
        )
    banned = set(ctx.proposal.instrument_flags) & set(ctx.ips.prohibited_instruments)
    if banned:
        reasons.append(
            Reason("G-1", "block", f"禁止商品フラグ(IPS §5): {sorted(banned)}")
        )
    return reasons


def _g2_mandate_universe(ctx: _Ctx) -> list[Reason]:
    """G-2 ユニバース+マンデート禁じ手(IPS の後に評価 — narrow only)。"""
    m = ctx.mandate
    if m is None:
        return [
            Reason(
                "G-2", "block", f"FM {ctx.proposal.fm!r} のマンデートが無い(fail-closed)"
            )
        ]
    reasons = []
    tags = set(ctx.proposal.universe_tags)
    if not tags:
        reasons.append(
            Reason("G-2", "block", "銘柄のユニバース分類タグが空(fail-closed)")
        )
    elif not tags & set(m.universe):
        reasons.append(
            Reason(
                "G-2",
                "block",
                f"銘柄分類 {sorted(tags)} は {m.fm} のユニバース {list(m.universe)} に無い",
            )
        )
    for prohibition in m.additional_prohibitions:
        if prohibition == "derivatives" and ctx.proposal.product in _DERIVATIVE_PRODUCTS:
            reasons.append(
                Reason("G-2", "block", f"{m.fm} の禁じ手: デリバティブ({ctx.proposal.product})")
            )
        elif prohibition == "short_selling" and ctx.proposal.side == "short":
            reasons.append(Reason("G-2", "block", f"{m.fm} の禁じ手: ショート"))
        elif prohibition == "margin" and ctx.proposal.is_margin:
            reasons.append(Reason("G-2", "block", f"{m.fm} の禁じ手: 信用取引"))
        elif prohibition == "single_name_equity" and ctx.proposal.is_single_name:
            reasons.append(Reason("G-2", "block", f"{m.fm} の禁じ手: 個別株"))
        elif (
            prohibition == "discretionary_trades_outside_signals"
            and not ctx.proposal.signal_ids
        ):
            reasons.append(
                Reason(
                    "G-2",
                    "block",
                    f"{m.fm} の禁じ手: シグナル外売買(signal_id 必須 — C-13)",
                )
            )
    return reasons


def _unit_lot_exception_applies(ctx: _Ctx) -> bool:
    """単元例外(IPS §7-1)が本注文に適用できるか。

    条件: 日本個別株の現物買い・約定後も1単元以内・1単元の取得価額が NAV の 35% 以下・
    信用買いでない(margin_buy_allowed=false)。
    """
    ule = ctx.ips.unit_lot_exception
    p = ctx.proposal
    if not (p.asset_class == "equity_jp" and p.is_single_name and p.side == "buy"):
        return False
    if p.is_margin and not ule.margin_buy_allowed:
        return False
    if p.unit_size is None or p.unit_size <= 0:
        return False
    if abs(ctx.post_fund_qty) > _dec(ule.max_units) * p.unit_size:
        return False
    unit_cost = p.unit_size * ctx.price
    assert ctx.state.nav is not None  # G-F 通過済み
    return unit_cost <= _dec(ule.unit_cost_nav_max) * ctx.state.nav


def _g3_concentration(ctx: _Ctx) -> list[Reason]:
    """G-3 発行体集中度: IPS(NAV の 20%)→ ポッド(仮想資本×マンデート上限)。"""
    assert ctx.state.nav is not None
    reasons = []
    post_fund_value = abs(ctx.post_fund_qty) * ctx.price
    fund_limit = _dec(ctx.ips.hard_limits.issuer_concentration_nav_max) * ctx.state.nav
    exceeds_fund = post_fund_value > fund_limit
    exception = exceeds_fund and _unit_lot_exception_applies(ctx)
    if exceeds_fund and not exception:
        reasons.append(
            Reason(
                "G-3",
                "block",
                f"発行体集中度: 約定後 ¥{post_fund_value:,.0f} > "
                f"NAV の {ctx.ips.hard_limits.issuer_concentration_nav_max:.0%}"
                f" = ¥{fund_limit:,.0f}(単元例外 不適用)",
            )
        )
    if ctx.mandate is not None:
        post_pod_value = abs(ctx.post_pod_qty) * ctx.price
        pod_limit = _dec(ctx.mandate.pod_concentration_limit) * _dec(ctx.mandate.capital_jpy)
        if post_pod_value > pod_limit and not _unit_lot_exception_applies(ctx):
            reasons.append(
                Reason(
                    "G-3",
                    "block",
                    f"ポッド内集中度({ctx.mandate.fm}): 約定後 ¥{post_pod_value:,.0f} > "
                    f"仮想資本の {ctx.mandate.pod_concentration_limit:.0%}"
                    f" = ¥{pod_limit:,.0f}",
                )
            )
    return reasons


def _g4_asset_class(ctx: _Ctx) -> list[Reason]:
    """G-4 資産クラス: 約定後の単一クラスグロス ≤ NAV の 70%(IPS §4.2)。"""
    assert ctx.state.nav is not None
    limit = _dec(ctx.ips.guardrails.single_asset_class_gross_nav_max) * ctx.state.nav
    cls = ctx.proposal.asset_class
    gross = _class_gross_post(ctx).get(cls, Decimal(0))
    if gross > limit:
        return [
            Reason(
                "G-4",
                "block",
                f"資産クラス {cls} のグロス ¥{gross:,.0f} > NAV の "
                f"{ctx.ips.guardrails.single_asset_class_gross_nav_max:.0%} = ¥{limit:,.0f}",
            )
        ]
    return []


def _g5_crypto(ctx: _Ctx) -> list[Reason]:
    """G-5 暗号資産: 休眠中は block。解除後も ≤ NAV の 5%(IPS §4.2)。"""
    if ctx.proposal.asset_class != "crypto":
        return []
    assert ctx.state.nav is not None
    if ctx.ips.guardrails.crypto_dormant:
        return [
            Reason(
                "G-5",
                "block",
                "暗号資産は休眠条項中(crypto_dormant — IPS §4.2)。"
                "有効化はマンデート改訂(ユーザー承認)による",
            )
        ]
    limit = _dec(ctx.ips.guardrails.crypto_nav_max) * ctx.state.nav
    gross = _class_gross_post(ctx).get("crypto", Decimal(0))
    if gross > limit:
        return [
            Reason(
                "G-5",
                "block",
                f"暗号資産グロス ¥{gross:,.0f} > NAV の "
                f"{ctx.ips.guardrails.crypto_nav_max:.0%} = ¥{limit:,.0f}",
            )
        ]
    return []


def _g6_cash_floor(ctx: _Ctx) -> list[Reason]:
    """G-6 現金下限: 約定後の現金 ≥ NAV の 5%(IPS §4.2)。現金が増える注文は除く。"""
    assert ctx.state.nav is not None and ctx.state.cash is not None
    post_cash = ctx.state.cash - ctx.delta * ctx.price
    if post_cash >= ctx.state.cash:  # 売り等で現金が増える注文は現金下限を悪化させない
        return []
    floor = _dec(ctx.ips.guardrails.cash_nav_min) * ctx.state.nav
    if post_cash < floor:
        return [
            Reason(
                "G-6",
                "block",
                f"約定後現金 ¥{post_cash:,.0f} < NAV の "
                f"{ctx.ips.guardrails.cash_nav_min:.0%} = ¥{floor:,.0f}(流動性・追証バッファ)",
            )
        ]
    return []


def _g7_turnover(ctx: _Ctx) -> list[Reason]:
    """G-7 売買代金: 当日累計+本注文 ≤ NAV の 30%(IPS §3.2 暴走ガード)。

    dd_soft(DD 15% ソフトリミット)中の新規建ては枠を半減して評価する(G-10 の解釈)。
    """
    assert ctx.state.nav is not None and ctx.state.daily_turnover is not None
    limit = _dec(ctx.ips.hard_limits.daily_turnover_nav_max) * ctx.state.nav
    halved = False
    if ctx.state.limits is not None and ctx.state.limits.dd_soft and ctx.new_build:
        limit /= 2
        halved = True
    total = ctx.state.daily_turnover + ctx.notional
    if total > limit:
        note = "(dd_soft により枠半減)" if halved else ""
        return [
            Reason(
                "G-7",
                "block",
                f"当日売買代金 ¥{total:,.0f} > 上限 ¥{limit:,.0f}{note}",
            )
        ]
    return []


def _g8_leverage(ctx: _Ctx) -> list[Reason]:
    """G-8 レバレッジ: 約定後グロス/NAV ≤ 2.0(IPS §3.2)+ポッド別上限(narrow only)。"""
    assert ctx.state.nav is not None
    reasons = []
    gross_total = sum(_class_gross_post(ctx).values(), Decimal(0))
    fund_max = _dec(ctx.ips.hard_limits.gross_leverage_max)
    if gross_total > fund_max * ctx.state.nav:
        reasons.append(
            Reason(
                "G-8",
                "block",
                f"約定後グロス ¥{gross_total:,.0f} が NAV×{fund_max}"
                f" = ¥{fund_max * ctx.state.nav:,.0f} 超",
            )
        )
    if ctx.mandate is not None:
        pod_gross = _pod_gross_post(ctx)
        pod_max = _dec(ctx.mandate.pod_gross_leverage_limit) * _dec(ctx.mandate.capital_jpy)
        if pod_gross > pod_max:
            reasons.append(
                Reason(
                    "G-8",
                    "block",
                    f"ポッド・グロス({ctx.mandate.fm}) ¥{pod_gross:,.0f} が仮想資本×"
                    f"{ctx.mandate.pod_gross_leverage_limit} = ¥{pod_max:,.0f} 超",
                )
            )
    return reasons


def _g9_short(ctx: _Ctx) -> list[Reason]:
    """G-9 ショート: IPS(許可+個別銘柄 NAV の 10%)→ マンデート(禁止/先物ヘッジ限定)。"""
    assert ctx.state.nav is not None
    # ショート性の判定は約定後の建玉方向で行う(sell の売り越しも捕捉する)。
    shorting = ctx.proposal.side == "short" or ctx.post_pod_qty < 0
    if not shorting:
        return []
    reasons = []
    if not ctx.ips.short_allowed:
        reasons.append(Reason("G-9", "block", "ショートは IPS で不許可"))
    if ctx.proposal.is_single_name and ctx.post_fund_qty < 0:
        short_value = abs(ctx.post_fund_qty) * ctx.price
        limit = _dec(ctx.ips.short_single_name_nav_max) * ctx.state.nav
        if short_value > limit:
            reasons.append(
                Reason(
                    "G-9",
                    "block",
                    f"個別銘柄ショート ¥{short_value:,.0f} > NAV の "
                    f"{ctx.ips.short_single_name_nav_max:.0%} = ¥{limit:,.0f}",
                )
            )
    m = ctx.mandate
    if m is not None:
        if m.short is False:
            reasons.append(Reason("G-9", "block", f"{m.fm} のマンデートはショート禁止"))
        elif isinstance(m.short, str) and m.short == "hedge_futures_only":
            if ctx.proposal.product not in _FUTURES_PRODUCTS:
                reasons.append(
                    Reason(
                        "G-9",
                        "block",
                        f"{m.fm} のショートは先物ヘッジのみ可({ctx.proposal.product} は不可)",
                    )
                )
    return reasons


def _g10_risk_state(ctx: _Ctx) -> list[Reason]:
    """G-10 リスク状態: dd_hard は全注文 block。vol/es は新規建て block。dd_soft は warn。"""
    limits = ctx.state.limits
    assert limits is not None  # G-F 通過済み
    reasons = []
    if limits.dd_hard:
        reasons.append(
            Reason("G-10", "block", "DD ハードリミット到達中(全新規発注停止 — IPS §3.2)")
        )
    if ctx.new_build:
        if limits.vol_exceeded:
            reasons.append(
                Reason("G-10", "block", "実現ボラ上限超過中(新規建てブロック — IPS §3.2)")
            )
        if limits.es_exceeded:
            reasons.append(
                Reason("G-10", "block", "日次 ES(95%)上限超過中(新規建てブロック — IPS §3.2)")
            )
        if limits.dd_soft:
            reasons.append(
                Reason(
                    "G-10",
                    "warn",
                    "DD ソフトリミット中の新規建て(枠半減で評価 — G-7)",
                )
            )
    return reasons


# ── 入口 ──────────────────────────────────────────────────────────────────────
def _missing_inputs(proposal: OrderProposal, state: PortfolioState, ips: IPSConfig) -> list[str]:
    """fail-closed: 判定に必要な入力の欠落を列挙する。"""
    missing = []
    if state.trading_state is None:
        missing.append("trading_state")
    if state.nav is None or state.nav <= 0:
        missing.append("nav")
    if state.cash is None:
        missing.append("cash")
    if state.positions is None:
        missing.append("positions")
    if state.daily_turnover is None:
        missing.append("daily_turnover")
    if state.limits is None:
        missing.append("risk.limits_state")
    price = proposal.limit_price if proposal.order_type == "limit" else proposal.ref_price
    if price is None or price <= 0:
        missing.append("ref_price")
    if proposal.book_id not in ips.books:
        missing.append(f"book_id({proposal.book_id} は IPS 対象帳簿に無い)")
    if proposal.asset_class not in ips.asset_classes:
        missing.append(f"asset_class({proposal.asset_class!r} はタクソノミーに無い)")
    return missing


def evaluate(
    proposal: OrderProposal,
    state: PortfolioState,
    ips: IPSConfig,
    mandates: Mapping[str, Mandate],
) -> GateResult:
    """ゲート判定(純決定論)。判定順序は IPS → マンデート(定款第4条)。

    G-F(入力完全性)で欠落があれば以降の規則は評価せず block を返す(fail-closed)。
    """
    checked: list[str] = ["G-F"]
    missing = _missing_inputs(proposal, state, ips)
    if missing:
        reasons = tuple(
            Reason("G-F", "block", f"入力不足(fail-closed): {name}") for name in missing
        )
        return GateResult("block", reasons, tuple(checked))

    price = proposal.limit_price if proposal.order_type == "limit" else proposal.ref_price
    assert price is not None
    delta = proposal.qty if proposal.side in ("buy", "cover") else -proposal.qty
    positions = state.positions or ()
    pre_pod = sum(
        (
            pos.qty
            for pos in positions
            if pos.fm == proposal.fm and pos.instrument_id == proposal.instrument_id
        ),
        Decimal(0),
    )
    pre_fund = sum(
        (pos.qty for pos in positions if pos.instrument_id == proposal.instrument_id),
        Decimal(0),
    )
    ctx = _Ctx(
        proposal=proposal,
        state=state,
        ips=ips,
        mandate=mandates.get(proposal.fm),
        price=price,
        delta=delta,
        pre_pod_qty=pre_pod,
        post_pod_qty=pre_pod + delta,
        pre_fund_qty=pre_fund,
        post_fund_qty=pre_fund + delta,
        notional=proposal.qty * price,
    )

    rules = (
        ("G-0", _g0_trading_state),
        ("G-1", _g1_products),
        ("G-2", _g2_mandate_universe),
        ("G-3", _g3_concentration),
        ("G-4", _g4_asset_class),
        ("G-5", _g5_crypto),
        ("G-6", _g6_cash_floor),
        ("G-7", _g7_turnover),
        ("G-8", _g8_leverage),
        ("G-9", _g9_short),
        ("G-10", _g10_risk_state),
    )
    reasons: list[Reason] = []
    for rule_id, fn in rules:
        checked.append(rule_id)
        reasons.extend(fn(ctx))

    if any(r.severity == "block" for r in reasons):
        verdict = "block"
    elif reasons:
        verdict = "warn"
    else:
        verdict = "pass"
    return GateResult(verdict, tuple(reasons), tuple(checked))


__all__ = [
    "GateResult",
    "LimitsState",
    "OrderProposal",
    "PortfolioState",
    "PositionState",
    "Reason",
    "evaluate",
    "mandates_hash",
]
