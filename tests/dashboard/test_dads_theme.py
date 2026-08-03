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

import ast
import io
import re
import tokenize
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


def test_accent_is_inside_the_single_source_and_passes_text_contrast():
    """低-8: ``_ORG_ACCENT`` を ``dads.ACCENT`` へ移し、検査対象に入れた。

    実値が dads.py の外にあると、このファイルのコントラスト検査から漏れる —— 実際
    旧値 ``#d9a441`` は白背景で 2:1 前後しか無いまま組織図の文字色に使われていた。
    """
    assert dads.ACCENT == "#927200"  # DADS semantic warning-yellow-2
    assert _contrast(dads.ACCENT, "#FFFFFF") >= 4.5


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


def test_focus_ring_does_not_deform_the_element(theme):
    """低-6: ``:focus-visible`` に border-radius を書かない。

    書くとフォーカスした瞬間だけ角丸が baseRadius(8px)から 4px へ変わり、リングが
    出たのではなく**要素そのものが動いた**ように見える。outline は要素の角丸に沿って
    描かれるので指定する必要がない。
    """
    block = next(d for s, d in _css_rules(dads.css()) if s == ":focus-visible")
    assert "border-radius" not in block
    assert theme["baseRadius"] == "8px"  # 変形の比較対象(これに沿って描かれる)


def test_layout_forcing_is_limited_to_the_verified_selector():
    """低-7: display/padding は実測で確認したセレクタにだけ与える。

    ``min-height`` は既に十分高い要素には何も起きない冪等な指定なので、防御的に
    広いセレクタへ当ててよい。一方 display/align-items/padding はレイアウトを
    作り替えるため、当たり所を間違えると崩れる —— 緩いセレクタに付けると
    「壊れにくくする」つもりが偽陽性のリスクだけを上げる。
    """
    nav_rules = [(s, d) for s, d in _css_rules(dads.css()) if "stSidebarNav" in s]
    forcing = [s for s, d in nav_rules if "display:" in d or "padding-" in d]
    # 実測済み(Streamlit 1.60 で <a> であることを DevTools で確認)の1本だけ。
    assert forcing == ['[data-testid="stSidebarNavLink"]'], nav_rules
    # 広いセレクタ側は min-height だけ(冪等な指定なので当たっても実害がない)。
    broad = next(s for s, d in nav_rules if s not in forcing)
    assert "min-height" in dict(nav_rules)[broad]


# ── 色・文字サイズの走査(重要-2: config.toml だけ見ていては足りない)─────────────
_APP = _ROOT / "dashboard" / "app.py"
_VIZ = _ROOT / "dashboard" / "viz.py"


