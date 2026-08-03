"""dashboard/viz — ダッシュボード共通の可視化ヘルパ(T-018)。

**目的は「禁止記法が構造的に混入しない」こと**。ページ実装(``app.py``)は数値を直接
``st.metric`` や ``st.bar_chart`` に流さず、必ずここのヘルパを通す。ヘルパが提供するのは
知覚精度順位(位置 > 長さ > 角度 > 面積 > 色。Cleveland & McGill 1984)の上位だけを使う
表示形と、比較文脈つきの数値フォーマッタである。根拠と禁止記法の一覧は
``docs/research/dashboard-visualization-guidelines.md``。

提供するもの:

- **bullet 型**(``make_bullet`` / ``render_bullet``): 実績値 + リミット/目標 + 使用率を
  「テキスト + ``st.progress``(長さ符号化)」の組で描く。Few が 2005 年にゲージの代替と
  して設計した bullet graph の、Streamlit ネイティブ部品だけによる縮約版。matplotlib 等の
  重い依存は追加しない(A2: 円形ゲージ禁止、A9: 比較文脈のない単独数値カード禁止)。
- **underwater(DD)図**(``underwater_frame`` / ``render_underwater``): NAV 系列から
  設定来ピーク比の下落率(≤0)を出し ``st.area_chart`` で描く。NAV ラインの直下に同じ
  index で置くと横軸が揃う。
- **共通フォーマッタ**(``fmt_sig`` / ``fmt_jpy`` / ``fmt_pct`` / ``fmt_delta_md``):
  有効桁 2〜3 桁(A7: false precision 禁止)と JPY 表記(万/億)を一箇所に集約する。
- **期間リターン**(``flow_adjusted_returns`` / ``period_return``): 外部フロー調整済み
  日次リターンの複利。定義は ``ryza.risk.engine.book_returns`` と同一
  (``r_t = (nav_t − flow_t − nav_{t−1}) / nav_{t−1}``)。

**色の規約**: 赤・緑は「差異(前日比・対計画)」と「リミット超過」だけに予約する
(IBCS。A12)。カテゴリ識別・通常状態の強調には使わない。したがって「正常」は緑では
なく無着色で描く。

テストは ``tests/dashboard/test_viz.py``(DB 不要の純ロジック + 境界値)。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

import pandas as pd
import streamlit as st

#: 値が取れないときの表示(空文字にしない — 「0」との区別がつかなくなるため)。
MISSING = "—"

#: 期間リターン表の既定期間。日次 NAV スナップショットから計算できる範囲だけを置く
#: (3M/1Y/設定来のうち、系列が短い間は 1W/1M/設定来で足りる)。ラベルに暦日数を
#: 明記するのは「1M」が営業月でも暦月でもなく **30 暦日**の窓だからである(低-13)。
DEFAULT_PERIODS: tuple[tuple[str, int | None], ...] = (
    ("1W(7日)", 7),
    ("1M(30日)", 30),
    ("設定来", None),
)

#: 窓の起点が cutoff からこれ以上古いと「起点が窓外」と注記する(暦日)。
#: スナップショットは営業日にしか無いため、連休を挟むと数日ずれるのが正常。
WINDOW_LAG_TOLERANCE_DAYS = 3


# ── フォーマッタ ──────────────────────────────────────────────────────────────
def fmt_sig(value: Any, digits: int = 3) -> str:
    """**画面表示専用**の有効桁 ``digits`` 桁表記(3桁区切り付き)。

    「小数点以下を何桁出すか」を有効桁数に合わせる方式で、**整数部は丸めない**
    (¥1,234 を ¥1,230 と書き換えると金額としては別物になるため)。したがって
    「有効桁 ``digits`` 桁**以下**にはしない」保証であって、桁数の上限保証ではない。

    **用途の限定**: 表示のためだけに使うこと。会計・リスク計算の丸めには使わない
    (計算は Decimal のまま行い、丸めた文字列を再パースして計算に戻さない)。0 は
    "0"、None/NaN/Inf は :data:`MISSING`。
    """
    if value is None:
        return MISSING
    try:
        v = float(value)
    except (TypeError, ValueError):
        return MISSING
    if math.isnan(v) or math.isinf(v):
        return MISSING
    if v == 0:
        return "0"
    exponent = math.floor(math.log10(abs(v)))
    decimals = max(digits - 1 - exponent, 0)
    return f"{v:,.{decimals}f}"


def fmt_jpy(value: Any, digits: int = 3) -> str:
    """JPY 表記。1万以上は「万」、1億以上は「億」に丸めて桁を読みやすくする。

    **小数は ¥1 未満のときだけ出す**(低-13)。円は最小単位が 1 円であり、¥1,234.5 の
    ような表記は存在しない精度を主張する(A7 false precision)。¥1 未満だけは LLM
    コストのような微小額を 0 に潰さないために 2 桁残す。
    """
    if value is None:
        return MISSING
    try:
        v = float(value)
    except (TypeError, ValueError):
        return MISSING
    if math.isnan(v) or math.isinf(v):
        return MISSING
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a == 0:
        return "¥0"
    if a >= 1e8:
        return f"{sign}¥{fmt_sig(a / 1e8, digits)}億"
    if a >= 1e4:
        return f"{sign}¥{fmt_sig(a / 1e4, digits)}万"
    if a >= 1:
        return f"{sign}¥{a:,.0f}"
    return f"{sign}¥{a:,.2f}"


def fmt_pct(value: Any, digits: int = 1) -> str:
    """比率(0.153)→ パーセント表記("15.3%")。"""
    if value is None:
        return MISSING
    try:
        v = float(value)
    except (TypeError, ValueError):
        return MISSING
    if math.isnan(v) or math.isinf(v):
        return MISSING
    return f"{v * 100:.{digits}f}%"


def fmt_signed_pct(value: Any, digits: int = 2) -> str:
    """符号つきパーセント("+1.23%" / "-1.23%")。差異(前日比・対計画)専用。"""
    if value is None:
        return MISSING
    try:
        v = float(value)
    except (TypeError, ValueError):
        return MISSING
    if math.isnan(v) or math.isinf(v):
        return MISSING
    return f"{v * 100:+.{digits}f}%"


def fmt_delta_md(
    value: Any,
    text: str | None = None,
    *,
    good_when: Literal["positive", "negative"] = "positive",
) -> str:
    """差異を色つき markdown にする(緑=有利・赤=不利。IBCS の variance 記法)。

    ``good_when="negative"`` は「小さいほど良い」差異(コスト超過・スリッページ・
    トラッキングエラーなど)で色を反転させる。符号の向きと「有利/不利」は指標ごとに
    違うため、呼び出し側が必ず意味を宣言する(既定は増加=有利)。

    ``text`` を省略すると ``fmt_signed_pct`` の結果を使う。値が取れないとき・0 のときは
    無着色(0 は差異ではない)。
    """
    body = text if text is not None else fmt_signed_pct(value)
    if body == MISSING or value is None:
        return MISSING
    try:
        v = float(value)
    except (TypeError, ValueError):
        return body
    if v == 0 or math.isnan(v):
        return body
    favourable = v > 0 if good_when == "positive" else v < 0
    return f":green[{body}]" if favourable else f":red[{body}]"


def fmt_hours(value: Any, digits: int = 1) -> str:
    """時間(h)表記。24h 以上は日に換算する。"""
    if value is None:
        return MISSING
    try:
        v = float(value)
    except (TypeError, ValueError):
        return MISSING
    if math.isnan(v) or math.isinf(v):
        return MISSING
    if abs(v) >= 24:
        return f"{v / 24:.{digits}f}日"
    return f"{v:.{digits}f}h"


# ── bullet 型 ─────────────────────────────────────────────────────────────────
#: 使用率がこの割合を超えたら警戒表示(絶対値のソフトリミットが無い指標の既定)。
DEFAULT_WARN_AT = 0.75


@dataclass(frozen=True)
class Bullet:
    """bullet 型 1 本の表示状態(描画から分離してテスト可能にした値オブジェクト)。"""

    label: str
    actual: float | None
    limit: float | None
    ratio: float  #: ``st.progress`` に渡す 0..1 に切り詰めた使用率
    usage: float | None  #: 切り詰めない使用率(actual / limit)
    level: str  #: ok | warn | breach | unknown
    actual_text: str
    limit_text: str
    note: str | None = None

    @property
    def text(self) -> str:
        """progress バーに添える 1 行(実績・リミット・使用率を必ず併記する)。"""
        if self.level == "unknown":
            body = f"{self.label}: {self.actual_text} / 上限 {self.limit_text}(使用率 {MISSING})"
            if self.note:
                body = f"{body} — {self.note}"
            return f":gray[{body}]"
        body = (
            f"{self.label}: {self.actual_text} / 上限 {self.limit_text}"
            f"(使用率 {fmt_pct(self.usage, 0)})"
        )
        if self.note:
            body = f"{body} — {self.note}"
        if self.level == "breach":
            return f":red[{body}]"
        if self.level == "warn":
            return f":orange[{body}]"
        return body


def make_bullet(
    label: str,
    actual: Any,
    limit: Any,
    *,
    fmt=fmt_pct,
    soft_limit: Any = None,
    warn_at: float | None = DEFAULT_WARN_AT,
    note: str | None = None,
) -> Bullet:
    """実績値とリミットから :class:`Bullet` を組む。

    判定は境界を含む(``actual >= limit`` で breach)。リスクリミットの「到達」で
    フラグが立つ IPS §3.2 の規約に合わせている。``limit`` が None・0 以下、または
    ``actual`` が None のときは ``level='unknown'``(比率を偽造しない)。
    """
    a = _as_float(actual)
    lim = _as_float(limit)
    soft = _as_float(soft_limit)
    actual_text = fmt(a) if a is not None else MISSING
    # リミットが 0 以下は「比率が定義できない設定ミス」であって上限値ではない。
    # 画面に -100.0% のような偽の上限を出さない(低-12)。
    limit_text = fmt(lim) if lim is not None and lim > 0 else MISSING
    if a is None or lim is None or lim <= 0:
        return Bullet(
            label=label, actual=a, limit=lim, ratio=0.0, usage=None, level="unknown",
            actual_text=actual_text, limit_text=limit_text, note=note,
        )
    usage = a / lim
    ratio = min(max(usage, 0.0), 1.0)
    if a >= lim:
        level = "breach"
    elif soft is not None and a >= soft:
        level = "warn"
    elif soft is None and warn_at is not None and usage >= warn_at:
        level = "warn"
    else:
        level = "ok"
    return Bullet(
        label=label, actual=a, limit=lim, ratio=ratio, usage=usage, level=level,
        actual_text=actual_text, limit_text=limit_text, note=note,
    )


def render_bullet(bullet: Bullet, *, target=None) -> None:
    """bullet 1 本を描く(長さ符号化の progress + 比較文脈つきテキスト)。"""
    (target or st).progress(bullet.ratio, text=bullet.text)


def render_bullets(bullets: Sequence[Bullet], *, target=None, by_usage: bool = True) -> None:
    """bullet 一覧。危ない順に上へ並べる。

    並びは「**unknown(測れていない)を最上段** → 使用率の高い順」。測れていない
    リミットは「安全」ではなく「安全が確認できていない」であり、fail-closed の思想
    では最下段に沈めてはならない(低-12)。
    """
    items = list(bullets)
    if by_usage:
        items.sort(
            key=lambda b: (
                b.level != "unknown",
                -(b.usage if b.usage is not None else 0.0),
            )
        )
    for b in items:
        render_bullet(b, target=target)


# ── 比率バー(リミット比較ではない進捗・確信度)────────────────────────────────
def render_ratio(label: str, value: Any, *, suffix: str = "", target=None) -> None:
    """0..1 の比率をそのまま長さで見せる(自己申告の確信度など)。

    bullet と違い「上限」の概念が無い量に使う。``st.progress`` をページ実装から
    直接呼ばせないための入り口でもある(中-8。CI が app.py の直書きを禁止する)。
    """
    v = _as_float(value)
    ratio = 0.0 if v is None else min(max(v, 0.0), 1.0)
    body = f"{label}: {fmt_pct(v, 0) if v is not None else MISSING}{suffix}"
    (target or st).progress(ratio, text=body if v is not None else f":gray[{body}]")


def render_count_ratio(label: str, done: Any, total: Any, *, target=None) -> None:
    """「済 X / 全 Y(Z%)」の進捗バー。分母を必ず併記する(比較文脈)。"""
    d = int(_as_float(done) or 0)
    t = int(_as_float(total) or 0)
    ratio = min(max(d / t, 0.0), 1.0) if t else 0.0
    pct = fmt_pct(d / t, 0) if t else MISSING
    (target or st).progress(ratio, text=f"{label}: {d} / {t}({pct})")


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


# ── 状態インジケータ(二値・少状態は bullet にしない)───────────────────────────
def render_state(label: str, text: str, *, alert: bool, target=None) -> None:
    """Kill Switch のような二値/少状態の表示。

    bullet(長さ符号化)は連続量のための形であり、二値には情報がない。テキスト +
    「異常時だけ赤」の状態インジケータにする(正常を緑にしないのは、緑を差異表示に
    予約しているため)。
    """
    t = target or st
    if alert:
        t.markdown(f"**{label}**: :red[⛔ {text}]")
    else:
        t.markdown(f"**{label}**: {text}")


def page_question(question: str, *, target=None) -> None:
    """ページ冒頭の「このページで答えられる問い」(IBCS の Say — メッセージを述べる)。"""
    (target or st).caption(f"このページで答えられる問い: {question}")


# ── NAV / underwater ─────────────────────────────────────────────────────────
def nav_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """NAV 系列 → ``day`` を index にした 1 列 DataFrame(``st.line_chart`` 用)。"""
    if not rows:
        return pd.DataFrame({"NAV": []})
    frame = pd.DataFrame(
        {"day": [r["day"] for r in rows], "NAV": [float(r["nav"]) for r in rows]}
    )
    return frame.set_index("day")


def underwater_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """NAV 系列 → 設定来ピーク比の下落率(%・0 以下)の DataFrame。

    ``dd_t = (nav_t / max(nav_1..nav_t) − 1) × 100``。ピーク定義は
    ``ryza.risk.engine.drawdown``(設定来ピーク・連続測定・IPS §3.1)と同じで、
    外部フローの調整は入れない(NAV そのものの水没度合いを見る図であるため)。
    """
    if not rows:
        return pd.DataFrame({"DD(%)": []})
    days: list[Any] = []
    values: list[float] = []
    peak = float("-inf")
    for r in rows:
        nav = float(r["nav"])
        peak = max(peak, nav)
        days.append(r["day"])
        values.append(0.0 if peak <= 0 else (nav / peak - 1.0) * 100.0)
    return pd.DataFrame({"day": days, "DD(%)": values}).set_index("day")


def render_underwater(rows: Sequence[Mapping[str, Any]], *, target=None) -> None:
    """underwater(DD)図。NAV ラインの直下に置くと index が同じで横軸が揃う。"""
    (target or st).area_chart(underwater_frame(rows))


# ── 期間リターン ──────────────────────────────────────────────────────────────
def flow_adjusted_returns(rows: Sequence[Mapping[str, Any]]) -> list[tuple[date, float]]:
    """外部フロー調整済みの日次リターン列 ``[(day, r)]``。

    ``r_t = (nav_t − flow_eop_t) / (nav_{t−1} + flow_bop_t) − 1``。定義は
    ``ryza.risk.engine.book_returns`` と同一(出資・払戻をリターンに数えない TWR)。
    ``flow_eop`` は測定日当日の仕訳、``flow_bop`` は前の測定日より後・当日より前の仕訳
    (``queries.fetch_nav_data`` が ``ryza.risk.navflow`` の規約で寄せた値 — 休日の
    仕訳はここに入り、その区間の**運用元本**なので分母に足す)。
    分母(運用元本)が 0 以下の区間は測定できないので除外する。
    """
    out: list[tuple[date, float]] = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        base = float(prev["nav"]) + float(cur.get("flow_bop") or 0)
        if base <= 0:
            continue
        out.append((cur["day"], (float(cur["nav"]) - float(cur.get("flow_eop") or 0)) / base - 1.0))
    return out


@dataclass(frozen=True)
class PeriodReturn:
    """1 期間の複利リターンと、その**測定窓の実体**(起点日・ずれ・充足性)。

    値だけを出すと「1W」と書かれた数字が実は 21 日分だった、設定 2 日目の帳簿で
    1W/1M/設定来が全部同じ値だった、という誤読を招く(重大-1・重大-2)。窓の起点を
    必ず併記できるようにするための値オブジェクト。
    """

    label: str
    days: int | None
    value: float | None
    base_day: date | None
    end_day: date | None
    lag_days: int | None  #: 起点が cutoff より何暦日古いか(0 = 窓ぴったり)
    note: str | None

    @property
    def value_text(self) -> str:
        return fmt_signed_pct(self.value)

    @property
    def base_text(self) -> str:
        return str(self.base_day) if self.base_day is not None else MISSING


def window_base_index(rows: Sequence[Mapping[str, Any]], days: int | None) -> int | None:
    """測定窓の**起点スナップショット**の index。期間未充足なら None。

    定義(重大-2 の是正): 起点は「``cutoff = 最終日 − days`` **以前の直近**
    スナップショット」。NAV は営業日にしか無いため cutoff ちょうどの行は普通存在せず、
    「cutoff より後の行だけを合成する」旧実装は基準 NAV の日付を見ないまま窓の外の
    区間を混ぜていた。

    ``cutoff`` 以前の行が 1 本も無い場合は **期間未充足** として None を返す
    (重大-1 の是正)。設定 2 日目の帳簿で 1W/1M/設定来が同じ値になる誤りを防ぐ。
    ``days=None``(設定来)の起点は最古のスナップショット。
    """
    if len(rows) < 2:
        return None
    if days is None:
        return 0
    cutoff = rows[-1]["day"] - timedelta(days=days)
    index: int | None = None
    for i, row in enumerate(rows):
        if row["day"] <= cutoff:
            index = i
        else:
            break
    if index is None or index >= len(rows) - 1:
        return None  # 未充足、または起点が終端でリターンが立たない
    return index


def period_return(rows: Sequence[Mapping[str, Any]], *, days: int | None) -> float | None:
    """起点スナップショットから最終日までの複利リターン(外部フロー調整済み)。

    期間未充足は None。0 を返さないのは「変化なし」と「測れない」を混同しないため。
    日次リターンの定義は :func:`flow_adjusted_returns`(BOP/EOP 分離)と同一。
    """
    base = window_base_index(rows, days)
    if base is None:
        return None
    acc = 1.0
    for _, r in flow_adjusted_returns(rows[base:]):
        acc *= 1.0 + r
    return acc - 1.0


def period_detail(
    rows: Sequence[Mapping[str, Any]], *, label: str, days: int | None
) -> PeriodReturn:
    """:class:`PeriodReturn` を 1 件組む(起点日・ずれ・注記つき)。"""
    base = window_base_index(rows, days)
    end_day = rows[-1]["day"] if rows else None
    if base is None:
        note = (
            f"期間未充足(系列が {days} 暦日に満たない)"
            if days is not None
            else "リターンが立たない(スナップショットが 1 点以下)"
        )
        return PeriodReturn(label, days, None, None, end_day, None, note)
    base_day = rows[base]["day"]
    lag = None if days is None else (rows[-1]["day"] - timedelta(days=days) - base_day).days
    note = None
    if lag is not None and lag > WINDOW_LAG_TOLERANCE_DAYS:
        note = f"起点が窓外: {base_day}(cutoff より {lag} 日古い)"
    return PeriodReturn(
        label, days, period_return(rows, days=days), base_day, end_day, lag, note
    )


def period_returns(
    rows: Sequence[Mapping[str, Any]],
    periods: Sequence[tuple[str, int | None]] = DEFAULT_PERIODS,
) -> list[PeriodReturn]:
    """期間別リターン(既定は 1W/1M/設定来)。"""
    return [period_detail(rows, label=label, days=days) for label, days in periods]


__all__ = [
    "DEFAULT_PERIODS",
    "MISSING",
    "WINDOW_LAG_TOLERANCE_DAYS",
    "Bullet",
    "PeriodReturn",
    "flow_adjusted_returns",
    "fmt_delta_md",
    "fmt_hours",
    "fmt_jpy",
    "fmt_pct",
    "fmt_sig",
    "fmt_signed_pct",
    "make_bullet",
    "nav_frame",
    "page_question",
    "period_detail",
    "period_return",
    "period_returns",
    "render_bullet",
    "render_bullets",
    "render_count_ratio",
    "render_ratio",
    "render_state",
    "render_underwater",
    "underwater_frame",
    "window_base_index",
]
