"""ips — IPS v1.3 機械可読版(``config/ips.yaml``)のローダとマンデート整合検証(Issue #28)。

**保護領域**(CLAUDE.md 不変原則6): IPS の変更はユーザー起票のみ。本モジュールは
docs/design/80-ips.md(正)から生成された ``config/ips.yaml`` を読み、コンプライアンス・
リスク両エンジンが参照する唯一の口となる(80-ips.md ステータス欄「機械可読版 config/ips.yaml を
生成し、コンプライアンス・リスク両エンジンはそれのみを参照する」)。

責務:

- **ローダ**: YAML → frozen dataclass。``research/providers.py`` の ``LLMConfig.load`` と同じ流儀
  (classmethod ``load`` + 既定パスは ``__file__`` 相対)。
- **値域検証**: 比率は (0,1] 等の値域チェックに加え、80-ips.md 本文から導出できる不変条件
  (実現ボラ上限 = σ予算+3pp、DD ソフト<ハード、σ予算 ≤ 上限 12% など)を ``load`` 時に検査し、
  違反は ``IPSValidationError`` で即座に露見させる。
- **マンデート整合検証**: ``config/mandates/*.yaml`` が IPS を「緩める」方向の値を持てないことの
  チェック(80-ips.md 冒頭「マンデートが IPS を緩める方向の記述を持つことはスキーマで禁止する」、
  81-fm-mandates.md §2-4)。違反は ``MandateViolationError`` に全件列挙する。

マンデート整合検証の根拠(81-fm-mandates.md §3 の注記に対応):

1. ポッド DD 上限 ≤ ファンド DD 上限(25%)— DD をファンドより緩くはできない。
2. グロスレバの資本加重和 ≤ ファンド上限 2.0x(81 §3: 0.2×(1.0+1.5+3.0+1.0)=1.3x < 2.0x)。
3. ポッド仮想資本の合計 ≤ ファンド出資金(81 §3: 各¥200万×4+中央リザーブ¥200万=¥1,000万)。
4. ポッド内集中度の絶対額(集中度×仮想資本)≤ ファンド集中度の絶対額(20%×NAV)—
   分母が違う(81 §3: 仮想資本の40% = NAV の 8%)ため絶対額で比較する。
5. 暗号資産休眠条項(IPS §4.2): 休眠中はマンデートのユニバースに暗号資産を含められない。

**σ配分の合計はハード検証しない**: 81 §3 が「相関1の最悪ケースで 13.4% とファンド σ予算 12% を
わずかに超えるが、哲学の直交性による低相関を前提とした配分であり、最終防衛線はファンドレベルの
実現ボラ上限(IPS §3.2 の EWMA ゲート)が担う」と明記して承認済みのため、素朴な合計 ≤ σ予算の
ハードチェックは承認済み設定を却下してしまう。算出値は ``worst_case_sigma`` で公開し、
実行時の防衛は実現ボラゲート(リスクエンジン実装時)が担う。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_IPS_PATH = _ROOT / "config" / "ips.yaml"
_MANDATES_DIR = _ROOT / "config" / "mandates"

# 80-ips.md §3.2「実現ボラティリティ上限 = σ予算(12%)+3pp」の 3pp。
_REALIZED_VOL_MARGIN = 0.03
_EPS = 1e-9


class IPSValidationError(ValueError):
    """``config/ips.yaml`` の値が 80-ips.md の値域・不変条件に反するときに送出する。"""


class MandateViolationError(ValueError):
    """マンデートが IPS を「緩める」方向の値を持つときに送出する(違反を全件列挙)。"""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        joined = "\n".join(f"- {v}" for v in violations)
        super().__init__(f"マンデートが IPS を緩めています({len(violations)}件):\n{joined}")


def _require(cond: bool, message: str) -> None:  # noqa: FBT001
    if not cond:
        raise IPSValidationError(message)


def _fraction(value: Any, name: str) -> float:
    """(0,1] の比率として検証して float を返す。"""
    v = float(value)
    _require(0.0 < v <= 1.0, f"{name} は (0,1] であるべき: {v}")
    return v


# ── dataclass 群(すべて frozen)──────────────────────────────────────────────
@dataclass(frozen=True)
class DrawdownDefinition:
    """DD の測定定義(§3.1・独立レビュー条件1)+3年連続条項。"""

    basis: str  # per_book(帳簿単位)
    peak: str  # since_inception(設定来ピーク)
    measurement: str  # continuous(連続測定・リセットなし)
    calibration_horizon_years: int  # 3(3年連続条項)
    breach_probability_max: float  # 0.10


@dataclass(frozen=True)
class RiskBudget:
    """§3.1 リスク予算(σ予算・SR 仮説・DD 定義)。"""

    sigma_annual: float
    sigma_annual_max: float
    sr_prior: float
    sr_update_frequency: str
    sr_range: tuple[float, float]
    expected_return_annual_range: tuple[float, float]
    drawdown: DrawdownDefinition


@dataclass(frozen=True)
class HardLimits:
    """§3.2 リスク上限(ハードリミット)。全ポッド合算・帳簿単位で適用。"""

    max_drawdown: float
    dd_soft_limit: float
    dd_hard_limit: float
    realized_vol_limit: float
    realized_vol_ewma_days: int
    daily_es95_nav_max: float
    gross_leverage_max: float
    maintenance_margin_buffer: float
    issuer_concentration_nav_max: float
    daily_turnover_nav_max: float


@dataclass(frozen=True)
class Guardrails:
    """§4.2 エクスポージャー・ガードレール(上限であって目標ではない)。"""

    single_asset_class_gross_nav_max: float
    crypto_nav_max: float
    crypto_dormant: bool
    cash_nav_min: float
    issuer_concentration_nav_max: float


@dataclass(frozen=True)
class UnitLotException:
    """§7-1 単元例外(日本個別株・全帳簿共通)。"""

    scope: str
    max_units: int
    unit_cost_nav_max: float  # 0.35(独立レビュー条件3)
    margin_buy_allowed: bool


@dataclass(frozen=True)
class KillSwitchMode:
    """§5 Kill Switch の1モード。"""

    name: str  # kill / winddown / flatten
    effect: str
    executor: str | None = None  # deterministic_code_only(②③)
    confirmation_steps: int | None = None  # flatten のみ 2
    exception_trades: str | None = None  # kill のみ per_trade_user_approval


@dataclass(frozen=True)
class ExperimentProfile:
    """§6 実験プロファイル1件(ファンド全体パラメータのバリエーション)。"""

    name: str
    max_drawdown: float
    gross_leverage_max: float
    sigma_annual: float | None  # moderate/conservative は文書に σ予算の記載なし(None)


@dataclass(frozen=True)
class IPSConfig:
    """``config/ips.yaml`` の内容(IPS v1.3 機械可読版)。"""

    version: str
    effective_date: str
    base_currency: str
    capital_jpy: int
    books: tuple[str, ...]
    risk_budget: RiskBudget
    hard_limits: HardLimits
    guardrails: Guardrails
    unit_lot_exception: UnitLotException
    prohibited_instruments: tuple[str, ...]
    short_allowed: bool
    short_single_name_nav_max: float
    products_default: str  # deny(§8.2 デフォルト不可)
    products_allowed: tuple[str, ...]
    kill_switch_modes: dict[str, KillSwitchMode]
    asset_classes: tuple[str, ...]
    experiment_profiles: dict[str, ExperimentProfile]
    base_profile: str
    live_books_profilable: bool
    e8_capital_jpy_range: tuple[int, int]
    mandate_direction: str  # narrow_only
    mandates_path: str = "config/mandates"
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def load(cls, path: str | Path = _IPS_PATH) -> IPSConfig:
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        fund = data.get("fund", {}) or {}
        rb = data.get("risk_budget", {}) or {}
        sr = rb.get("sr_hypothesis", {}) or {}
        dd = rb.get("drawdown_definition", {}) or {}
        hl = data.get("hard_limits", {}) or {}
        gr = data.get("guardrails", {}) or {}
        ule = data.get("unit_lot_exception", {}) or {}
        pro = data.get("prohibitions", {}) or {}
        short = pro.get("short_selling", {}) or {}
        products = data.get("products", {}) or {}
        ks = (data.get("kill_switch", {}) or {}).get("modes", {}) or {}
        tax = data.get("asset_class_taxonomy", {}) or {}
        ep = data.get("experiment_profiles", {}) or {}
        e8 = ep.get("e8_sweep", {}) or {}
        man = data.get("mandates", {}) or {}

        config = cls(
            version=str(data.get("version", "")),
            effective_date=str(data.get("effective_date", "")),
            base_currency=str(fund.get("base_currency", "")),
            capital_jpy=int(fund.get("capital_jpy", 0)),
            books=tuple(str(b) for b in fund.get("books", [])),
            risk_budget=RiskBudget(
                sigma_annual=float(rb.get("sigma_annual", 0.0)),
                sigma_annual_max=float(rb.get("sigma_annual_max", 0.0)),
                sr_prior=float(sr.get("prior", 0.0)),
                sr_update_frequency=str(sr.get("update_frequency", "")),
                sr_range=_pair(sr.get("range", [])),
                expected_return_annual_range=_pair(rb.get("expected_return_annual_range", [])),
                drawdown=DrawdownDefinition(
                    basis=str(dd.get("basis", "")),
                    peak=str(dd.get("peak", "")),
                    measurement=str(dd.get("measurement", "")),
                    calibration_horizon_years=int(dd.get("calibration_horizon_years", 0)),
                    breach_probability_max=float(dd.get("breach_probability_max", 0.0)),
                ),
            ),
            hard_limits=HardLimits(
                max_drawdown=float(hl.get("max_drawdown", 0.0)),
                dd_soft_limit=float(hl.get("dd_soft_limit", 0.0)),
                dd_hard_limit=float(hl.get("dd_hard_limit", 0.0)),
                realized_vol_limit=float(hl.get("realized_vol_limit", 0.0)),
                realized_vol_ewma_days=int(hl.get("realized_vol_ewma_days", 0)),
                daily_es95_nav_max=float(hl.get("daily_es95_nav_max", 0.0)),
                gross_leverage_max=float(hl.get("gross_leverage_max", 0.0)),
                maintenance_margin_buffer=float(hl.get("maintenance_margin_buffer", 0.0)),
                issuer_concentration_nav_max=float(hl.get("issuer_concentration_nav_max", 0.0)),
                daily_turnover_nav_max=float(hl.get("daily_turnover_nav_max", 0.0)),
            ),
            guardrails=Guardrails(
                single_asset_class_gross_nav_max=float(
                    gr.get("single_asset_class_gross_nav_max", 0.0)
                ),
                crypto_nav_max=float(gr.get("crypto_nav_max", 0.0)),
                crypto_dormant=bool(gr.get("crypto_dormant", False)),
                cash_nav_min=float(gr.get("cash_nav_min", 0.0)),
                issuer_concentration_nav_max=float(gr.get("issuer_concentration_nav_max", 0.0)),
            ),
            unit_lot_exception=UnitLotException(
                scope=str(ule.get("scope", "")),
                max_units=int(ule.get("max_units", 0)),
                unit_cost_nav_max=float(ule.get("unit_cost_nav_max", 0.0)),
                margin_buy_allowed=bool(ule.get("margin_buy_allowed", True)),
            ),
            prohibited_instruments=tuple(str(x) for x in pro.get("instruments", [])),
            short_allowed=bool(short.get("allowed", False)),
            short_single_name_nav_max=float(short.get("single_name_nav_max", 0.0)),
            products_default=str(products.get("default", "")),
            products_allowed=tuple(str(x) for x in products.get("allowed", [])),
            kill_switch_modes={
                str(name): KillSwitchMode(
                    name=str(name),
                    effect=str((spec or {}).get("effect", "")),
                    executor=(spec or {}).get("executor"),
                    confirmation_steps=(spec or {}).get("confirmation_steps"),
                    exception_trades=(spec or {}).get("exception_trades"),
                )
                for name, spec in ks.items()
            },
            asset_classes=tuple(str(x) for x in tax.get("classes", [])),
            experiment_profiles={
                str(name): ExperimentProfile(
                    name=str(name),
                    max_drawdown=float((spec or {}).get("max_drawdown", 0.0)),
                    gross_leverage_max=float((spec or {}).get("gross_leverage_max", 0.0)),
                    sigma_annual=(
                        None
                        if (spec or {}).get("sigma_annual") is None
                        else float((spec or {})["sigma_annual"])
                    ),
                )
                for name, spec in (ep.get("profiles", {}) or {}).items()
            },
            base_profile=str(ep.get("base_profile", "")),
            live_books_profilable=bool(ep.get("live_books_profilable", True)),
            e8_capital_jpy_range=_pair_int(e8.get("capital_jpy_range", [])),
            mandate_direction=str(man.get("direction", "")),
            mandates_path=str(man.get("path", "config/mandates")),
            raw=data,
        )
        config.validate()
        return config

    def validate(self) -> None:
        """値域と 80-ips.md から導出できる不変条件を検査する。違反は ``IPSValidationError``。"""
        _require(self.base_currency == "JPY", f"基準通貨は JPY(確定): {self.base_currency}")
        _require(self.capital_jpy > 0, f"出資金は正であるべき: {self.capital_jpy}")
        _require(len(self.books) > 0, "帳簿(fund.books)が空")

        rb, hl, gr = self.risk_budget, self.hard_limits, self.guardrails
        _fraction(rb.sigma_annual, "risk_budget.sigma_annual")
        _require(
            rb.sigma_annual <= rb.sigma_annual_max + _EPS,
            f"σ予算 {rb.sigma_annual} が上限 {rb.sigma_annual_max} 超"
            "(実績分布での再較正まで上限 12% — §3.1)",
        )
        _require(
            rb.drawdown.calibration_horizon_years == 3,
            f"DD 較正ホライズンは 3年(3年連続条項・§3.1): {rb.drawdown.calibration_horizon_years}",
        )
        _require(
            rb.drawdown.measurement == "continuous",
            f"DD は連続測定・リセットなし(§3.1): {rb.drawdown.measurement}",
        )
        _fraction(rb.drawdown.breach_probability_max, "drawdown.breach_probability_max")

        for name in (
            "max_drawdown",
            "dd_soft_limit",
            "dd_hard_limit",
            "realized_vol_limit",
            "daily_es95_nav_max",
            "maintenance_margin_buffer",
            "issuer_concentration_nav_max",
            "daily_turnover_nav_max",
        ):
            _fraction(getattr(hl, name), f"hard_limits.{name}")
        _require(
            hl.dd_soft_limit < hl.dd_hard_limit,
            f"DD ソフトリミット {hl.dd_soft_limit} はハードリミット {hl.dd_hard_limit} 未満のはず",
        )
        _require(
            abs(hl.dd_hard_limit - hl.max_drawdown) < _EPS,
            f"DD ハードリミット {hl.dd_hard_limit} = 最大 DD 許容 {hl.max_drawdown} のはず(§3.2)",
        )
        _require(
            abs(hl.realized_vol_limit - (rb.sigma_annual + _REALIZED_VOL_MARGIN)) < _EPS,
            f"実現ボラ上限 {hl.realized_vol_limit} は σ予算+3pp = "
            f"{rb.sigma_annual + _REALIZED_VOL_MARGIN} のはず(§3.2)",
        )
        _require(hl.realized_vol_ewma_days > 0, "EWMA 日数は正のはず(§3.2: 20日)")
        _require(hl.gross_leverage_max > 0, f"レバレッジ上限は正のはず: {hl.gross_leverage_max}")

        _fraction(
            gr.single_asset_class_gross_nav_max, "guardrails.single_asset_class_gross_nav_max"
        )
        _fraction(gr.crypto_nav_max, "guardrails.crypto_nav_max")
        _fraction(gr.cash_nav_min, "guardrails.cash_nav_min")
        _require(
            abs(gr.issuer_concentration_nav_max - hl.issuer_concentration_nav_max) < _EPS,
            "ガードレールの発行体集中度は §3.2 の再掲(同値)のはず",
        )

        ule = self.unit_lot_exception
        _require(ule.max_units == 1, f"単元例外は1単元まで(§7-1): {ule.max_units}")
        _fraction(ule.unit_cost_nav_max, "unit_lot_exception.unit_cost_nav_max")
        _require(not ule.margin_buy_allowed, "単元例外ポジションの信用買いは不可(§7-1)")

        _fraction(self.short_single_name_nav_max, "prohibitions.short_selling.single_name_nav_max")
        _require(
            self.products_default == "deny",
            f"商品方針はデフォルト不可(§8.2): {self.products_default}",
        )
        _require(
            set(self.kill_switch_modes) == {"kill", "winddown", "flatten"},
            f"Kill Switch は3モード(§5): {sorted(self.kill_switch_modes)}",
        )
        flatten = self.kill_switch_modes["flatten"]
        _require(flatten.confirmation_steps == 2, "/flatten は2段階確認必須(§5)")
        for mode in ("winddown", "flatten"):
            _require(
                self.kill_switch_modes[mode].executor == "deterministic_code_only",
                f"/{mode} は LLM を経由しない決定論コードのみで実行(§5)",
            )
        _require(len(self.asset_classes) > 0, "資産クラス・タクソノミー(§8.1)が空")

        _require(
            self.base_profile in self.experiment_profiles,
            f"基準プロファイル {self.base_profile!r} が profiles に無い",
        )
        _require(not self.live_books_profilable, "実弾帳簿はプロファイル化不可(§6)")
        for prof in self.experiment_profiles.values():
            _fraction(prof.max_drawdown, f"profiles.{prof.name}.max_drawdown")
            _require(
                prof.gross_leverage_max > 0,
                f"profiles.{prof.name}.gross_leverage_max は正のはず",
            )
            if prof.sigma_annual is not None:
                _fraction(prof.sigma_annual, f"profiles.{prof.name}.sigma_annual")
        base = self.experiment_profiles[self.base_profile]
        _require(
            abs(base.max_drawdown - hl.max_drawdown) < _EPS
            and abs(base.gross_leverage_max - hl.gross_leverage_max) < _EPS,
            "基準プロファイルの DD・レバは §3.2 の既定値と一致するはず",
        )
        lo, hi = self.e8_capital_jpy_range
        _require(0 < lo < hi, f"E8 資本レンジが不正: {self.e8_capital_jpy_range}")
        _require(
            self.mandate_direction == "narrow_only",
            f"マンデートは狭める方向のみ有効(80-ips.md 冒頭): {self.mandate_direction}",
        )


def _pair(value: Any) -> tuple[float, float]:
    seq = list(value or [])
    if len(seq) != 2:
        raise IPSValidationError(f"2要素のレンジであるべき: {value!r}")
    return float(seq[0]), float(seq[1])


def _pair_int(value: Any) -> tuple[int, int]:
    lo, hi = _pair(value)
    return int(lo), int(hi)


# ── マンデート ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Mandate:
    """``config/mandates/<fm>.yaml`` の内容(81-fm-mandates.md §3)。"""

    fm: str
    version: str
    approved_at: str
    universe: tuple[str, ...]
    capital_jpy: int
    pod_sigma_budget: float  # 対仮想資本・年率
    pod_gross_leverage_limit: float  # 対仮想資本
    pod_dd_limit: float
    pod_concentration_limit: float  # 対仮想資本
    additional_prohibitions: tuple[str, ...]
    short: bool | str
    benchmark: str

    @classmethod
    def load(cls, path: str | Path) -> Mandate:
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        mandate = cls(
            fm=str(data.get("fm", "")),
            version=str(data.get("version", "")),
            approved_at=str(data.get("approved_at", "")),
            universe=tuple(str(x) for x in data.get("universe", [])),
            capital_jpy=int(data.get("capital_jpy", 0)),
            pod_sigma_budget=float(data.get("pod_sigma_budget", 0.0)),
            pod_gross_leverage_limit=float(data.get("pod_gross_leverage_limit", 0.0)),
            pod_dd_limit=float(data.get("pod_dd_limit", 0.0)),
            pod_concentration_limit=float(data.get("pod_concentration_limit", 0.0)),
            additional_prohibitions=tuple(
                str(x) for x in data.get("additional_prohibitions", [])
            ),
            short=data.get("short", False),
            benchmark=str(data.get("benchmark", "")),
        )
        _require(mandate.fm != "", f"{path}: fm が空")
        _require(mandate.capital_jpy > 0, f"{path}: capital_jpy は正のはず")
        _fraction(mandate.pod_sigma_budget, f"{mandate.fm}: pod_sigma_budget")
        _fraction(mandate.pod_dd_limit, f"{mandate.fm}: pod_dd_limit")
        _fraction(mandate.pod_concentration_limit, f"{mandate.fm}: pod_concentration_limit")
        _require(
            mandate.pod_gross_leverage_limit > 0,
            f"{mandate.fm}: pod_gross_leverage_limit は正のはず",
        )
        return mandate


def load_mandates(directory: str | Path = _MANDATES_DIR) -> dict[str, Mandate]:
    """``config/mandates/*.yaml`` を全部読み、fm 名 → ``Mandate`` を返す。"""
    mandates: dict[str, Mandate] = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        mandate = Mandate.load(path)
        _require(mandate.fm not in mandates, f"fm 重複: {mandate.fm}")
        mandates[mandate.fm] = mandate
    return mandates


def weighted_gross_leverage(ips: IPSConfig, mandates: dict[str, Mandate]) -> float:
    """グロスレバ上限の資本加重和(81 §3: 0.2×(1.0+1.5+3.0+1.0)=1.3x)。"""
    return sum(
        m.capital_jpy / ips.capital_jpy * m.pod_gross_leverage_limit for m in mandates.values()
    )


def worst_case_sigma(ips: IPSConfig, mandates: dict[str, Mandate]) -> float:
    """ポッド σ配分の資本加重和 = 相関1の最悪ケース合算 σ(81 §3: 13.4%)。

    ハード検証には使わない(モジュール docstring 参照 — 81 §3 が σ予算 12% 超過を低相関前提で
    承認済み。最終防衛線は IPS §3.2 の実現ボラ上限ゲート)。監視・レポート用。
    """
    return sum(m.capital_jpy / ips.capital_jpy * m.pod_sigma_budget for m in mandates.values())


def validate_mandates(ips: IPSConfig, mandates: dict[str, Mandate]) -> None:
    """マンデートが IPS を「緩める」方向の値を持たないことを検証する。

    違反があれば ``MandateViolationError`` に全件列挙して送出する(1件目で止めない —
    交付・改訂時に全違反を一度に返すため)。
    """
    violations: list[str] = []
    hl = ips.hard_limits

    for m in mandates.values():
        # 1) ポッド DD 上限はファンド DD 上限(25%)以下(狭める方向のみ)。
        if m.pod_dd_limit > hl.max_drawdown + _EPS:
            violations.append(
                f"{m.fm}: ポッド DD 上限 {m.pod_dd_limit:.2f} がファンド上限 "
                f"{hl.max_drawdown:.2f}(IPS §3.2)超"
            )
        # 4) ポッド内集中度の絶対額 ≤ ファンド集中度の絶対額(分母が違うため絶対額比較)。
        pod_abs = m.pod_concentration_limit * m.capital_jpy
        fund_abs = hl.issuer_concentration_nav_max * ips.capital_jpy
        if pod_abs > fund_abs + _EPS:
            violations.append(
                f"{m.fm}: ポッド集中度の絶対額 ¥{pod_abs:,.0f}"
                f"({m.pod_concentration_limit:.0%}×¥{m.capital_jpy:,})が"
                f"ファンド集中度 ¥{fund_abs:,.0f}(NAV の "
                f"{hl.issuer_concentration_nav_max:.0%})超"
            )
        # 5) 暗号資産休眠条項(IPS §4.2): 休眠中のユニバースに暗号資産は置けない。
        if ips.guardrails.crypto_dormant:
            crypto_terms = [u for u in m.universe if "crypto" in u.lower()]
            if crypto_terms:
                violations.append(
                    f"{m.fm}: 暗号資産は休眠条項中(IPS §4.2)— "
                    f"有効化はマンデート改訂(ユーザー承認)による: {crypto_terms}"
                )

    # 2) グロスレバの資本加重和 ≤ ファンド上限(81 §3 の整合検証)。
    lev = weighted_gross_leverage(ips, mandates)
    if lev > hl.gross_leverage_max + _EPS:
        violations.append(
            f"グロスレバの資本加重和 {lev:.2f}x がファンド上限 "
            f"{hl.gross_leverage_max:.1f}x(IPS §3.2)超"
        )
    # 3) ポッド仮想資本の合計 ≤ ファンド出資金(中央リザーブ含めた枠 — 81 §3)。
    total_capital = sum(m.capital_jpy for m in mandates.values())
    if total_capital > ips.capital_jpy:
        violations.append(
            f"ポッド仮想資本の合計 ¥{total_capital:,} がファンド出資金 "
            f"¥{ips.capital_jpy:,}(IPS §1)超"
        )

    if violations:
        raise MandateViolationError(violations)


def load_and_validate(
    ips_path: str | Path = _IPS_PATH, mandates_dir: str | Path = _MANDATES_DIR
) -> tuple[IPSConfig, dict[str, Mandate]]:
    """IPS とマンデートを読み、値域検証+整合検証まで済ませて返す(エンジンの入口)。"""
    ips = IPSConfig.load(ips_path)
    mandates = load_mandates(mandates_dir)
    validate_mandates(ips, mandates)
    return ips, mandates


__all__ = [
    "DrawdownDefinition",
    "ExperimentProfile",
    "Guardrails",
    "HardLimits",
    "IPSConfig",
    "IPSValidationError",
    "KillSwitchMode",
    "Mandate",
    "MandateViolationError",
    "RiskBudget",
    "UnitLotException",
    "load_and_validate",
    "load_mandates",
    "validate_mandates",
    "weighted_gross_leverage",
    "worst_case_sigma",
]
