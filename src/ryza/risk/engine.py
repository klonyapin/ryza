"""engine — リスク計算の純関数群(T-015。保護領域 — 定款第5条)。

入力は「NAV 系列(フロー付き)・ポジション評価額・銘柄リターン系列・``IPSConfig``」、
出力は ``RiskState``(測定値+フラグ)。DB 非依存・LLM 不関与・純決定論。判定値は
すべて ``config/ips.yaml`` の発効値から取る(ハードコード禁止)。

測定定義(IPS v1.3):

- **DD**(§3.1 ``drawdown_definition``): 帳簿単位・設定来ピーク・連続測定。
  ``dd = (peak − nav) / peak``。データ1日目から有効。フラグは「到達」(≥)で立てる
  (ips.yaml §3.2「DD 15% **到達時**」— 指示書の「超」表記との差異は境界1点のみで、
  安全側 = 早く立つ方を採る)
- **実現ボラ**(§3.2): 帳簿日次リターンの平均ゼロ EWMA(RiskMetrics 流儀)。
  平滑係数は span 規約 ``α = 2/(N+1)``(N = ``realized_vol_ewma_days``)、初期値は
  最初のリターンの2乗、年率換算は √252。上限「超」(>)で vol_exceeded
- **日次 ES95**(§3.2・00 §9): ヒストリカル法(現在ポジションウェイトを過去の銘柄
  日次リターンに適用した系列の下位 5% 平均)+パラメトリック併算(平均ゼロ正規仮定:
  ``ES = σ·φ(z95)/0.05``)。**大きい方を採用**し、NAV 比上限「超」で es_exceeded。
  ポジションが無い間は 0
- **フロー調整**: リターンは ``r_t = (nav_t − flow_t − nav_{t−1}) / nav_{t−1}``。
  出資・払戻(外部フロー)を損益と混同しない。DD は素の NAV 系列で測る(§3.1 の
  文言どおり「帳簿 NAV 系列」。出資でピークが上がるのは保守側)
- **データ不足時は fail-safe**(指示書): 帳簿リターン系列が N(=20)営業日に満たない
  間は vol/es フラグを立てず、``notes`` に「データ不足 n/N営業日」を明記する。
  DD はデータ1日目から有効
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ryza.ips import IPSConfig

# 年率換算の営業日数(慣行値)。IPS の判定値ではなく換算規約。
TRADING_DAYS = 252

# 標準正規の z_{0.95} とその密度 φ(z)(数学定数 — IPS 値ではない)。
_Z95 = 1.6448536269514722
_PHI_Z95 = math.exp(-_Z95 * _Z95 / 2) / math.sqrt(2 * math.pi)
_ALPHA = 0.05  # ES95 のテイル確率(「ES(95%)」の定義そのもの)


@dataclass(frozen=True)
class NavPoint:
    """NAV 系列の1点。``net_flow`` はその日の外部フロー(出資+・払戻−、JPY)。"""

    day: date
    nav: Decimal
    net_flow: Decimal = Decimal(0)


@dataclass(frozen=True)
class RiskPosition:
    """ポジション評価額1件(ES・ガードレール入力)。value は符号付き JPY 時価総額。"""

    instrument_id: int
    asset_class: str
    value: Decimal


@dataclass(frozen=True)
class ESResult:
    """日次 ES(95%)の計算結果(NAV 比・正の値=損失側)。"""

    historical: float | None  # ヒストリカル法(観測不足なら None)
    parametric: float | None  # パラメトリック併算
    adopted: float  # 採用値 = max(両者)。ポジション無し/観測ゼロは 0
    n_obs: int  # ポートフォリオ・リターンの観測数


@dataclass(frozen=True)
class RiskState:
    """リスクエンジンの出力(測定値+フラグ)。``risk.limits_state`` 更新の入力。"""

    as_of_day: date
    nav: Decimal
    peak_nav: Decimal
    drawdown: Decimal  # 0..1
    n_returns: int  # 帳簿日次リターンの観測数
    sufficient: bool  # n_returns ≥ EWMA 日数(vol/es フラグの有効条件)
    ewma_vol_annual: float | None  # リターンゼロ件なら None
    es95: ESResult
    dd_soft: bool
    dd_hard: bool  # 測定値。DB 側のラッチ(非自動解除)は state.py の管轄
    vol_exceeded: bool
    es_exceeded: bool
    notes: tuple[str, ...]  # データ不足・除外銘柄などの明記(fail-safe の説明責任)


def book_returns(series: Sequence[NavPoint]) -> list[float]:
    """帳簿日次リターン(外部フロー調整済み)。``r_t = (nav_t − flow_t − nav_{t−1}) / nav_{t−1}``。"""
    returns: list[float] = []
    for prev, cur in zip(series, series[1:], strict=False):
        if prev.nav > 0:
            returns.append(float((cur.nav - cur.net_flow - prev.nav) / prev.nav))
    return returns


def drawdown(series: Sequence[NavPoint]) -> tuple[Decimal, Decimal]:
    """現在 DD と設定来ピーク。``(dd, peak_nav)``。系列は日付昇順であること。"""
    if not series:
        raise ValueError("NAV 系列が空(DD は測定できない)")
    peak = max(p.nav for p in series)
    last = series[-1].nav
    if peak <= 0:
        return Decimal(0), peak
    return (peak - last) / peak, peak


def ewma_vol(
    returns: Sequence[float], *, days: int, trading_days: int = TRADING_DAYS
) -> float | None:
    """平均ゼロ EWMA 実現ボラの年率換算。リターンゼロ件なら None。

    ``α = 2/(days+1)``(span 規約)、``σ²_1 = r_1²``、``σ²_t = (1−α)σ²_{t−1} + α r_t²``。
    """
    if not returns:
        return None
    alpha = 2.0 / (days + 1)
    var = returns[0] * returns[0]
    for r in returns[1:]:
        var = (1 - alpha) * var + alpha * r * r
    return math.sqrt(var * trading_days)


def es95(
    positions: Sequence[RiskPosition],
    nav: Decimal,
    instrument_returns: Mapping[int, Mapping[date, float]],
) -> ESResult:
    """日次 ES(95%)を NAV 比で返す(ヒストリカル+パラメトリック併算、大きい方を採用)。

    現在ポジションのウェイト(value/NAV・符号付き)を、**全保有銘柄のリターンが揃う日**
    だけで構成したポートフォリオ・リターン系列に適用する(欠測日の混入で分散を歪めない)。
    ポジションが無い間は 0(指示書)。
    """
    if nav <= 0:
        return ESResult(None, None, 0.0, 0)
    weights: dict[int, float] = {}
    for pos in positions:
        if pos.value != 0:
            weights[pos.instrument_id] = weights.get(pos.instrument_id, 0.0) + float(
                pos.value / nav
            )
    if not weights:
        return ESResult(None, None, 0.0, 0)

    series_by_id = {i: instrument_returns.get(i, {}) for i in weights}
    common_days: set[date] | None = None
    for rets in series_by_id.values():
        days_set = set(rets)
        common_days = days_set if common_days is None else common_days & days_set
    port: list[float] = []
    for d in sorted(common_days or ()):
        port.append(sum(w * series_by_id[i][d] for i, w in weights.items()))
    n = len(port)
    if n == 0:
        return ESResult(None, None, 0.0, 0)

    # ヒストリカル: 下位 5% テイル(最低1観測)の平均損失。
    k = max(1, int(n * _ALPHA))
    tail = sorted(port)[:k]
    hist = max(0.0, -sum(tail) / k)
    # パラメトリック: 平均ゼロ正規仮定 ES = σ·φ(z95)/α(母標準偏差)。
    mean = sum(port) / n
    var = sum((r - mean) ** 2 for r in port) / n
    param = math.sqrt(var) * _PHI_Z95 / _ALPHA
    return ESResult(hist, param, max(hist, param), n)


def evaluate(
    series: Sequence[NavPoint],
    positions: Sequence[RiskPosition],
    instrument_returns: Mapping[int, Mapping[date, float]],
    ips: IPSConfig,
    *,
    extra_notes: Sequence[str] = (),
) -> RiskState:
    """リスク状態を測定してフラグに変換する(純関数)。

    - DD フラグはデータ1日目から有効(到達 ≥ で発動)
    - vol/es フラグは帳簿リターンが ``realized_vol_ewma_days`` 営業日そろうまで
      立てない(fail-safe)。その間は ``notes`` にデータ不足を明記する
    """
    hl = ips.hard_limits
    dd, peak = drawdown(series)
    returns = book_returns(series)
    n = len(returns)
    days = hl.realized_vol_ewma_days
    sufficient = n >= days
    vol = ewma_vol(returns, days=days)
    es = es95(positions, series[-1].nav, instrument_returns)

    notes = list(extra_notes)
    if not sufficient:
        notes.append(f"データ不足 {n}/{days}営業日 — 実現ボラ・ES フラグは判定保留(fail-safe)")
    if es.n_obs and es.n_obs < days:
        notes.append(f"ES 観測 {es.n_obs}日 < {days}営業日 — ES フラグは判定保留(fail-safe)")

    dd_soft = dd >= Decimal(str(hl.dd_soft_limit))
    dd_hard = dd >= Decimal(str(hl.dd_hard_limit))
    vol_exceeded = sufficient and vol is not None and vol > hl.realized_vol_limit
    es_exceeded = (
        sufficient and es.n_obs >= days and es.adopted > hl.daily_es95_nav_max
    )
    return RiskState(
        as_of_day=series[-1].day,
        nav=series[-1].nav,
        peak_nav=peak,
        drawdown=dd,
        n_returns=n,
        sufficient=sufficient,
        ewma_vol_annual=vol,
        es95=es,
        dd_soft=dd_soft,
        dd_hard=dd_hard,
        vol_exceeded=vol_exceeded,
        es_exceeded=es_exceeded,
        notes=tuple(notes),
    )


def guardrail_usage(
    positions: Sequence[RiskPosition],
    nav: Decimal,
    cash: Decimal | None,
    ips: IPSConfig,
) -> dict[str, dict[str, float | str | None]]:
    """ガードレール消費率(現在値/上限 — 日次リスクレポート用。判定はゲートの管轄)。

    返り値は ``{名前: {value, limit, usage}}``。usage は上限に対する消費率(0..∞)。
    現金下限のみ「下限」なので usage = 下限/現在値(現在値が下限に近いほど 1 に近づく)。
    """
    if nav <= 0:
        return {}
    hl, gr = ips.hard_limits, ips.guardrails
    by_issuer: dict[int, Decimal] = {}
    by_class: dict[str, Decimal] = {}
    gross = Decimal(0)
    for pos in positions:
        by_issuer[pos.instrument_id] = by_issuer.get(pos.instrument_id, Decimal(0)) + abs(
            pos.value
        )
        by_class[pos.asset_class] = by_class.get(pos.asset_class, Decimal(0)) + abs(pos.value)
        gross += abs(pos.value)

    top_issuer = max(by_issuer.values(), default=Decimal(0))
    top_class_name, top_class_value = max(
        by_class.items(), key=lambda kv: kv[1], default=("-", Decimal(0))
    )
    usage: dict[str, dict[str, float | str | None]] = {
        "issuer_concentration": {
            "value": float(top_issuer / nav),
            "limit": hl.issuer_concentration_nav_max,
            "usage": float(top_issuer / nav) / hl.issuer_concentration_nav_max,
        },
        "single_asset_class_gross": {
            "value": float(top_class_value / nav),
            "limit": gr.single_asset_class_gross_nav_max,
            "usage": float(top_class_value / nav) / gr.single_asset_class_gross_nav_max,
            "class": top_class_name,
        },
        "gross_leverage": {
            "value": float(gross / nav),
            "limit": hl.gross_leverage_max,
            "usage": float(gross / nav) / hl.gross_leverage_max,
        },
        "cash_floor": {
            "value": None if cash is None else float(cash / nav),
            "limit": gr.cash_nav_min,
            "usage": (
                None
                if cash is None or cash <= 0
                else gr.cash_nav_min / float(cash / nav)
            ),
        },
    }
    return usage


__all__ = [
    "ESResult",
    "NavPoint",
    "RiskPosition",
    "RiskState",
    "TRADING_DAYS",
    "book_returns",
    "drawdown",
    "es95",
    "evaluate",
    "ewma_vol",
    "guardrail_usage",
]
