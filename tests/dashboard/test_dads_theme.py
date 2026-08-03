"""DADS デザイントークン層のテスト(2026-08-03 デザイン改修)。

**このテストで確かめられること/確かめられないこと**を先に断っておく。トークンの
実値(`.streamlit/config.toml`)は静的に読めるので厳密に検査できる。一方、CSS 注入層
(`dashboard/dads.py`)で確かめられるのは「CSS ブロックがページに届いていること」まで
であり、**実際に 44px になっているかは検査できない** — 寸法はブラウザのレイアウト
計算の結果であって、AppTest は DOM もレンダラも持たないためである
(`docs/research/dads-streamlit-application.md` §6-7)。したがって Streamlit を更新して
セレクタが一致しなくなっても、このテストは通ってしまう。実寸は人間が実ブラウザで
確認するしかない、という限界を明示的に受け入れている。

コントラスト比だけは自前で計算して検査する。「4.5:1 を満たす色を選んだ」は設計判断で
あり、値を書き換えたときに黙って基準を割ることを防ぐ価値があるため。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="streamlit 未導入(.[dashboard] を入れると走る)")

import dads  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / ".streamlit" / "config.toml"


@pytest.fixture(scope="module")
def theme() -> dict:
    with _CONFIG.open("rb") as f:
        return tomllib.load(f)["theme"]


# ── コントラスト比(WCAG 2.1 の相対輝度)──────────────────────────────────────
def _luminance(hex_color: str) -> float:
    """WCAG の相対輝度。sRGB 各成分をリニア化して係数付きで足す。"""
    raw = hex_color.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(fg: str, bg: str) -> float:
    lighter, darker = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_contrast_helper_matches_known_values():
    """自前の計算式が正しいことを既知の値で固定する(検査器そのものの検査)。"""
    assert _contrast("#FFFFFF", "#000000") == pytest.approx(21.0, abs=0.01)
    assert _contrast("#FFFFFF", "#FFFFFF") == pytest.approx(1.0, abs=0.01)
    # DADS が「白背景でテキストの下限」と定める Solid Gray-536。
    assert _contrast("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.05)


# ── トークンの実値 ────────────────────────────────────────────────────────────
def test_theme_uses_dads_primitive_values(theme):
    """色は DADS プリミティブの実値そのもの(近い色に置き換えない)。"""
    assert theme["primaryColor"] == "#0017C1"  # Blue-900
    assert theme["textColor"] == "#1A1A1A"  # Solid Gray-900
    assert theme["backgroundColor"] == "#FFFFFF"
    assert theme["secondaryBackgroundColor"] == "#F2F2F2"  # Solid Gray-50
    assert theme["borderColor"] == "#949494"  # Solid Gray-420
    assert theme["linkColor"] == "#0017C1"


def test_theme_typography_and_radius_follow_dads(theme):
    """本文の標準最小 16px(14px 未満は不許可)。角丸は DADS の 8px 段。"""
    assert theme["baseFontSize"] == 16
    assert theme["baseRadius"] == "8px"
    assert theme["font"].startswith("Noto Sans JP")
    # WOFF2 が未取得でも壊れないよう、フォールバックを必ず並べる。
    assert "sans-serif" in theme["font"]


def test_semantic_colors_match_dads_and_pass_text_contrast(theme):
    """差異・超過の色は DADS セマンティックで、かつ白背景で 4.5:1 を満たす。

    既定値(#bd4043 / #158237 / #e2660c、灰は 60% 不透明度)は DADS 外であり、
    とくに既定の灰は 4.5:1 を割る。4色とも明示的に上書きしていることを固定する。
    """
    expected = {
        "redTextColor": dads.ERROR,  # semantic error-1
        "greenTextColor": dads.SUCCESS,  # semantic success-2
        "orangeTextColor": dads.WARNING,  # semantic warning-orange-2
        "grayTextColor": dads.GRAY_TEXT,  # primitive Solid Gray-536
    }
    for key, value in expected.items():
        assert theme[key] == value, key
        assert _contrast(value, theme["backgroundColor"]) >= 4.5, key


def test_primary_and_border_pass_their_respective_minimums(theme):
    """テキスト・リンクは 4.5:1、境界などの非テキストは 3:1(DADS の規定)。"""
    bg = theme["backgroundColor"]
    assert _contrast(theme["textColor"], bg) >= 4.5
    assert _contrast(theme["linkColor"], bg) >= 4.5
    assert _contrast(theme["borderColor"], bg) >= 3.0


def test_dads_module_constants_are_the_single_source_of_the_values(theme):
    """``dads.py`` の定数と config.toml が同じ値を指していること。

    実際に描画色を決めるのは config.toml で、``dads.py`` の定数は Styler など
    Streamlit の色指定を経由できない箇所のためにある。二箇所に散った値が食い違うと
    同じ「エラー色」が画面上で2種類になるため、突き合わせを固定する。
    """
    assert (dads.PRIMARY, dads.BORDER) == (theme["primaryColor"], theme["borderColor"])


# ── フォント(self-host)──────────────────────────────────────────────────────
def test_font_faces_are_self_hosted_and_static_serving_is_on():
    """CDN 配信にしない(閲覧のたびに代表の IP・UA が第三者へ渡るため)。"""
    with _CONFIG.open("rb") as f:
        config = tomllib.load(f)
    faces = config["theme"]["fontFaces"]
    # DADS はウェイトを 400/700 の2段しか使わない。
    assert sorted(face["weight"] for face in faces) == [400, 700]
    for face in faces:
        assert face["family"] == "Noto Sans JP"
        # app/static/... は Streamlit の静的配信(自ホスト)。http(s) が出てきたら CDN。
        assert face["url"].startswith("app/static/fonts/"), face["url"]
    # 静的配信が無効だと上の URL は 404 になる。
    assert config["server"]["enableStaticServing"] is True


def test_font_directory_is_where_streamlit_can_serve_it():
    """配信できるのはエントリポイントと同階層の static/ 配下だけ。

    ``dashboard/fonts/`` に置くと ``app/static/...`` では 404 になるという、
    一度踏むと原因が分かりにくい罠を構造として固定する。
    """
    assert (_ROOT / "dashboard" / "static" / "fonts").is_dir()
    assert not (_ROOT / "dashboard" / "fonts").exists()


def test_font_binaries_ship_the_ofl_license_when_present():
    """OFL 1.1 はライセンス全文の同梱が再配布の条件。WOFF2 だけを残せない。"""
    fonts_dir = _ROOT / "dashboard" / "static" / "fonts"
    woff2 = list(fonts_dir.glob("*.woff2"))
    if not woff2:
        pytest.skip("フォント未取得(./ops/fetch-fonts.sh)。未取得でもアプリは動く")
    assert (fonts_dir / "LICENSE-OFL.txt").is_file()


# ── CSS 注入層(検査できるのは「届いていること」まで)───────────────────────────
def test_css_declares_the_44px_target_size():
    """WCAG 2.1 SC 2.5.5。**実寸は検査できない** — 宣言があることだけを固定する。"""
    css = dads.css()
    assert dads.TARGET_SIZE_PX == 44
    assert f"min-height: {dads.TARGET_SIZE_PX}px" in css
    # 代表の指摘(ページ切替ボタンが小さい)の直接の対象。
    assert '[data-testid="stSidebarNav"] a' in css
    assert ".stButton button" in css


def test_css_declares_dads_line_heights():
    """本文 150%・密な情報表示(表)130%。"""
    assert "line-height: 1.5" in dads.css()
    assert "line-height: 1.3" in dads.css()


def test_css_declares_a_focus_ring_meeting_wcag_minimums():
    """キーボード操作は採用した項目。DADS にトークンが無いため WCAG 一般則に従う。"""
    css = dads.css()
    assert ":focus-visible" in css
    assert f"outline: 2px solid {dads.PRIMARY}" in css