def _code_only(path: Path) -> str:
    """コメントと docstring を潰したソース(規約の**説明文**を走査対象から外す)。

    素朴な正規表現(``#.*$`` を消す)は使えない —— CSS の ``#RRGGBB`` を行コメントの
    開始と誤認し、``font-size`` を含む行の残りごと消してしまう(走査が空振りしても
    テストは通るので、この取り違えは静かに検査を無効化する)。``tokenize`` は
    文字列リテラル内の ``#`` をコメントと見なさないので、そこだけは言語の側に任せる。

    範囲を消さずに**同じ長さの空白で潰す**のは、後段の AST 由来の行・列オフセットを
    ずらさないため。処理は後方から行い、複数行 docstring の潰しが前方の座標へ
    影響しないようにする。
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    spans: list[tuple[int, int, int, int]] = []

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            spans.append((*token.start, *token.end))

    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if not isinstance(node, holders) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(getattr(first.value, "value", None), str):
            spans.append(
                (first.lineno, first.col_offset, first.end_lineno, first.end_col_offset)
            )

    for lineno, col, end_lineno, end_col in sorted(spans, reverse=True):
        if lineno == end_lineno:
            line = lines[lineno - 1]
            lines[lineno - 1] = line[:col] + " " * (end_col - col) + line[end_col:]
            continue
        lines[lineno - 1] = lines[lineno - 1][:col] + "\n"
        for i in range(lineno, end_lineno - 1):
            lines[i] = "\n"
        lines[end_lineno - 1] = " " * end_col + lines[end_lineno - 1][end_col:]
    # 自前 CSS(文字列リテラル)の中の /* … */ も説明文。Python のコメントではないので
    # tokenize では落ちず、ここで別途潰す。
    return re.sub(r"/\*.*?\*/", "", "".join(lines), flags=re.DOTALL)


def _css_rules(css: str) -> list[tuple[str, str]]:
    """CSS を ``[(セレクタ, 宣言ブロック)]`` にする(``/* … */`` コメントは除去)。"""
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    body = body.split("<style", 1)[1].split(">", 1)[1].split("</style>", 1)[0]
    rules = []
    for chunk in body.split("}"):
        if "{" not in chunk:
            continue
        selector, _, declarations = chunk.partition("{")
        rules.append((selector.strip(), declarations.strip()))
    return rules


def test_app_py_has_no_hardcoded_hex_colors_outside_the_token_module():
    """``app.py`` の色も ``dads.py`` 経由に保つ(viz.py と同型の走査)。

    改修前の ``app.py`` は Styler に ``color: orange``、組織図に ``#d9a441`` /
    ``#c9a24b`` を直書きしており、どちらも 4.5:1 を割っていた。テストが
    ``config.toml`` しか読んでいなかったため検出できなかった(独立役員審査 重要-2)。
    例外は ``_REPRESENTATIVE_COLOR``(台帳に載らない代表カードの色。``safe_color`` と
    ``text_on`` を通す)だけで、それも定数として1箇所に置く。
    """
    code = _code_only(_APP)
    found = set(re.findall(r"#[0-9A-Fa-f]{3,8}\b", code))
    assert found <= {"#64748B"}, found
    # CSS の色名も同様に禁止(orange は白背景で 4.5:1 を割る)。
    assert not re.search(r"color:\s*(orange|red|green|yellow|blue)\b", code)


def test_app_py_writes_no_font_size_literal_below_the_dads_minimum():
    """重要-2: ソースに 14px 未満の font-size **リテラル**を書かない。

    ``config.toml`` が「14px 未満は不許可」と宣言する一方、同じ改修で書き換えた
    ``app.py`` の自前 CSS だけが 10.4〜13.6px のまま取り残されていた。宣言と実装の
    食い違いは、宣言側だけを読むテストでは永久に見つからない。

    **このテストが見るのはリテラルだけ**である。是正後の値は
    ``font-size:{dads.MIN_FONT_REM}rem`` という f-string の補間で書かれており、
    ソース上に数字が現れない —— つまりここを通っても「実際に 14px 以上で描かれる」
    保証にはならない。実効値は描画後の CSS を見る
    ``test_app_pages.test_rendered_css_has_no_font_size_below_the_dads_minimum``
    が担当する。ここは**新しく直書きされたリテラル**を捕まえるための番人。
    """
    code = _code_only(_APP)
    for value, unit in re.findall(r"font-size:\s*([0-9.]+)(rem|em|px)", code):
        px = float(value) * (1 if unit == "px" else 16)
        assert px >= dads.MIN_FONT_REM * 16, f"{value}{unit} = {px}px"


def test_font_size_literal_scan_is_not_vacuous():
    """上の走査が実際に宣言を拾えていること(検査器そのものの検査)。

    ``#RRGGBB`` を行コメント開始と誤認する素朴な実装だと、走査結果が空になっても
    テストは通ってしまう —— 静かに無効化された検査は無いより悪い。
    """
    literals = re.findall(r"font-size:\s*([0-9.]+)(rem|em|px)", _code_only(_APP))
    assert len(literals) >= 2, literals
    assert "font-size" in _code_only(_APP)


def test_viz_py_also_stays_free_of_hardcoded_hex():
    """viz.py 側の同じ規約(色の変更点を config.toml 一箇所に保つ)。"""
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", _code_only(_VIZ))


# ── 外部入力の色(低-9)+ 背景輝度による文字色選択(重要-2)─────────────────────
@pytest.mark.parametrize(
    "value",
    [
        "red;position:fixed;top:0",  # 同じ style 属性へ CSS 宣言を追記する経路
        "url(https://example.com/x)",
        "#888",  # 3桁省略形は通さない(輝度計算の入力形式を一意に保つ)
        "#12345",
        "rgb(0,0,0)",
        "",
        None,
        123,
    ],
)
def test_safe_color_rejects_anything_but_six_digit_hex(value):
    """``style`` へ埋める色は ``#RRGGBB`` だけ。

    ``html.escape`` は引用符しか潰さないため、属性値の中で ``;`` を使った宣言追記を
    防げない(独立役員審査 低-9)。色は装飾なので、弾いたら既定色で描き続ける。
    """
    assert dads.safe_color(value) == dads.FALLBACK_COLOR


def test_safe_color_passes_valid_hex_unchanged():
    assert dads.safe_color("#a78bfa") == "#a78bfa"
    assert dads.safe_color("#A78BFA") == "#A78BFA"


def test_fallback_color_itself_meets_text_contrast():
    """既定色に落ちたカードも 4.5:1 を満たすこと(逃げ場が基準割れでは意味がない)。"""
    assert _contrast(dads.text_on(dads.FALLBACK_COLOR), dads.FALLBACK_COLOR) >= 4.5


def test_text_on_picks_the_readable_colour_for_every_ledger_colour():
    """重要-2: 台帳の全キャラクター色でアバター文字が 4.5:1 を満たす。

    白固定だった旧実装は ``#a78bfa`` で 2.72:1、``#059669`` で 3.77:1、既定 ``#888``
    で 3.54:1 と、9 人中 3 人がテキスト下限を割っていた。台帳は色を自由に決めてよい
    設計なので、描画側が輝度で黒/白を選ぶ。新しいメンバーが増えても自動で守られる。
    """
    import yaml

    ledger = yaml.safe_load((_ROOT / "config" / "org.yaml").read_text(encoding="utf-8"))
    colours = [m["color"] for m in ledger["members"] if m.get("color")]
    assert len(colours) >= 9, colours
    for colour in colours:
        chosen = dads.text_on(dads.safe_color(colour))
        assert chosen in (dads.ON_LIGHT, dads.ON_DARK)
        assert _contrast(chosen, colour) >= 4.5, (colour, chosen)


def test_dads_contrast_helper_agrees_with_the_test_helper():
    """``dads.contrast_ratio`` と本ファイルの検査器が一致すること(相互検算)。"""
    for fg, bg in ((dads.ERROR, "#FFFFFF"), (dads.ON_DARK, "#a78bfa"), (dads.ACCENT, "#FFFFFF")):
        assert dads.contrast_ratio(fg, bg) == pytest.approx(_contrast(fg, bg), abs=1e-9)
