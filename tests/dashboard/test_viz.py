"""dashboard/viz.py のテスト(T-018)。

DB を使わない純ロジック(フォーマッタの桁数・bullet の境界値・underwater・期間
リターン)を検証する。描画関数(``render_*``)は AppTest 側(``test_app_pages.py``)で
例外ゼロを確認する。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

viz = pytest.importorskip("viz", reason="streamlit 未導入(.[dashboard] を入れると走る)")


# ── フォーマッタ ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("value", "digits", "expected"),
    [
        (0, 3, "0"),
        (12.3456, 3, "12.3"),
        (1.23456, 3, "1.23"),
        (0.0123456, 3, "0.0123"),
        (-12.3456, 3, "-12.3"),
        (12.3456, 2, "12"),
        (1234.5, 3, "1,234"),  # 整数部は丸めない(金額を別物にしないため)
        (None, 3, viz.MISSING),
        (float("nan"), 3, viz.MISSING),
    ],
)
def test_fmt_sig_digits(value, digits, expected):
    assert viz.fmt_sig(value, digits) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "¥0"),
        (1234, "¥1,234"),
        (9999, "¥9,999"),
        (10_000, "¥1.00万"),  # 万の境界
        (1_000_000, "¥100万"),
        (99_000_000, "¥9,900万"),
        (100_000_000, "¥1.00億"),  # 億の境界
        (-1_000_000, "-¥100万"),
        (Decimal("12345.6"), "¥1.23万"),
        (None, viz.MISSING),
    ],
)
def test_fmt_jpy(value, expected):
    assert viz.fmt_jpy(value) == expected


def test_fmt_pct_and_signed():
    assert viz.fmt_pct(0.153) == "15.3%"
    assert viz.fmt_pct(0.153, 0) == "15%"
    assert viz.fmt_signed_pct(0.0123) == "+1.23%"
    assert viz.fmt_signed_pct(-0.0123) == "-1.23%"
    assert viz.fmt_signed_pct(0.0) == "+0.00%"
    assert viz.fmt_pct(None) == viz.MISSING


def test_fmt_delta_md_colors_are_variance_only():
    # 赤緑は差異専用(IBCS)。0 と欠測は無着色。
    assert viz.fmt_delta_md(0.01) == ":green[+1.00%]"
    assert viz.fmt_delta_md(-0.01) == ":red[-1.00%]"
    assert viz.fmt_delta_md(0.0) == "+0.00%"
    assert viz.fmt_delta_md(None) == viz.MISSING
    assert viz.fmt_delta_md(-1, text="-¥100万") == ":red[-¥100万]"


def test_fmt_hours_switches_to_days():
    assert viz.fmt_hours(3.25) == "3.2h"
    assert viz.fmt_hours(48) == "2.0日"
    assert viz.fmt_hours(None) == viz.MISSING


# ── bullet の境界値 ───────────────────────────────────────────────────────────
def test_bullet_zero_actual_is_ok():
    b = viz.make_bullet("DD", 0.0, 0.25)
    assert (b.ratio, b.usage, b.level) == (0.0, 0.0, "ok")
    assert "使用率 0%" in b.text


def test_bullet_at_limit_is_breach_inclusive():
    """IPS §3.2 のフラグは「到達」(>=)で立つ。bullet も境界を含めて breach。"""
    b = viz.make_bullet("DD", 0.25, 0.25)
    assert b.ratio == 1.0
    assert b.usage == 1.0
    assert b.level == "breach"
    assert b.text.startswith(":red[")


def test_bullet_over_limit_clamps_ratio_but_keeps_usage():
    b = viz.make_bullet("DD", 0.50, 0.25)
    assert b.ratio == 1.0  # st.progress は 0..1 しか受けない
    assert b.usage == 2.0  # 実際の超過度合いは失わない
    assert b.level == "breach"
    assert "使用率 200%" in b.text


def test_bullet_warn_at_default_threshold():
    assert viz.make_bullet("x", 0.74, 1.0).level == "ok"
    assert viz.make_bullet("x", 0.75, 1.0).level == "warn"  # 既定 warn_at = 0.75 の境界
    assert viz.make_bullet("x", 0.99, 1.0).level == "warn"


def test_bullet_soft_limit_overrides_warn_at():
    # ソフトリミットがあるときは使用率ではなく絶対値で警戒判定する。
    b = viz.make_bullet("DD", 0.16, 0.25, soft_limit=0.15)
    assert b.level == "warn"
    assert viz.make_bullet("DD", 0.14, 0.25, soft_limit=0.15).level == "ok"
    # ソフト境界も「到達」で立つ。
    assert viz.make_bullet("DD", 0.15, 0.25, soft_limit=0.15).level == "warn"


def test_bullet_negative_actual_clamps_to_zero():
    b = viz.make_bullet("x", -0.5, 1.0)
    assert b.ratio == 0.0
    assert b.usage == -0.5
    assert b.level == "ok"


@pytest.mark.parametrize(("actual", "limit"), [(None, 0.25), (0.1, None), (0.1, 0), (0.1, -1)])
def test_bullet_unknown_when_ratio_undefined(actual, limit):
    """比率を偽造しない: 実績・リミットが欠けたら unknown(灰色・使用率は —)。"""
    b = viz.make_bullet("x", actual, limit)
    assert b.level == "unknown"
    assert b.ratio == 0.0
    assert b.usage is None
    assert b.text.startswith(":gray[")
    assert viz.MISSING in b.text


def test_bullet_text_always_carries_comparison_context():
    b = viz.make_bullet("ES95", 0.012, 0.03, fmt=viz.fmt_pct)
    assert "1.2%" in b.text and "3.0%" in b.text and "使用率 40%" in b.text


# ── underwater / NAV ─────────────────────────────────────────────────────────
def _nav(day: int, nav: float, flow: float = 0.0) -> dict:
    return {"day": date(2026, 1, day), "nav": Decimal(str(nav)), "net_flow": Decimal(str(flow))}


def test_underwater_frame_is_non_positive_and_zero_at_peak():
    rows = [_nav(1, 100), _nav(2, 120), _nav(3, 90), _nav(4, 120)]
    frame = viz.underwater_frame(rows)
    assert list(frame.columns) == ["DD(%)"]
    values = [round(v, 6) for v in frame["DD(%)"].tolist()]
    assert values == [0.0, 0.0, -25.0, 0.0]  # ピーク 120 に対し 90 は -25%
    assert max(values) <= 0.0


def test_underwater_frame_empty_series():
    frame = viz.underwater_frame([])
    assert frame.empty
    assert list(frame.columns) == ["DD(%)"]


def test_nav_frame_indexed_by_day():
    frame = viz.nav_frame([_nav(1, 100), _nav(2, 110)])
    assert frame.index.tolist() == [date(2026, 1, 1), date(2026, 1, 2)]
    assert frame["NAV"].tolist() == [100.0, 110.0]


# ── 期間リターン ──────────────────────────────────────────────────────────────
def test_flow_adjusted_returns_excludes_capital_flows():
    """出資 ¥50 で NAV が 100→150 でもリターンは 0%(TWR)。"""
    rows = [_nav(1, 100), _nav(2, 150, flow=50)]
    assert viz.flow_adjusted_returns(rows) == [(date(2026, 1, 2), 0.0)]


def test_period_return_compounds():
    rows = [_nav(1, 100), _nav(2, 110), _nav(3, 121)]
    assert viz.period_return(rows, days=None) == pytest.approx(0.21)


def test_period_return_window_cutoff():
    rows = [_nav(1, 100), _nav(10, 200), _nav(25, 220)]
    # 直近 7 日窓(1/18 より後)には 1/25 の +10% だけが入る。
    assert viz.period_return(rows, days=7) == pytest.approx(0.10)


def test_period_return_none_when_insufficient():
    assert viz.period_return([], days=None) is None
    assert viz.period_return([_nav(1, 100)], days=None) is None  # 1 点ではリターンが立たない
    assert viz.period_return([_nav(1, 100), _nav(2, 110)], days=0) is None


def test_period_returns_table_shape():
    rows = [_nav(1, 100), _nav(2, 110)]
    table = viz.period_returns(rows)
    assert [r["期間"] for r in table] == ["1W", "1M", "設定来"]
    assert table[-1]["表示"] == "+10.00%"
