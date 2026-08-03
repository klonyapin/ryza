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
- **フロー調整**(BOP/EOP 分離 — 独立審査 2026-08-03 重要-1):
  ``r_t = (nav_t − flow_eop_t) / (nav_{t−1} + flow_bop_t) − 1``。
  ``flow_eop_t`` は測定日当日(``snap_date`` と同日)の外部フロー、``flow_bop_t`` は
  前の測定日より後・当日より前に入った外部フロー(``ryza.risk.navflow`` が
  ロールフォワードした分)。期中に入った資金は**その区間の運用元本になっている**ため
  分母に入れる。期末フロー一律仮定(分子から引くだけ)だと区間リターンが
  ``(1 + flow/nav_{t−1})`` 倍に増幅され、資本形成期に誤 ``vol_exceeded`` を招く
  (審査の手計算: V₀=100万・期中+50万・市場+5% → 期末仮定 +7.5% / 真値 +5.0%)。
  フローが無い日は従来式に退化する。出資・払戻(外部フロー)を損益と混同しない。
  DD は素の NAV 系列で測る(§3.1 の文言どおり「帳簿 NAV 系列」。出資でピークが
  上がるのは保守側)
- **データ不足時は fail-safe**(指示書): 帳簿リターン系列が N(=20)営業日に満たない
  間は vol/es フラグを立てず、``notes`` に「データ不足 n/N営業日」を明記する。
  DD はデータ1日目から有効
- **保留・除外は機械可読で残す**(独立役員審査 T-018 重大-3 の恒久対応):
  ``notes`` は人向けの日本語文であり、読み手側で「どの指標がなぜ判定保留か」を
  復元できない。``RiskState.deferred``(``Deferral`` の列)と ``RiskState.excluded``
  (``Exclusion`` の列)が同じ事実を構造化して持ち、``risk.limits_state_events.metrics``
  経由でダッシュボードと監査が同じ理由を読む。``notes`` はこれらの表示形であって
  正ではない
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
    """NAV 系列の1点(外部フローは出資+・払戻−、JPY)。

    フローを2つに分けて持つのは、区間リターンの分母(運用元本)と分子(期末残高)で
    扱いが違うため(モジュール docstring「フロー調整」):

    - ``flow_eop``: 測定日**当日**の仕訳。当日の運用には乗っていないので分子から引く
    - ``flow_bop``: 前の測定日より後・当日より前の仕訳(締めが走らない休日などの分。
      ``ryza.risk.navflow`` がロールフォワードして付ける)。区間の運用元本なので分母に足す
    """

    day: date
    nav: Decimal
    flow_eop: Decimal = Decimal(0)
    flow_bop: Decimal = Decimal(0)

    @property
    def net_flow(self) -> Decimal:
        """その点に帰属する外部フロー純額(表示用。測定は BOP/EOP を分けて使う)。"""
        return self.flow_eop + self.flow_bop


@dataclass(frozen=True)
class RiskPosition:
    """ポジション評価額1件(ES・ガードレール入力)。value は符号付き JPY 時価総額。"""

    instrument_id: int
    asset_class: str
    value: Decimal


@dataclass(frozen=True)
class ESResult:
    """日次 ES(95%)の計算結果(NAV 比・正の値=損失側)。

    測定空白の縮退(独立役員審査 2026-08-03 条件2): リターン系列が ``min_obs`` に
    満たない銘柄は測定から**除外して残部で測定**し ``excluded`` に列挙する(1銘柄の
    データ不足で全体が判定保留化するのを防ぐ)。判定を保留したときは
    ``deferral_reason`` に**計算した本人が**理由コードを入れる(``deferred`` はその
    有無を見るだけの派生値)。

    理由を呼び出し側で推測させない(独立役員審査 2026-08-04 重大-1): 旧実装は
    ``deferred``(bool)しか返さず、``evaluate`` が「除外銘柄に観測があるか」という
    代理指標から理由を当てていたため、**過半でないのに ``majority_excluded``** と
    記録される系統的な誤りがあった(3 銘柄保有・1 銘柄が短系列・残る 2 銘柄の共通
    観測日がゼロ、など)。理由は分岐を持つ本人しか正しく言えない。
    """

    historical: float | None  # ヒストリカル法(観測不足なら None)
    parametric: float | None  # パラメトリック併算
    adopted: float  # 採用値 = max(両者)。ポジション無し/観測ゼロは 0
    n_obs: int  # ポートフォリオ・リターンの観測数
    excluded: tuple[int, ...] = ()  # 短系列のため測定から除外した instrument_id
    #: 判定保留の理由コード(REASON_*)。保留していなければ None。
    deferral_reason: str | None = None

    @property
    def deferred(self) -> bool:
        """判定保留か(理由コードの有無 — フラグは立てず urgent 注記に回る)。"""
        return self.deferral_reason is not None


