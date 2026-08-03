"""dashboard/dads — DADS トークンのうち config.toml で表現できない層を CSS で補う。

代表指示 2026-08-03「ページ切替ボタンが小さい/デジタル庁ガイドライン参考に見直し」への
対応。デジタル庁デザインシステム(DADS)の実値・出典は
``docs/research/dads-streamlit-application.md``、適用範囲の裁定は同 §7。

**役割分担**: 色・タイポ・角丸・フォントは ``.streamlit/config.toml`` の ``[theme]``
(公式 API)で与える。ここに置くのは config.toml に対応する設定が無い2項目だけ:

1. **タップターゲット 44×44 CSS px**(WCAG 2.1 達成基準 2.5.5・DADS ボタン規定)
2. **行間**(DADS: 本文 150% 以上・密な情報表示 120〜130%)

これに、色だけに頼らない表示を支える**フォーカスリング**(WCAG 一般則 2px 以上・3:1。
DADS はフォーカスリングのトークンを配布していない — 調査 §3)を足す。

**この CSS は壊れやすい。** Streamlit の内部 DOM(``data-testid`` と生成クラス名)に
依存しており、非公式 API である。Streamlit を更新するとセレクタが一致しなくなり、
**例外も警告も出さずに無効化する**(調査 §6-7)。CI で「効いていること」は検査できない
(描画は実ブラウザ内で起き、AppTest は DOM を持たない)。したがって:

- 検査できるのは「CSS ブロックがページに注入されていること」まで(``tests/dashboard/
  test_dads_theme.py``)。実寸 44px の検証は人間が実ブラウザで見るしかない。
- セレクタは**一段ゆるく・複数を併記**する(``data-testid`` と クラス名の両方を書く)。
  片方が消えても残りが効く確率を上げる。
- Streamlit のメジャー更新後は、このファイルを目視確認の対象に入れること。

**採用しなかったもの**(裁定 §7): JIS X 8341-3:2016 の全面準拠。スクリーンリーダー対応・
スキップリンク・visited リンク色は、(a) 本ダッシュボードが IAP 許可リスト1名の完全
非公開ツールで支援技術の利用者が存在せず、(b) 中核である ``st.dataframe`` が canvas 描画で
DOM にテキストを持たないため CSS では到達すらできない(調査 §6-1・§6-2・§6-5)。
実利のない準拠コストを払わない、という判断であって「無視してよい」という判断ではない。
前提(利用者が代表1名)が変わったら再評価する。
"""

from __future__ import annotations

import re

import streamlit as st

# ── DADS セマンティックカラー(実値の単一の出所)────────────────────────────────
# 値は npm ``@digital-go-jp/design-tokens`` v1.1.0 の tokens.css。括弧内は白背景での
# コントラスト比で、いずれも**テキストの 4.5:1 を満たす**(調査 §3)。
#
# 実際の描画色は ``.streamlit/config.toml`` の ``redTextColor`` 等が与える —
# ``viz.py`` は ``:red[…]`` のような Streamlit の色指定しか使わず hex を直書きしない。
# ここの定数は「config.toml と同じ値であること」をテストで突き合わせるための参照であり、
# 二重管理ではなく**単一の値を2箇所から検証する**ための固定点である。
ERROR = "#EC0000"  #: semantic error-1 — リミット超過・不利な差異(4.60:1)
SUCCESS = "#197A4B"  #: semantic success-2 — 有利な差異(5.35:1)
WARNING = "#C74700"  #: semantic warning-orange-2 — 警戒・SLA 違反(4.85:1)
GRAY_TEXT = "#767676"  #: primitive Solid Gray-536 — 測れていない値(4.54:1)
PRIMARY = "#0017C1"  #: primitive Blue-900 — キーカラー・リンク(11.1:1)
BORDER = "#949494"  #: primitive Solid Gray-420 — 非テキストの下限(3.00:1)
#: semantic warning-yellow-2 — 組織図で「注意して見るべき節点」(投資委員会・コンプラ
#: ゲート)の強調(4.54:1)。異常ではないので ERROR は使わない。独立役員審査 低-8 で
#: app.py の ``_ORG_ACCENT`` からここへ移した — 色の実値がこのモジュールの外にあると
#: コントラスト検査の対象から漏れる(旧値 #d9a441 は白背景で 2:1 前後しか無かった)。
ACCENT = "#927200"

#: 有彩色の背景に載せる文字色の候補。DADS の本文色(Gray-900)と白の2択にする —
#: 中間調の灰を混ぜても可読性は上がらず、トークンの数だけが増えるため。
ON_LIGHT = "#1A1A1A"  #: primitive Solid Gray-900
ON_DARK = "#FFFFFF"

#: WCAG 2.1 達成基準 2.5.5(ターゲットサイズ)。DADS のボタン規定も同値。
TARGET_SIZE_PX = 44

