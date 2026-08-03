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

#: WCAG 2.1 達成基準 2.5.5(ターゲットサイズ)。DADS のボタン規定も同値。
TARGET_SIZE_PX = 44

#: CSS ブロックの目印。テストが「注入されていること」を探す文字列で、
#: 見た目には影響しない(コメントなので描画にも出ない)。変更するときはテストも直すこと。
CSS_MARKER = "ryza-dads-tokens"

_CSS = f"""
<style id="{CSS_MARKER}">
/* {CSS_MARKER}: DADS トークンのうち config.toml で表現できない層。
   Streamlit の内部 DOM 依存(非公式 API)。更新で無言で壊れる — dashboard/dads.py 参照。 */

/* ── ① タップターゲット {TARGET_SIZE_PX}×{TARGET_SIZE_PX} px ───────────────────
   WCAG 2.1 SC 2.5.5。代表の指摘「ページ切替ボタンが小さい」の直接の是正点は
   stSidebarNav(st.navigation が描くページリンク)である。 */
[data-testid="stSidebarNav"] a,
[data-testid="stSidebarNavLink"],
[data-testid="stSidebarNav"] li > div,
section[data-testid="stSidebar"] nav a {{
    min-height: {TARGET_SIZE_PX}px;
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
   :focus-visible なのでマウス操作では出ない(キーボード操作時だけ現れる)。 */
:focus-visible {{
    outline: 2px solid {PRIMARY};
    outline-offset: 2px;
    border-radius: 4px;
}}
</style>
"""


def inject() -> None:
    """DADS の CSS 層をページへ注入する。

    エントリポイントの先頭で毎回呼ぶ(Streamlit はウィジェット操作のたびにスクリプト
    全体を再実行するため、注入も毎回必要)。``st.html`` を使うのは、生の
    ``st.markdown(..., unsafe_allow_html=True)`` と違い**このために用意された公式 API**で
    あり、意図が読み手に伝わるため。
    """
    st.html(_CSS)


def css() -> str:
    """注入する CSS 全文(テストと目視確認のために公開する)。"""
    return _CSS


__all__ = [
    "BORDER",
    "CSS_MARKER",
    "ERROR",
    "GRAY_TEXT",
    "PRIMARY",
    "SUCCESS",
    "TARGET_SIZE_PX",
    "WARNING",
    "css",
    "inject",
]