# 判定保留の理由コード(``Deferral.reason``)。値は機械可読の識別子で、表示文言は
# 読み手側(ダッシュボード・レポート)が持つ。
REASON_INSUFFICIENT_RETURNS = "insufficient_returns"  # 帳簿リターンが N 営業日に満たない
REASON_INSUFFICIENT_OBS = "insufficient_obs"  # ES のポートフォリオ観測が N 日に満たない
REASON_NO_OBSERVATIONS = "no_observations"  # 保有はあるがリターン観測が 1 件も無い
REASON_MAJORITY_EXCLUDED = "majority_excluded"  # 除外銘柄が保有の過半(全除外を含む)
REASON_NO_COMMON_DAYS = "no_common_days"  # 測定対象は残ったが共通観測日がゼロ

# 測定から銘柄を外した理由(``Exclusion.reason``)。
REASON_SHORT_SERIES = "short_series"  # リターン系列が min_obs 未満(ES の縮退)
REASON_MISSING_PRICE = "missing_price"  # 時価が取れず評価自体ができない(daily 側)

# 保留・除外が及ぶ測定の識別子(``Deferral.metric`` / ``Exclusion.measure``)。
METRIC_REALIZED_VOL = "realized_vol"
METRIC_ES95 = "es95"
MEASURE_VALUATION = "valuation"  # 時価評価(= ES とガードレールの入力そのもの)


@dataclass(frozen=True)
class Deferral:
    """判定を保留した測定と、その理由(機械可読 — 独立役員審査 T-018 重大-3)。

    ``notes`` の日本語文と同じ事実を、読み手が分岐に使える形で持つ。1 つの測定に
    複数の理由が並ぶことがある(例: 帳簿リターン不足 **かつ** ES 観測ゼロ)。
    ``observed`` / ``required`` は判定に必要な観測数と実績(``0`` 起点の件数)。
    """

    metric: str  # METRIC_* のいずれか
    reason: str  # REASON_* のいずれか
    observed: int
    required: int


@dataclass(frozen=True)
class Exclusion:
    """測定から外した銘柄とその理由(採用値が「何を含まないか」の説明責任)。

    ES は観測不足以外の理由(時価欠測による評価除外など)でも採用値が動くため、
    除外の事実を ``sufficient``(bool)に潰さず銘柄単位で残す。``observed`` /
    ``required`` は短系列除外のときだけ意味を持つ(それ以外は None)。
    """

    instrument_id: int
    measure: str  # METRIC_ES95 / MEASURE_VALUATION
    reason: str  # REASON_*
    observed: int | None = None
    required: int | None = None


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
    #: 判定を保留した測定とその理由(``notes`` の機械可読版)。空なら全測定が有効。
    deferred: tuple[Deferral, ...] = ()
    #: 測定から外した銘柄とその理由(ES の短系列除外+呼び出し側の評価除外)。
    excluded: tuple[Exclusion, ...] = ()

    def deferred_metrics(self) -> frozenset[str]:
        """判定保留になっている測定名の集合(``METRIC_*``)。"""
        return frozenset(d.metric for d in self.deferred)


