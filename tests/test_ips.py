"""ips と保護領域の不変条件テスト(Issue #28)。

``config/ips.yaml``(IPS v1.3 機械可読版)のローダ・値域検証と、マンデート整合検証
(IPS を「緩める」方向の値の拒否)を検証する。DB 不要の純粋テスト。実ファイル
(config/ips.yaml・config/mandates/*.yaml)が 80-ips.md / 81-fm-mandates.md の値と
一致していることもここで固定する(保護領域のリグレッション検知)。

さらに ``config/governance.yaml`` の ``protected_areas`` に登録されたパスが実体を持つことを
不変条件として固定する(独立役員審査 C-5)。改名・削除で登録が実体を失うと A-18-1 は
「そのパスに触れたコミット」を永久に見つけられないまま静かに無力化するため、CI で検知する。
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ryza.ips import (
    IPSConfig,
    IPSValidationError,
    Mandate,
    MandateViolationError,
    load_and_validate,
    load_mandates,
    validate_mandates,
    weighted_gross_leverage,
    worst_case_sigma,
)

_ROOT = Path(__file__).resolve().parents[1]
_IPS_PATH = _ROOT / "config" / "ips.yaml"
_MANDATES_DIR = _ROOT / "config" / "mandates"
_GOVERNANCE_PATH = _ROOT / "config" / "governance.yaml"

# glob メタ文字を含むパスは「実在」を単純検査できないため、追跡ファイルとの照合に回す。
_GLOB_CHARS = ("*", "?")


@pytest.fixture(scope="module")
def ips() -> IPSConfig:
    return IPSConfig.load(_IPS_PATH)


@pytest.fixture()
def raw_ips() -> dict[str, Any]:
    return yaml.safe_load(_IPS_PATH.read_text(encoding="utf-8"))


def _dump(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "ips.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# ── 実 config/ips.yaml が 80-ips.md の値と一致(保護領域の固定)─────────────────
class TestRealIPSValues:
    def test_fund(self, ips: IPSConfig) -> None:
        assert ips.version == "1.3"
        assert ips.effective_date == "2026-08-03"
        assert ips.base_currency == "JPY"
        assert ips.capital_jpy == 10_000_000  # 初期 ¥100万 + 増額 ¥900万(§1)
        assert ips.books == ("DEMO_FUND",)

    def test_risk_budget(self, ips: IPSConfig) -> None:
        rb = ips.risk_budget
        assert rb.sigma_annual == 0.12  # §3.1 運用値(暫定)
        assert rb.sigma_annual_max == 0.12  # 再較正まで上限 12%
        assert rb.sr_prior == 0.5  # 縮小推定の事前分布
        assert rb.sr_update_frequency == "quarterly"
        assert rb.expected_return_annual_range == (0.06, 0.13)

    def test_drawdown_definition(self, ips: IPSConfig) -> None:
        dd = ips.risk_budget.drawdown
        assert dd.basis == "per_book"  # 帳簿単位(独立レビュー条件1)
        assert dd.peak == "since_inception"  # 設定来ピーク
        assert dd.measurement == "continuous"  # 連続測定・リセットなし
        assert dd.calibration_horizon_years == 3  # 3年連続条項
        assert dd.breach_probability_max == 0.10

    def test_hard_limits(self, ips: IPSConfig) -> None:
        hl = ips.hard_limits
        assert hl.max_drawdown == 0.25  # 確定
        assert hl.dd_soft_limit == 0.15
        assert hl.dd_hard_limit == 0.25
        assert hl.realized_vol_limit == 0.15  # σ予算 12% + 3pp
        assert hl.realized_vol_ewma_days == 20
        assert hl.daily_es95_nav_max == 0.03
        assert hl.gross_leverage_max == 2.0  # 確定
        assert hl.maintenance_margin_buffer == 0.20
        assert hl.issuer_concentration_nav_max == 0.20  # 確定
        assert hl.daily_turnover_nav_max == 0.30

    def test_guardrails(self, ips: IPSConfig) -> None:
        gr = ips.guardrails
        assert gr.single_asset_class_gross_nav_max == 0.70
        assert gr.crypto_nav_max == 0.05
        assert gr.crypto_dormant is True  # 休眠条項
        assert gr.cash_nav_min == 0.05
        assert gr.issuer_concentration_nav_max == 0.20  # §3.2 再掲

    def test_unit_lot_exception(self, ips: IPSConfig) -> None:
        ule = ips.unit_lot_exception
        assert ule.max_units == 1  # 1単元まで超過許容(§7-1)
        assert ule.unit_cost_nav_max == 0.35  # 独立レビュー条件3
        assert ule.margin_buy_allowed is False  # 信用買い不可

    def test_prohibitions_and_products(self, ips: IPSConfig) -> None:
        assert "leveraged_etf" in ips.prohibited_instruments
        assert "inverse_etf" in ips.prohibited_instruments
        assert ips.short_allowed is True
        assert ips.short_single_name_nav_max == 0.10  # 個別ショートは NAV の 10% まで
        assert ips.products_default == "deny"  # §8.2 デフォルト不可
        assert "exchange_fx" in ips.products_allowed
        assert "otc_derivatives" not in ips.products_allowed
        assert "options" not in ips.products_allowed

    def test_kill_switch_three_modes(self, ips: IPSConfig) -> None:
        modes = ips.kill_switch_modes
        assert set(modes) == {"kill", "winddown", "flatten"}
        assert modes["kill"].effect == "freeze"  # 全新規発注停止・ポジション維持
        assert modes["kill"].exception_trades == "per_trade_user_approval"
        assert modes["winddown"].effect == "staged_liquidation"
        assert modes["winddown"].executor == "deterministic_code_only"  # LLM 非経由
        assert modes["flatten"].effect == "immediate_market_liquidation"
        assert modes["flatten"].executor == "deterministic_code_only"
        assert modes["flatten"].confirmation_steps == 2  # 2段階確認必須

    def test_asset_class_taxonomy(self, ips: IPSConfig) -> None:
        assert ips.asset_classes == (
            "equity_jp", "equity_us", "equity_other", "bond", "fx",
            "crypto", "commodity_futures", "rates", "cash",
        )  # §8.1

    def test_experiment_profiles(self, ips: IPSConfig) -> None:
        profs = ips.experiment_profiles
        assert ips.base_profile == "aggressive"
        assert ips.live_books_profilable is False  # 実弾帳簿はプロファイル化不可
        agg = profs["aggressive"]
        assert (agg.max_drawdown, agg.gross_leverage_max, agg.sigma_annual) == (0.25, 2.0, 0.12)
        mod = profs["moderate"]
        assert (mod.max_drawdown, mod.gross_leverage_max) == (0.15, 1.5)
        assert mod.sigma_annual is None  # 文書に記載なし → TODO(発明禁止)
        con = profs["conservative"]
        assert (con.max_drawdown, con.gross_leverage_max) == (0.10, 1.0)
        assert con.sigma_annual is None
        moon = profs["moonshot"]
        assert (moon.max_drawdown, moon.gross_leverage_max, moon.sigma_annual) == (0.60, 2.0, 0.50)

    def test_e8_sweep_range(self, ips: IPSConfig) -> None:
        assert ips.e8_capital_jpy_range == (100_000, 1_000_000)  # ¥10万〜100万

    def test_mandate_direction(self, ips: IPSConfig) -> None:
        assert ips.mandate_direction == "narrow_only"


# ── 値域検証(改竄した YAML は拒否される)─────────────────────────────────────
class TestIPSValidation:
    @pytest.mark.parametrize(
        ("mutate", "match"),
        [
            (lambda d: d["risk_budget"].__setitem__("sigma_annual", 0.20), "σ予算"),
            (lambda d: d["hard_limits"].__setitem__("max_drawdown", 1.5), r"\(0,1\]"),
            (lambda d: d["hard_limits"].__setitem__("dd_soft_limit", 0.30), "ソフトリミット"),
            (lambda d: d["hard_limits"].__setitem__("realized_vol_limit", 0.20), "実現ボラ上限"),
            (lambda d: d["fund"].__setitem__("base_currency", "USD"), "JPY"),
            (
                lambda d: d["risk_budget"]["drawdown_definition"].__setitem__(
                    "calibration_horizon_years", 1
                ),
                "3年",
            ),
            (
                lambda d: d["unit_lot_exception"].__setitem__("margin_buy_allowed", True),
                "信用買い",
            ),
            (lambda d: d["products"].__setitem__("default", "allow"), "デフォルト不可"),
            (lambda d: d["kill_switch"]["modes"].pop("flatten"), "3モード"),
            (
                lambda d: d["kill_switch"]["modes"]["flatten"].__setitem__(
                    "confirmation_steps", 1
                ),
                "2段階確認",
            ),
            (
                lambda d: d["kill_switch"]["modes"]["winddown"].__setitem__("executor", "llm"),
                "決定論コード",
            ),
            (
                lambda d: d["experiment_profiles"].__setitem__("live_books_profilable", True),
                "実弾帳簿",
            ),
            (lambda d: d["mandates"].__setitem__("direction", "loosen_ok"), "狭める方向"),
        ],
    )
    def test_tampered_config_rejected(
        self, tmp_path: Path, raw_ips: dict[str, Any], mutate, match: str
    ) -> None:
        data = copy.deepcopy(raw_ips)
        mutate(data)
        with pytest.raises(IPSValidationError, match=match):
            IPSConfig.load(_dump(tmp_path, data))

    def test_real_config_loads_clean(self) -> None:
        IPSConfig.load(_IPS_PATH)  # validate() 込みで例外なし


# ── マンデート整合検証 ─────────────────────────────────────────────────────────
def _mandate(**overrides: Any) -> Mandate:
    base: dict[str, Any] = dict(
        fm="test",
        version="2",
        approved_at="2026-08-03",
        universe=("jp_equity_cash",),
        capital_jpy=2_000_000,
        pod_sigma_budget=0.12,
        pod_gross_leverage_limit=1.0,
        pod_dd_limit=0.20,
        pod_concentration_limit=0.40,
        additional_prohibitions=(),
        short=False,
        benchmark="universe_equal_weight_buy_and_hold",
    )
    base.update(overrides)
    return Mandate(**base)


class TestMandates:
    def test_real_mandates_load(self) -> None:
        mandates = load_mandates(_MANDATES_DIR)
        assert set(mandates) == {"ben", "jim", "stan", "peter"}
        assert all(m.capital_jpy == 2_000_000 for m in mandates.values())  # 81 §3
        assert mandates["stan"].pod_gross_leverage_limit == 3.0

    def test_real_mandates_pass_validation(self, ips: IPSConfig) -> None:
        # 承認済みの初代4名マンデート(81 §3)は IPS を緩めていない。
        validate_mandates(ips, load_mandates(_MANDATES_DIR))

    def test_load_and_validate_entry_point(self) -> None:
        ips, mandates = load_and_validate(_IPS_PATH, _MANDATES_DIR)
        assert ips.version == "1.3"
        assert len(mandates) == 4

    def test_weighted_gross_leverage_matches_doc(self, ips: IPSConfig) -> None:
        # 81 §3: 0.2×(1.0+1.5+3.0+1.0) = 1.3x < ファンド上限 2.0x
        lev = weighted_gross_leverage(ips, load_mandates(_MANDATES_DIR))
        assert lev == pytest.approx(1.3)

    def test_worst_case_sigma_matches_doc(self, ips: IPSConfig) -> None:
        # 81 §3: 相関1の最悪ケースで 0.2×(12+15+25+15) = 13.4%(承認済み・ハード検証対象外)
        sigma = worst_case_sigma(ips, load_mandates(_MANDATES_DIR))
        assert sigma == pytest.approx(0.134)

    def test_pod_dd_cannot_exceed_fund_dd(self, ips: IPSConfig) -> None:
        loose = _mandate(pod_dd_limit=0.30)  # ファンド DD 25% より緩い
        with pytest.raises(MandateViolationError, match="ポッド DD 上限"):
            validate_mandates(ips, {"test": loose})

    def test_pod_concentration_absolute_cannot_exceed_fund(self, ips: IPSConfig) -> None:
        # 集中度の絶対額: 50%×¥500万 = ¥250万 > 20%×¥1,000万 = ¥200万 → 拒否
        loose = _mandate(capital_jpy=5_000_000, pod_concentration_limit=0.50)
        with pytest.raises(MandateViolationError, match="集中度の絶対額"):
            validate_mandates(ips, {"test": loose})

    def test_weighted_leverage_cannot_exceed_fund_limit(self, ips: IPSConfig) -> None:
        # 資本加重和: 0.5×3.0 + 0.5×3.0 = 3.0x > 2.0x → 拒否
        pods = {
            "a": _mandate(fm="a", capital_jpy=5_000_000, pod_gross_leverage_limit=3.0,
                          pod_concentration_limit=0.20),
            "b": _mandate(fm="b", capital_jpy=5_000_000, pod_gross_leverage_limit=3.0,
                          pod_concentration_limit=0.20),
        }
        with pytest.raises(MandateViolationError, match="資本加重和"):
            validate_mandates(ips, pods)

    def test_capital_sum_cannot_exceed_fund_capital(self, ips: IPSConfig) -> None:
        pods = {
            "a": _mandate(fm="a", capital_jpy=6_000_000, pod_concentration_limit=0.20),
            "b": _mandate(fm="b", capital_jpy=6_000_000, pod_concentration_limit=0.20),
        }
        with pytest.raises(MandateViolationError, match="仮想資本の合計"):
            validate_mandates(ips, pods)

    def test_crypto_universe_rejected_while_dormant(self, ips: IPSConfig) -> None:
        loose = _mandate(universe=("jp_equity_cash", "crypto_spot"))
        with pytest.raises(MandateViolationError, match="休眠条項"):
            validate_mandates(ips, {"test": loose})

    def test_violations_are_all_listed(self, ips: IPSConfig) -> None:
        # 1件目で止めず全件列挙する(交付・改訂時に一度で返す)。
        loose = _mandate(pod_dd_limit=0.40, universe=("crypto_spot",))
        with pytest.raises(MandateViolationError) as exc_info:
            validate_mandates(ips, {"test": loose})
        assert len(exc_info.value.violations) == 2

    def test_mandate_value_range_checked(self, tmp_path: Path) -> None:
        bad = {
            "fm": "bad", "version": "1", "approved_at": "2026-08-03",
            "universe": ["jp_equity_cash"], "capital_jpy": 2_000_000,
            "pod_sigma_budget": 1.5,  # (0,1] 違反
            "pod_gross_leverage_limit": 1.0, "pod_dd_limit": 0.2,
            "pod_concentration_limit": 0.4, "additional_prohibitions": [],
            "short": False, "benchmark": "universe_equal_weight_buy_and_hold",
        }
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
        with pytest.raises(IPSValidationError, match="pod_sigma_budget"):
            Mandate.load(path)


# ── 保護領域の登録が実体を持つ(不変条件・独立役員審査 C-5)─────────────────────
def _governance() -> dict[str, Any]:
    return yaml.safe_load(_GOVERNANCE_PATH.read_text(encoding="utf-8")) or {}


def _protected_paths() -> list[str]:
    return [str(e["path"]) for e in _governance().get("protected_areas", [])]


def _governance_line(path: str) -> str:
    """governance.yaml 内で当該 path を宣言している行を「N行目: ...」で返す(失敗メッセージ用)。"""
    for i, line in enumerate(_GOVERNANCE_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("- path:"):
            continue
        if stripped.split("- path:", 1)[1].split("#")[0].strip() == path:
            return f"config/governance.yaml {i} 行目: {stripped}"
    return f"config/governance.yaml(該当行を特定できず): {path}"


def _tracked_files() -> list[str]:
    """git 追跡ファイルの一覧(.venv 等の未追跡物を含めない)。"""
    out = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files"], capture_output=True, text=True, check=True
    )
    return [ln for ln in out.stdout.splitlines() if ln]


class TestProtectedAreaPathsExist:
    """protected_areas の登録が実体を失っていないこと。

    ファイルを改名・削除しても登録は残るため、A-18-1 は「そのパスに触れたコミット」を
    永久に見つけられないまま静かに無力化する(改番 PR #66 で deploy-a13.sh → deploy-a18.sh、
    audit/a13.py → a18.py を改名した際に現実の危険として顕在化した)。
    """

    def test_non_glob_paths_exist(self) -> None:
        missing = [
            p for p in _protected_paths()
            if not any(c in p for c in _GLOB_CHARS) and not (_ROOT / p).exists()
        ]
        assert not missing, (
            "保護領域の登録が実体を失っている(改名・削除の見落とし。A-18-1 が当該パスの"
            "無承認変更を検出できなくなる):\n"
            + "\n".join(f"  - {p}\n    {_governance_line(p)}" for p in missing)
        )

    def test_glob_paths_match_at_least_one_tracked_file(self) -> None:
        """glob 登録も 1 件以上にマッチすること(空 glob は実質無効な登録)。"""
        from ryza.audit.a18 import glob_to_regex

        files = _tracked_files()
        empty = [
            p for p in _protected_paths()
            if any(c in p for c in _GLOB_CHARS)
            and not any(glob_to_regex(p).match(f) for f in files)
        ]
        assert not empty, (
            "保護領域の glob 登録が 1 件もマッチしない(実体を失っているか、実体の追加前に"
            "登録された可能性。A-18-1 は空振りする):\n"
            + "\n".join(f"  - {p}\n    {_governance_line(p)}" for p in empty)
        )

    def test_governance_yaml_itself_is_protected(self) -> None:
        """自己参照の固定: 本検査の入力(governance.yaml)自身が保護領域であること。"""
        assert "config/governance.yaml" in _protected_paths()

    def test_this_test_file_is_protected(self) -> None:
        """不変条件テスト自体が保護領域であること(テストを外して統制を消せないようにする)。"""
        assert "tests/test_ips.py" in _protected_paths()

    def test_ci_workflow_is_protected(self) -> None:
        """CI 定義が保護領域であること(独立役員審査 2026-08-04 中-5)。

        不変条件テストを守っても、それを走らせる唯一の執行点(required status check)が
        無防備なら統制は `Run tests` ステップの削除だけで静かに外れる。
        """
        assert ".github/workflows/ci.yml" in _protected_paths()

    def test_ci_checkout_fetches_full_history(self) -> None:
        """CI の checkout が全履歴を取ること(浅い clone では A-18 の実リポジトリ検査が落ちる)。"""
        ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "fetch-depth: 0" in ci
