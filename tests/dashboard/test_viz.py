"""dashboard/viz.py のテスト(T-018)。

DB を使わない純ロジック(フォーマッタの桁数・bullet の境界値・underwater・期間
リターン)を検証する。描画関数(``render_*``)は AppTest 側(``test_app_pages.py``)で
例外ゼロを確認する。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

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
        (1234.5, "¥1,234"),  # ¥1 以上は小数を出さない(円の最小単位は 1 円)
        (9999, "¥9,999"),
        (10_000, "¥1.00万"),  # 万の境界
        (1_000_000, "¥100万"),
        (99_000_000, "¥9,900万"),
        (100_000_000, "¥1.00億"),  # 億の境界
        (-1_000_000, "-¥100万"),
        (Decimal("12345.6"), "¥1.23万"),
        (0.9, "¥0.90"),  # ¥1 未満だけ小数(LLM 微小コストを 0 に潰さない)
        (-0.05, "-¥0.05"),
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
    # 赤緑は差異専用(IBCS)。0 と欠測は無着色・矢印なし(0 は差異ではない)。
    assert viz.fmt_delta_md(0.01) == ":green[▲ +1.00%]"
    assert viz.fmt_delta_md(-0.01) == ":red[▼ -1.00%]"
    assert viz.fmt_delta_md(0.0) == "+0.00%"
    assert viz.fmt_delta_md(None) == viz.MISSING
    assert viz.fmt_delta_md(-1, text="-¥100万") == ":red[▼ -¥100万]"


def test_fmt_delta_md_good_when_negative_inverts_colors():
    """コスト超過のように「小さいほど良い」差異は色を反転する。"""
    assert viz.fmt_delta_md(0.01, good_when="negative") == ":red[▲ +1.00%]"
    assert viz.fmt_delta_md(-0.01, good_when="negative") == ":green[▼ -1.00%]"
    assert viz.fmt_delta_md(0.0, good_when="negative") == "+0.00%"


def test_fmt_delta_md_arrow_tracks_sign_not_favourability():
    """DADS: 色だけに頼らない。矢印は**符号の向き**で、有利/不利は色が担う。

    「下がって有利」(緑の ▼)が普通に起きる — 矢印を有利/不利に紐付けると、
    コスト削減が ▲ で表示されて増減が読めなくなる。
    """
    assert viz.fmt_delta_md(-0.01, good_when="negative").startswith(":green[▼")
    assert viz.fmt_delta_md(0.01, good_when="negative").startswith(":red[▲")


def test_fmt_delta_md_is_readable_without_color():
    """色指定を剥がしても増減が読めること(色覚特性・モノクロ・CSS 破損への冗長性)。"""
    for value in (0.0123, -0.0123):
        body = viz.fmt_delta_md(value).split("[", 1)[1].rstrip("]")
        assert body[0] in (viz.UP_ARROW, viz.DOWN_ARROW)
        assert ("+" in body) if value > 0 else ("-" in body)


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


@pytest.mark.parametrize("limit", [0, -1])
def test_bullet_non_positive_limit_hides_fake_limit_text(limit):
    """リミット 0/負は設定ミス。上限として -100.0% のような値を画面に出さない。"""
    b = viz.make_bullet("x", 0.1, limit)
    assert b.limit_text == viz.MISSING
    assert "上限 —" in b.text


def test_bullet_unknown_text_carries_note():
    b = viz.make_bullet("実現ボラ", None, 0.15, note="観測不足で判定無効(n=3/20)")
    assert b.level == "unknown"
    assert "観測不足で判定無効(n=3/20)" in b.text


def test_bullet_text_always_carries_comparison_context():
    b = viz.make_bullet("ES95", 0.012, 0.03, fmt=viz.fmt_pct)
    assert "1.2%" in b.text and "3.0%" in b.text and "使用率 40%" in b.text


# ── 色に頼らない表示(DADS・2026-08-03 デザイン改修)──────────────────────────
def test_bullet_level_is_labelled_in_words_not_only_color():
    """breach / warn / unknown は語でも読めること。ok だけは無印(異常だけを立たせる)。"""
    assert viz.LEVEL_LABELS["breach"] in viz.make_bullet("x", 1.0, 1.0).text
    assert viz.LEVEL_LABELS["warn"] in viz.make_bullet("x", 0.8, 1.0).text
    assert viz.LEVEL_LABELS["unknown"] in viz.make_bullet("x", None, 1.0).text
    ok = viz.make_bullet("x", 0.1, 1.0).text
    assert not any(viz.LEVEL_LABELS[lv] in ok for lv in ("breach", "warn", "unknown"))


def test_viz_never_hardcodes_hex_colors():
    """色の変更点を .streamlit/config.toml の一箇所に保つ。

    viz.py が hex を直書きすると、DADS セマンティックの統一(error-1 / success-2 /
    warning-orange-2)が config.toml とここの二箇所に散る。Streamlit の色指定
    (:red[…] 等)だけを使い、実色はテーマ設定に解決させる規約を機械的に守らせる。
    """
    source = Path(viz.__file__).read_text(encoding="utf-8")
    # docstring 内の DADS 実値への言及(#EC0000 等)は規約の説明なので除外する。
    code = "".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    code = re.sub(r'"""(.|\n)*?"""', "", code)
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", code)


# ── underwater / NAV ─────────────────────────────────────────────────────────
def _nav(day: int, nav: float, flow: float = 0.0, bop: float = 0.0) -> dict:
    """NAV 行。``flow`` は当日仕訳(EOP)、``bop`` は区間内仕訳(BOP — 分母に足す)。"""
    return {
        "day": date(2026, 1, day),
        "nav": Decimal(str(nav)),
        "flow_eop": Decimal(str(flow)),
        "flow_bop": Decimal(str(bop)),
        "net_flow": Decimal(str(flow + bop)),
    }


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


def test_flow_adjusted_returns_bop_flow_is_working_capital():
    """区間内の出資は分母に入る(審査シナリオ B: 100 + BOP 50 → 157.5 は +5%)。

    期末仮定だと +7.5% になる。engine.book_returns と同じ BOP/EOP 分離であること。
    """
    rows = [_nav(1, 100), _nav(2, 157.5, bop=50)]
    out = viz.flow_adjusted_returns(rows)
    assert [d for d, _ in out] == [date(2026, 1, 2)]
    assert [r for _, r in out] == pytest.approx([0.05])


def test_period_return_compounds():
    rows = [_nav(1, 100), _nav(2, 110), _nav(3, 121)]
    assert viz.period_return(rows, days=None) == pytest.approx(0.21)


def test_period_return_requires_a_base_snapshot_on_or_before_cutoff():
    """重大-1: 系列の最古日が cutoff より新しければ「期間未充足」で値を出さない。

    設定 2 日目の帳簿で 1W/1M/設定来 が全部同じ数字になる誤りを防ぐ。
    """
    rows = [_nav(1, 100), _nav(2, 110)]  # 8/1 開始・8/2 時点(2 日目)
    assert viz.period_return(rows, days=7) is None
    assert viz.period_return(rows, days=30) is None
    assert viz.period_return(rows, days=None) == pytest.approx(0.10)  # 設定来だけ有効


def test_period_return_base_is_latest_snapshot_on_or_before_cutoff():
    """重大-2: 窓の起点は cutoff **以前の直近**スナップショット(終端日だけで切らない)。

    1/1・1/10・1/25 の系列で 7 日窓の cutoff は 1/18。起点は 1/10 であり、実測期間は
    15 日 = 窓外。値は出すが起点日と「窓外」注記で誤読を防ぐ。
    """
    rows = [_nav(1, 100), _nav(10, 200), _nav(25, 220)]
    assert viz.window_base_index(rows, 7) == 1
    assert viz.period_return(rows, days=7) == pytest.approx(0.10)
    detail = viz.period_detail(rows, label="1W", days=7)
    assert detail.base_day == date(2026, 1, 10)
    assert detail.end_day == date(2026, 1, 25)
    assert detail.lag_days == 8  # cutoff 1/18 より 8 日古い起点
    assert detail.note is not None and "起点が窓外" in detail.note


def test_period_detail_no_note_when_base_is_within_tolerance():
    rows = [_nav(1, 100), _nav(2, 105), _nav(8, 110)]
    detail = viz.period_detail(rows, label="1W", days=7)
    assert detail.base_day == date(2026, 1, 1)  # cutoff 1/1 ちょうど(境界は含む)
    assert detail.lag_days == 0
    assert detail.note is None
    assert detail.value == pytest.approx(0.10)


def test_period_return_none_when_insufficient():
    assert viz.period_return([], days=None) is None
    assert viz.period_return([_nav(1, 100)], days=None) is None  # 1 点ではリターンが立たない
    assert viz.period_return([_nav(1, 100), _nav(2, 110)], days=0) is None


def test_period_returns_table_shape_and_unmet_periods():
    rows = [_nav(1, 100), _nav(2, 110)]
    table = viz.period_returns(rows)
    assert [r.label for r in table] == ["1W(7日)", "1M(30日)", "設定来"]
    assert [r.value_text for r in table] == [viz.MISSING, viz.MISSING, "+10.00%"]
    assert [r.base_text for r in table] == [viz.MISSING, viz.MISSING, "2026-01-01"]
    assert all("期間未充足" in r.note for r in table[:2])
    assert table[-1].note is None