def book_returns(series: Sequence[NavPoint]) -> list[float]:
    """帳簿日次リターン(外部フロー調整済み TWR)。

    ``r_t = (nav_t − flow_eop_t) / (nav_{t−1} + flow_bop_t) − 1``(BOP/EOP 分離)。
    分母(運用元本)が 0 以下になる区間は測定できないので除外する — 全額払戻後に
    再出資した区間は ``nav_{t−1} = 0`` でも ``flow_bop > 0`` なら測れる。
    """
    returns: list[float] = []
    for prev, cur in zip(series, series[1:], strict=False):
        base = prev.nav + cur.flow_bop
        if base > 0:
            returns.append(float((cur.nav - cur.flow_eop) / base) - 1.0)
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
    *,
    min_obs: int,
) -> ESResult:
    """日次 ES(95%)を NAV 比で返す(ヒストリカル+パラメトリック併算、大きい方を採用)。

    現在ポジションのウェイト(value/NAV・符号付き)を、**測定対象銘柄のリターンが揃う日**
    だけで構成したポートフォリオ・リターン系列に適用する(欠測日の混入で分散を歪めない)。
    リターン系列が ``min_obs`` 未満の銘柄は除外して残部で測定する(縮退 — ``ESResult``
    docstring)。除外分のエクスポージャーは測定に含まれない(過小方向)ため、除外は
    必ず注記され、残部で測れないときは判定保留にして ``deferral_reason`` に理由を返す:

    - ``no_observations``: 保有銘柄のリターンが 1 件も無い(データが届いていない)
    - ``majority_excluded``: 除外が保有の過半(全除外を含む — 残部が無い場合もこれ)
    - ``no_common_days``: 測定対象銘柄は残ったが**共通観測日がゼロ**(取引所休日の
      ずれ・上場直後など)。残部は存在するのに系列が組めない別事象

    ポジションが無い間は 0(指示書)— これは保留ではない(測るべき保有が無い)。
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

    # 縮退: 短系列(min_obs 未満)の銘柄は除外して残部で測定する(審査条件2)。
    included = {
        i: instrument_returns.get(i, {})
        for i in weights
        if len(instrument_returns.get(i, {})) >= min_obs
    }
    excluded = tuple(sorted(set(weights) - set(included)))
    majority_excluded = len(excluded) > len(weights) / 2
    common_days: set[date] | None = None
    for rets in included.values():
        days_set = set(rets)
        common_days = days_set if common_days is None else common_days & days_set
    port: list[float] = []
    for d in sorted(common_days or ()):
        port.append(sum(w * included[i][d] for i, w in weights.items() if i in included))
    n = len(port)
    if n == 0:
        # 保有があるのに観測ゼロ = 測定空白。値 0 を「リスクなし」と読ませない(判定保留)。
        # 理由は 3 通りに分かれ、ここでしか正しく区別できない(重大-1)。
        if included:
            reason = REASON_NO_COMMON_DAYS  # 残部はあるが日付が揃わない
        elif any(instrument_returns.get(i) for i in weights):
            reason = REASON_MAJORITY_EXCLUDED  # 全銘柄が短系列で落ちた(⊃ 過半)
        else:
            reason = REASON_NO_OBSERVATIONS  # そもそもリターンが 1 件も無い
        return ESResult(None, None, 0.0, 0, excluded=excluded, deferral_reason=reason)

    # ヒストリカル: 下位 5% テイル(最低1観測)の平均損失。
    k = max(1, int(n * _ALPHA))
    tail = sorted(port)[:k]
    hist = max(0.0, -sum(tail) / k)
    # パラメトリック: 平均ゼロ正規仮定 ES = σ·φ(z95)/α(母標準偏差)。
    mean = sum(port) / n
    var = sum((r - mean) ** 2 for r in port) / n
    param = math.sqrt(var) * _PHI_Z95 / _ALPHA
    return ESResult(
        hist, param, max(hist, param), n,
        excluded=excluded,
        deferral_reason=REASON_MAJORITY_EXCLUDED if majority_excluded else None,
    )


def _es_deferral_note(reason: str, n_obs: int) -> str:
    """ES 判定保留の注記文(理由コード → 人向けの説明)。

    ``majority_excluded`` だけは残部の有無で言い分けが変わる: 全銘柄が落ちた日に
    「残部の測定値は参考値」と書くと、存在しない残部を読み手に想像させる
    (独立役員審査 2026-08-04 重大-1 の再現ケース)。
    """
    if reason == REASON_NO_OBSERVATIONS:
        return "【要確認】ES 測定不能(保有ありだがリターン系列なし)— 判定保留"
    if reason == REASON_NO_COMMON_DAYS:
        return (
            "【要確認】ES: 測定対象銘柄の共通観測日がゼロのため測定不能 — 判定保留"
            "(残部は存在しない)"
        )
    if reason == REASON_MAJORITY_EXCLUDED and n_obs == 0:
        return "【要確認】ES: 保有銘柄がすべて除外され測定対象なし — 判定保留"
    return "【要確認】ES: 除外銘柄が過半のため判定保留(残部の測定値は参考値)"


def evaluate(
    series: Sequence[NavPoint],
    positions: Sequence[RiskPosition],
    instrument_returns: Mapping[int, Mapping[date, float]],
    ips: IPSConfig,
    *,
    extra_notes: Sequence[str] = (),
    extra_exclusions: Sequence[Exclusion] = (),
) -> RiskState:
    """リスク状態を測定してフラグに変換する(純関数)。

    - DD フラグはデータ1日目から有効(到達 ≥ で発動)
    - vol/es フラグは帳簿リターンが ``realized_vol_ewma_days`` 営業日そろうまで
      立てない(fail-safe)。その間は ``notes`` にデータ不足を明記し、同じ事実を
      ``deferred``(機械可読)に入れる
    - ``extra_exclusions`` は**エンジンに届く前に**測定から外れた銘柄(時価欠測で
      評価できなかった保有など)。エンジン自身の短系列除外と同じ列に載せて、
      採用値が何を含まないかを 1 か所で読めるようにする
    """
    hl = ips.hard_limits
    dd, peak = drawdown(series)
    returns = book_returns(series)
    n = len(returns)
    days = hl.realized_vol_ewma_days
    sufficient = n >= days
    vol = ewma_vol(returns, days=days)
    es = es95(positions, series[-1].nav, instrument_returns, min_obs=days)

    notes = list(extra_notes)
    deferred: list[Deferral] = []
    excluded = list(extra_exclusions) + [
        Exclusion(
            instrument_id=i,
            measure=METRIC_ES95,
            reason=REASON_SHORT_SERIES,
            observed=len(instrument_returns.get(i, {})),
            required=days,
        )
        for i in es.excluded
    ]
    if not sufficient:
        notes.append(f"データ不足 {n}/{days}営業日 — 実現ボラ・ES フラグは判定保留(fail-safe)")
        # 帳簿リターン不足は vol と ES の**両方**の判定を止める(es_exceeded も
        # sufficient を条件にしている)。読み手が指標ごとに分岐できるよう 2 件に分ける。
        deferred += [
            Deferral(METRIC_REALIZED_VOL, REASON_INSUFFICIENT_RETURNS, n, days),
            Deferral(METRIC_ES95, REASON_INSUFFICIENT_RETURNS, n, days),
        ]
    if es.excluded:
        notes.append(
            f"ES: 短系列(<{days}営業日)のため測定から除外: "
            f"instruments {list(es.excluded)}(残部で測定 — 除外分は過小方向)"
        )
    if es.deferral_reason is not None:
        # 保有があるのに測れない — 判定保留は urgent 注記(審査条件2)。理由は
        # ``es95()`` が返したものをそのまま使う(推測しない — 重大-1)。注記文だけは
        # 「残部が存在するか」で言い分けが変わるため、ここで理由コードから引く。
        notes.append(_es_deferral_note(es.deferral_reason, es.n_obs))
        deferred.append(Deferral(METRIC_ES95, es.deferral_reason, es.n_obs, days))
    elif es.n_obs and es.n_obs < days:
        notes.append(f"ES 観測 {es.n_obs}日 < {days}営業日 — ES フラグは判定保留(fail-safe)")
        deferred.append(Deferral(METRIC_ES95, REASON_INSUFFICIENT_OBS, es.n_obs, days))

    dd_soft = dd >= Decimal(str(hl.dd_soft_limit))
    dd_hard = dd >= Decimal(str(hl.dd_hard_limit))
    vol_exceeded = sufficient and vol is not None and vol > hl.realized_vol_limit
    es_exceeded = (
        sufficient
        and not es.deferred
        and es.n_obs >= days
        and es.adopted > hl.daily_es95_nav_max
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
        deferred=tuple(deferred),
        excluded=tuple(excluded),
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
    "Deferral",
    "Exclusion",
    "METRIC_ES95",
    "METRIC_REALIZED_VOL",
    "MEASURE_VALUATION",
    "NavPoint",
    "REASON_INSUFFICIENT_OBS",
    "REASON_INSUFFICIENT_RETURNS",
    "REASON_MAJORITY_EXCLUDED",
    "REASON_NO_COMMON_DAYS",
    "REASON_MISSING_PRICE",
    "REASON_NO_OBSERVATIONS",
    "REASON_SHORT_SERIES",
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