#: DADS タイポグラフィの下限(本文標準 16px / スペース制約時 14px / 14px 未満は不許可)。
#: rem 指定に使う値で、``baseFontSize = 16`` の下で 0.875rem = 14px。
MIN_FONT_REM = 0.875

#: メンバー色などの外部入力(config/org.yaml・DB)を受け入れる形式。``style`` 属性へ
#: 直に埋める値なので、HTML エスケープだけでは CSS 宣言の追記(``red;position:fixed``)を
#: 防げない(独立役員審査 低-9)。3桁省略形を許さないのは、検証を1本の正規表現に
#: 保ちつつ輝度計算の入力形式を一意にするため。
_HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: 検証を通らない色の代替。Solid Gray-536(白文字で 4.54:1)。
FALLBACK_COLOR = GRAY_TEXT


def safe_color(value: object, fallback: str = FALLBACK_COLOR) -> str:
    """``#RRGGBB`` 形式だけを通し、それ以外は ``fallback`` に落とす。

    台帳(config/org.yaml)と DB 上書きの色を ``style`` 属性へ埋める前に必ず通す。
    黙って落とすのは、色は装飾であって欠けても情報が失われないためである
    (欠けたら困る値なら例外にすべきだが、ここは既定色で描き続ける方が良い)。
    """
    text = value if isinstance(value, str) else ""
    return text if _HEX6.match(text) else fallback


def relative_luminance(hex_color: str) -> float:
    """WCAG 2.1 の相対輝度。sRGB 各成分をリニア化して係数付きで足す。"""
    raw = safe_color(hex_color).lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(fg: str, bg: str) -> float:
    """2 色のコントラスト比(1.0〜21.0)。"""
    lighter, darker = sorted((relative_luminance(fg), relative_luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def text_on(background: str) -> str:
    """``background`` の上に置く文字色を、コントラストが高い方から選ぶ。

    キャラクター色は台帳が自由に決める(``config/org.yaml``)ため、文字色を白に
    固定すると淡い色のメンバーで 4.5:1 を割る — 実際 ``#a78bfa`` は白文字で
    **2.72:1** しか無かった(独立役員審査 重要-2)。背景の輝度で黒/白を選べば、
    どの色が来ても機械的に良い方を取れる。
    """
    return max((ON_LIGHT, ON_DARK), key=lambda fg: contrast_ratio(fg, background))

#: CSS ブロックの目印。テストが「注入されていること」を探す文字列で、
#: 見た目には影響しない(コメントなので描画にも出ない)。変更するときはテストも直すこと。
CSS_MARKER = "ryza-dads-tokens"

_CSS = f"""
<style id="{CSS_MARKER}">
/* {CSS_MARKER}: DADS トークンのうち config.toml で表現できない層。
   Streamlit の内部 DOM 依存(非公式 API)。更新で無言で壊れる — dashboard/dads.py 参照。 */

/* ── ① タップターゲット {TARGET_SIZE_PX}×{TARGET_SIZE_PX} px ───────────────────
   WCAG 2.1 SC 2.5.5。代表の指摘「ページ切替ボタンが小さい」の直接の是正点は
   stSidebarNav(st.navigation が描くページリンク)である。

   **min-height だけを広めのセレクタに当てる**(独立役員審査 低-7)。min-height は
   要素が既に十分高ければ何も起きない冪等な指定で、意図しない要素に当たっても実害が
   ない。一方 display/align-items/padding はレイアウトを作り替えるため、当たり所を
   間違えると崩れる —— これらは Streamlit 1.60 で実測して確認した
   stSidebarNavLink(実体は <a>)にだけ与える。 */
[data-testid="stSidebarNav"] a,
[data-testid="stSidebarNavLink"],
[data-testid="stSidebarNav"] li > div,
section[data-testid="stSidebar"] nav a {{
    min-height: {TARGET_SIZE_PX}px;
}}

/* 実測で確認済みのセレクタにだけレイアウトを与える。Streamlit 側の既定でも縦中央
   揃えになっているが、min-height で伸びた分の中央揃えを自前で保証しておく。 */
[data-testid="stSidebarNavLink"] {{
    display: flex;
    align-items: center;
    /* 8px グリッド(DADS 余白の基本単位)。ターゲット領域を余白で確保する。 */
    padding-top: 8px;
    padding-bottom: 8px;
}}

.stButton button,
.stFormSubmitButton button,
.stDownloadButton button,
.stLinkButton a,
[data-testid="stBaseButton-secondary"],
[data-testid="stBaseButton-primary"] {{
    min-height: {TARGET_SIZE_PX}px;
}}

/* 入力系。select / text input / chat input は「押す」対象なので同じ下限を与える。 */
.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div,
.stTextInput input,
.stNumberInput input,
.stDateInput input,
.stChatInput textarea {{
    min-height: {TARGET_SIZE_PX}px;
}}

/* チェックボックス・ラジオは四角/丸自体を大きくすると DADS の見た目から外れるため、
   クリック領域(ラベル全体)の高さで {TARGET_SIZE_PX}px を確保する。 */
.stCheckbox label,
.stRadio label,
.stToggle label {{
    min-height: {TARGET_SIZE_PX}px;
    align-items: center;
}}

/* 展開ヘッダ・タブも切替操作なので同扱い。 */
.stExpander summary,
[data-testid="stExpanderHeader"],
.stTabs button[role="tab"] {{
    min-height: {TARGET_SIZE_PX}px;
}}

/* ── ② 行間(DADS タイポグラフィ)────────────────────────────────────────────
   本文 150% 以上。見出しは 140% で、既定より詰める方向には動かさない。 */
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
[data-testid="stCaptionContainer"] p,
.stMarkdown p,
.stMarkdown li {{
    line-height: 1.5;
}}

[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3,
[data-testid="stMarkdownContainer"] h4 {{
    line-height: 1.4;
}}

/* 密な情報表示(表)は 130%。**st.dataframe には効かない** — glide-data-grid の
   canvas 描画で行の高さは JS 側が決めており CSS が届かない(調査 §6-1)。
   効くのは st.table と、markdown で書いた表だけである。 */
[data-testid="stTable"] td,
[data-testid="stTable"] th,
[data-testid="stMarkdownContainer"] table td,
[data-testid="stMarkdownContainer"] table th {{
    line-height: 1.3;
}}

/* ── ③ フォーカスリング ──────────────────────────────────────────────────────
   キーボード操作は裁定で「実利のあるアクセシビリティ」として採用した項目。DADS は
   フォーカスリングのトークンを配布していないため WCAG 一般則(2px 以上・3:1)に従う。
   :focus-visible なのでマウス操作では出ない(キーボード操作時だけ現れる)。

   border-radius は**指定しない**(独立役員審査 低-6): フォーカスした瞬間だけ要素の
   角丸が baseRadius(8px)から 4px へ変形し、リングではなく要素そのものが動いて
   見える。outline は要素の角丸に沿って描かれるので、そもそも指定する必要がない。 */
:focus-visible {{
    outline: 2px solid {PRIMARY};
    outline-offset: 2px;
}}
</style>
"""


def inject() -> None:
    """DADS の CSS 層をページへ注入する。

    Streamlit はウィジェット操作のたびにスクリプト全体を再実行するため、注入も毎回
    必要になる。**各ページ関数の先頭**で呼ぶ(``_build_pages`` の ``_with_dads_css``
    が全ページに被せている)。

    **``st.html`` ではなく ``st.markdown(unsafe_allow_html=True)`` を使う**
    (2026-08-03 実ブラウザ検証)。``st.html`` は「内容が style タグだけの場合、
    場所を取らないよう**イベントコンテナ**へ送る」という仕様で、本アプリの
    ``st.navigation`` 構成ではそのコンテナが DOM に現れず、CSS が**一切適用されな
    かった**(ナビのタップターゲットが 28px = 素の高さのまま)。``st.markdown`` は
    同じ ``<style>`` を通常のコンテナへ出し、実測で適用されることを確認している
    (同じ仕組みで描いている組織図 ``_ORG_CSS`` が以前から効いていた)。

    呼ぶ場所も効き方に影響する。``st.navigation`` の ``page.run()`` は**メイン
    コンテナをリセットする**ため、エントリポイントが ``page.run()`` より前に書いた
    要素はブラウザに届かない。サイドバーへ逃がす案は**もっと悪い** —— サイドバーは
    折り畳むとコンテナごと unmount され、CSS が丸ごと消える。ページ自身の描画パスの
    中で書くのが唯一の安全な位置である。

    これらの失敗は **AppTest では一つも検出できなかった**。AppTest は送出された delta を
    すべて集めるので「要素は存在する」と報告し、フロントエンドのコンテナ挙動を再現
    しない —— テストは緑のままブラウザでは無効、というこのモジュール冒頭の但し書き
    そのものの事例である。テスト側は「``main()`` で呼ばず、ページ側で呼ぶ」という
    **構造**までは検査するが、実効性は実ブラウザでしか確認できない。
    """
    st.markdown(_CSS, unsafe_allow_html=True)


def css() -> str:
    """注入する CSS 全文(テストと目視確認のために公開する)。"""
    return _CSS


__all__ = [
    "ACCENT",
    "BORDER",
    "CSS_MARKER",
    "ERROR",
    "FALLBACK_COLOR",
    "GRAY_TEXT",
    "MIN_FONT_REM",
    "ON_DARK",
    "ON_LIGHT",
    "PRIMARY",
    "SUCCESS",
    "TARGET_SIZE_PX",
    "WARNING",
    "contrast_ratio",
    "css",
    "inject",
    "relative_luminance",
    "safe_color",
    "text_on",
]
