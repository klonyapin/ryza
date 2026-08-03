"""dashboard/app — Ryza 運用ダッシュボード(Streamlit・Issue #10 → 組織サイト化)。

**Cloud Run + IAP で公開(2026-08-03 代表指示)+ローカル実行。役員室タブを除き
読み取り専用。** アクセス制御は IAP の許可リスト(roles/iap.httpsResourceAccessor)に
全面委譲し、アプリ内に認証コードは置かない(2026-08-02 の無認証 Cloud Run 公開版とは
異なり、IAP が Google アカウント認証を強制する。デプロイ: ops/deploy-dashboard.sh)。
Kill Switch 等の操作系 UI は置かない(Discord Bot の管轄)。例外は追記だけを行う2ページ:
「役員室」(Issue #9・05-governance §5)が議事録・決議マーク・stances を、「開発室」
(0024・代表指示 2026-08-03)が設計リードへの連絡(``ops.dev_chat``)を書く。
いずれも発注・設定変更の経路は持たない。

起動: ``.venv/bin/streamlit run dashboard/app.py``(README 参照)。
接続先は用途で2本に分かれる(独立役員審査 2026-08-03 重大-2 の是正):
読取は env ``RYZA_DATABASE_URL``(読取専用ロール ``ryza_dashboard``)、役員室の書込は
env ``RYZA_BOARDROOM_DATABASE_URL``(最小権限ロール ``ryza_boardroom``)。ローカルでは
後者を省略でき、その場合は前者にフォールバックする(``queries.connect_boardroom``)。
役員室の LLM 呼び出しは Anthropic API キーが必要(env RYZA_ANTHROPIC_API_KEY /
ANTHROPIC_API_KEY、または Secret Manager — providers.load_api_key の既定に任せる)。

DB アクセスは ``queries.py``(接続と読取)と ``ryza.governance.boardroom``(役員室の
書込・テスト対象)に分離し、本ファイルは表示だけを担う。

**可視化の規約(T-018)**: 表示形は ``viz.py`` のヘルパ経由でのみ作る。禁止記法は
円グラフ・ゲージ・二軸・生 JSON・比較文脈のない単独数値カード・累計 vanity 数値で、
根拠は ``docs/research/dashboard-visualization-guidelines.md``。数値には必ず比較対象
(前日比・対リミット・対予算)を添え、赤緑は差異とリミット超過にだけ使う。概況は
Few の一画面原則に従い 6 ブロック固定の監視面とし、明細は各詳細ページへ降ろす
(Shneiderman: overview first, then details-on-demand)。

**ナビゲーション(2026-08-03 代表指示のデザイン改修)**: ページ切替は ``st.navigation``
+ ``st.Page`` で、監視/成績・リスク/組織・統治/開発の 4 セクションにグルーピング
する(構成は ``NAV_SECTIONS``)。旧実装のサイドバー ``st.radio`` は 14 行が縦に圧縮され
タップターゲット 44px を大きく割っていた。デザイントークンは ``.streamlit/config.toml``
(DADS 実値)、config.toml で表現できない CSS 層は ``dads.py``。根拠は
``docs/research/dads-streamlit-application.md``。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from html import escape as _esc
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# streamlit run はスクリプトの親ディレクトリを sys.path に足すが、実行環境差への保険。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import dads  # noqa: E402
import github_api  # noqa: E402
import queries  # noqa: E402
import viz  # noqa: E402

from ryza import org  # noqa: E402
from ryza.governance import boardroom, devchat, personas  # noqa: E402
from ryza.provenance.runs import run as run_ctx  # noqa: E402
from ryza.research.llm import StructuredLLM  # noqa: E402
from ryza.research.providers import AnthropicProvider, LLMConfig  # noqa: E402

st.set_page_config(page_title="Ryza 運用ダッシュボード", layout="wide")

_STATE_LABELS = {
    "normal": "通常",
    "frozen": "凍結(/kill)",
    "winding_down": "段階的現金化中(/winddown)",
    "flattening": "緊急清算中(/flatten)",
    "flattened": "清算完了(現金)",
}


@st.cache_resource
def _conn():
    """read-only 接続(プロセス内で使い回す)。"""
    return queries.connect_readonly()


# ── 重いクエリの短期キャッシュ(独立役員審査 2026-08-03 中-10)────────────────────
# ウィジェット操作のたびに Streamlit はスクリプト全体を再実行する。無索引の
# ``ledger.journal_lines`` 全集計(NAV 系列のフロー結合)や ``meta.runs`` の全走査が
# その都度走るため、60 秒 TTL でキャッシュする。TTL を短くしてあるのは日次サイクルの
# 進行中に古い値を見せないため(監視面としての鮮度 > キャッシュ効率)。
# ``conn`` はハッシュ不能なので引数に取らず ``_conn()`` を内部で引く。
# NAV 系列と未反映フローは **1 クエリ・1 キャッシュ**で取る(独立審査 中-6): 別々に
# キャッシュすると TTL の切れ方次第で「系列は新しいが pending は古い」画面が出うる。
@st.cache_data(ttl=60)
def _nav_data() -> dict[str, list[dict[str, Any]]]:
    return queries.fetch_nav_data(_conn())


def _nav_series() -> list[dict[str, Any]]:
    return _nav_data()["series"]


def _pending_flows() -> list[dict[str, Any]]:
    """スナップショット未生成の外部フロー(NAV 系列に載らない — 重要-5)。"""
    return _nav_data()["pending"]


@st.cache_data(ttl=60)
def _cost_summary() -> dict[str, Any]:
    return queries.fetch_cost_summary(_conn())


@st.cache_data(ttl=60)
def _cost_daily(days: int = 30) -> list[dict[str, Any]]:
    return queries.fetch_cost_daily(_conn(), days=days)


@st.cache_data(ttl=60)
def _ingest_daily_counts(days: int = 30) -> list[dict[str, Any]]:
    return queries.fetch_ingest_daily_counts(_conn(), days=days)


def _df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _render_embed(embed: dict[str, Any]) -> None:
    """Discord embed(dict)の簡易プレビュー。"""
    with st.container(border=True):
        author = embed.get("author") or {}
        if author.get("name"):
            st.caption(author["name"])  # 発信者キャラクター(org.yaml 由来)
        if embed.get("title"):
            st.markdown(f"**{embed['title']}**")
        if embed.get("description"):
            st.markdown(embed["description"])
        for field in embed.get("fields", []):
            st.markdown(f"**{field.get('name', '')}**")
            st.markdown(str(field.get("value", "")))
        image_url = (embed.get("image") or {}).get("url")
        if image_url:
            st.image(image_url, width=320)
        footer = (embed.get("footer") or {}).get("text")
        if footer:
            st.caption(footer)


# ── 概況(一画面の監視面 — 6 ブロック固定)──────────────────────────────────────
# Few の一画面原則: スクロールなしで全体が見えることを要件とする。したがってこの
# ページに明細テーブルは置かない(旧「meta.runs 30 行」は「ジョブ」ページへ移した)。
# ブロックの並びは重要度順(左上が最強): ①取引の生死 ②NAV ③リスク / ④ジョブ
# ⑤未処理通知 ⑥コスト。
def _overview_trading_state(conn) -> None:
    """①取引状態。二値/少状態なので bullet ではなく状態インジケータ。"""
    st.markdown("**① 取引状態**")
    state = queries.fetch_trading_state(conn)
    if state is None:
        st.markdown("**状態**: 未初期化(ops.trading_state — Bot 未起動)")
        return
    label = _STATE_LABELS.get(state["state"], state["state"])
    viz.render_state("状態", label, alert=state["state"] != "normal")
    st.caption(
        f"更新 {state['updated_at']:%m-%d %H:%M} / {state['updated_by']}"
        + (f" — {state['reason']}" if state["reason"] else "")
    )


def _overview_nav(conn) -> None:
    """②NAV。単独数値にせず「前日比」「設定来」の 2 つの比較文脈を必ず併記する。"""
    st.markdown("**② NAV(ファンド帳簿)**")
    series = _nav_series()
    if not series:
        st.markdown(f"NAV: {viz.MISSING}")
        st.caption("ledger.nav_snapshots が空(締め処理が未実行)")
        return
    latest = series[-1]
    d1 = viz.period_return(series, days=1)
    itd = viz.period_return(series, days=None)
    st.markdown(f"**NAV**: {viz.fmt_jpy(latest['nav'])}({latest['status']})")
    st.markdown(
        f"前日比 {viz.fmt_delta_md(d1)} / 設定来 {viz.fmt_delta_md(itd)}"
        # 注記なので小さくするが DADS の下限(14px)は割らない(重要-2 と同じ理由)。
        # opacity:.6 の実効色は #767676 相当で 4.54:1 = テキスト下限ちょうど。
        f"  \n<span style='opacity:.6;font-size:{dads.MIN_FONT_REM}rem'>"
        "外部フロー調整済み(TWR)</span>",
        unsafe_allow_html=True,
    )
    st.caption(f"評価日 {latest['day']} / {len(series)} 営業日分")


def _dd_bullet(
    metrics: dict[str, Any], limits: dict[str, Any], *, label: str = "DD"
) -> viz.Bullet:
    """DD 使用率 = 現在 DD / dd_hard。ソフトリミット到達で警戒色に切り替わる。"""
    dd = metrics.get("drawdown")
    soft = limits.get("dd_soft_limit")
    return viz.make_bullet(
        label,
        dd,
        limits.get("dd_hard_limit"),
        soft_limit=soft,
        note="ソフト到達" if _reached(dd, soft) else None,
    )


def _reached(value: Any, threshold: Any) -> bool:
    try:
        return value is not None and threshold is not None and float(value) >= float(threshold)
    except (TypeError, ValueError):
        return False


def _overview_latches(conn) -> None:
    """ラッチ 4 種の要約(独立役員審査 2026-08-03 重要-4)。

    DD 使用率だけでは「DD が回復しても dd_hard ラッチが残って発注停止中」という状態が
    画面に出ない(ラッチは自動解除されないため使用率は下がるがブロックは続く)。実際に
    発注を止めているものを概況で名指しする。
    """
    states = queries.fetch_limits_state(conn)
    if not states:
        viz.render_state(
            "リスクフラグ", "risk.limits_state に行なし(ゲート G-10 は fail-closed)", alert=True
        )
        return
    active = [key for key in _LATCH_LABELS if any(s[key] for s in states)]
    blocking = [key for key in active if key != "dd_soft"]
    if not active:
        viz.render_state("リスクフラグ", "4 種すべて未作動", alert=False)
    elif blocking:
        viz.render_state("リスクフラグ", "発注ブロック中: " + ", ".join(blocking), alert=True)
    else:
        viz.render_state(
            "リスクフラグ", "作動中: " + ", ".join(active) + "(新規建て枠半減)", alert=True
        )


def _overview_drawdown(conn) -> None:
    """③DD 使用率+ラッチ要約。測定値はリスクエンジンの出力をそのまま使い再計算しない。"""
    st.markdown("**③ DD 使用率(対 dd_hard)**")
    event = queries.fetch_latest_risk_metrics(conn)
    limits = queries.load_ips_limits()
    if event is None:
        viz.render_bullet(viz.make_bullet("DD", None, limits.get("dd_hard_limit")))
        st.caption("リスクエンジン未実行(risk.limits_state_events が空)")
    else:
        viz.render_bullet(_dd_bullet(event["metrics"] or {}, limits))
        st.caption(
            f"測定 {event['as_of']:%m-%d %H:%M} / {event['actor']} / run {event['run_id']}"
        )
    _overview_latches(conn)


def _overview_daily(conn) -> None:
    """④直近 daily の成否。段別所要は meta.runs に記録が無いため出さない。"""
    st.markdown("**④ 直近の日次サイクル**")
    run = queries.fetch_latest_daily_run(conn)
    if run is None:
        st.markdown(f"直近実行: {viz.MISSING}")
        st.caption("jobs.daily の実行記録なし")
    else:
        alert = run["status"] != "success"
        viz.render_state("実行結果", run["status"], alert=alert)
        st.caption(
            f"開始 {run['started_at']:%m-%d %H:%M} / 所要 "
            f"{viz.fmt_sig((run['duration_seconds'] or 0) / 60, 2)} 分 / run {run['run_id']}"
        )
    summary = queries.fetch_latest_daily_summary(conn)
    if summary is not None:
        ok, ng = _stage_counts(summary["embed_json"])
        line = f"ステージ 成功 {ok} / 失敗 {ng}"
        st.markdown(f":red[{line}]" if ng else line)
    st.caption("段別所要は未記録(jobs.daily は段の成否のみ残す)— 詳細は「ジョブ」へ")


def _stage_counts(embed: dict[str, Any]) -> tuple[int, int]:
    """日次サイクル embed の field から段の成否を数える(値の先頭が ✅/⚠️)。"""
    ok = ng = 0
    for field in (embed or {}).get("fields", []):
        value = str(field.get("value", ""))
        if value.startswith("✅"):
            ok += 1
        elif value.startswith("⚠️"):
            ng += 1
    return ok, ng


def _overview_pending(conn) -> None:
    """⑤未処理の承認・通知(press.outbox の未配送)。"""
    st.markdown("**⑤ 未処理の承認・通知**")
    pending = queries.fetch_outbox_pending(conn)
    if not pending:
        st.markdown("未配送: 0 件")
        st.caption("press.outbox は全て配送済み")
        return
    total = sum(int(r["pending"]) for r in pending)
    oldest = max(float(r["oldest_age_hours"] or 0) for r in pending)
    st.markdown(f"**未配送 {total} 件** / 最古 {viz.fmt_hours(oldest)} 前")
    st.markdown(
        " ・".join(f"{r['channel']} {r['pending']}" for r in pending)
    )


def _overview_cost(conn) -> None:
    """⑥当月(暦月)LLM コストの予算消化率。累計額の単独表示はしない。"""
    st.markdown("**⑥ LLM コスト予算消化(当月)**")
    summary = _cost_summary()
    budget = queries.load_llm_budget().get("monthly_jpy")
    viz.render_bullet(
        viz.make_bullet("消化", summary["total_cost"], budget, fmt=viz.fmt_jpy)
    )
    # コスト記録のある実行の割合を添える: 分子が全実行を覆っていなければ消化率は
    # 過小評価であり、その事実を隠さない(独立役員審査 中-9)。
    st.markdown(
        f"コスト記録のある実行 {int(summary['cost_runs'] or 0)} / "
        f"{int(summary['all_runs'] or 0)}"
    )
    if budget is None:
        st.caption("config/llm.yaml に budget.monthly_jpy が無い(比率は出せない)")
    else:
        st.caption(
            f"月次予算 {viz.fmt_jpy(budget)} / 起点 {summary['since']:%Y-%m-%d}"
            " — 内訳は「コスト」へ"
        )


def page_overview(conn) -> None:
    st.header("概況")
    viz.page_question(
        "いま止めるべき事象が起きていないか — 取引状態・NAV・リスク・ジョブ・"
        "未処理通知・コストを一画面で確認する(明細は各詳細ページへ)"
    )
    top = st.columns(3)
    with top[0]:
        _overview_trading_state(conn)
    with top[1]:
        _overview_nav(conn)
    with top[2]:
        _overview_drawdown(conn)
    st.divider()
    bottom = st.columns(3)
    with bottom[0]:
        _overview_daily(conn)
    with bottom[1]:
        _overview_pending(conn)
    with bottom[2]:
        _overview_cost(conn)


# ── 成績 ──────────────────────────────────────────────────────────────────────
def _render_flow_notice(
    series: list[dict[str, Any]], pending: list[dict[str, Any]] | None = None
) -> None:
    """外部フロー発生日の注記(独立役員審査 2026-08-03 重要-6・重要-5)。

    underwater 図は NAV そのものを見る図なので、出資で NAV が跳ねた日・払戻で NAV が
    落ちた日は「回復/下落」に見える。払戻を −30% の損失と読み違えないよう、フローの
    あった日を図の直下で名指しする(期間リターンの方は TWR で調整済み)。

    フローの帰属日は仕訳日ではなく「その日以降の最初のスナップショット日」である
    (``ryza.risk.navflow``)。スナップショットがまだ無いフロー(``pending``)は系列の
    どの点にも載らないため、別行で明示する — 次の締めで NAV が跳ねる原因になる。

    先頭点は除く: 系列の起点より前の出資(設定時の払込など)はすべて先頭点に寄るが、
    前の点が無い以上そこに段差は生じない(起点 NAV が既にそれを含む)。段差の説明という
    この注記の用途に対しては誤誘導になるため出さない。金額は明細表の net_flow に残る。
    """
    flows = [r for r in series[1:] if float(r.get("net_flow") or 0) != 0]
    if flows:
        items = " / ".join(
            f"{r['day']} {'出資' if float(r['net_flow']) > 0 else '払戻'}"
            f" {viz.fmt_jpy(abs(float(r['net_flow'])))}"
            for r in flows[-8:]
        )
        st.caption(
            f":orange[外部フロー発生日({len(flows)} 日・図は未調整)]: {items}"
            " — 上の図の段差はこの出資・払戻によるもので、損益ではない"
            "(仕訳日が休日の場合は直後のスナップショット日に寄せてある)。"
        )
    if pending:
        items = " / ".join(
            f"{r['day']} {'出資' if float(r['amount']) > 0 else '払戻'}"
            f" {viz.fmt_jpy(abs(float(r['amount'])))}"
            for r in pending[-8:]
        )
        st.caption(
            f":red[スナップショット未生成の外部フロー({len(pending)} 件)]: {items}"
            " — まだ NAV 系列にもリターン測定にも入っていない(次の会計締めで反映)。"
        )


def page_performance(conn) -> None:
    st.header("成績")
    viz.page_question(
        "デモ運用の資産はどう推移し、いまどれだけ水没しているか(NAV と DD)"
    )
    series = _nav_series()
    if not series:
        st.info("ledger.nav_snapshots が空(締め処理が未実行)。NAV 系列が無いため成績は出せない。")
        return

    st.subheader("NAV 推移")
    st.line_chart(viz.nav_frame(series))
    st.subheader("アンダーウォーター(設定来ピーク比の下落率)")
    viz.render_underwater(series)
    st.caption(
        "上下は同じ日付 index で横軸が揃う。DD はピーク定義 = 設定来・連続測定"
        "(IPS §3.1)で、外部フロー調整は入れない NAV そのものの水没度合い。"
        "リミット判定に使う測定値はリスクエンジンの出力(「リスク」ページ)。"
    )
    _render_flow_notice(series, _pending_flows())

    st.subheader("期間別リターン(外部フロー調整済み TWR)")
    rows = [
        {
            "期間": r.label,
            "リターン": r.value_text,
            "起点日": r.base_text,
            "対照(等配分 buy-and-hold)": "未実装",
            "注記": r.note or "",
        }
        for r in viz.period_returns(series)
    ]
    rows.append(
        {
            "期間": "—",
            "リターン": "—",
            "起点日": "—",
            "対照(等配分 buy-and-hold)": "対照系列は未実装(T-019 候補)",
            "注記": "",
        }
    )
    st.dataframe(_df(rows), use_container_width=True, hide_index=True)
    st.caption(
        "窓は暦日(1M = 30 暦日)。**起点は cutoff 以前の直近スナップショット**で、"
        "その日付を併記する — 系列に穴があると「1W」が実際には 3 週間分になり得るため"
        "(起点が窓から大きく外れたら注記に出る)。cutoff 以前のスナップショットが"
        "1 本も無い期間は「期間未充足」として値を出さない。"
        "E4(等配分 buy-and-hold 対照)は評価の必須条件だが対照系列がまだ無く、"
        "無い比較を推定値で埋めず「未実装」と明示する。"
    )

    st.subheader("NAV スナップショット(明細)")
    st.dataframe(
        _df(
            [
                {k: r[k] for k in ("day", "nav", "status", "net_flow", "flow_bop", "flow_eop")}
                for r in series[::-1]
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "net_flow はその測定日に帰属した外部フローで、内訳は flow_bop(前の測定日より後・"
        "当日より前 = 締めが走らなかった日の分)と flow_eop(当日仕訳)。リターンは "
        "`(nav − flow_eop) / (前日 nav + flow_bop) − 1` で測る(期中に入った資金は"
        "その区間の運用元本のため分母に入れる)。**最古の行の net_flow だけは「設定来の"
        "累計」**である — 系列の起点より前の出資はすべて先頭点に寄る(起点 NAV が既に"
        "それを含むため、この値はリターン計算には使われない)。"
    )


# ── リスク ────────────────────────────────────────────────────────────────────
def _insufficiency_note(metrics: dict[str, Any], limits: dict[str, Any]) -> str | None:
    """観測不足のときの注記。十分なら None(独立役員審査 2026-08-03 重大-3)。

    リスクエンジンは帳簿リターンが ``realized_vol_ewma_days`` 営業日そろうまで
    vol/ES フラグを立てない(``engine.evaluate`` の ``sufficient``)。測定値だけを
    そのまま bullet にすると、フラグが「未作動」なのに使用率が赤 breach を出す
    — 同一画面で矛盾する。判定が無効な間は値を出さず、その理由を出す。

    ``sufficient`` キーが無い古い metrics も「十分と確認できていない」側に倒す
    (fail-closed)。恒久対応は state_metrics への deferred/excluded 追加
    (ops/reminders.yaml: risk-state-metrics-sufficiency)。
    """
    if metrics.get("sufficient"):
        return None
    n = metrics.get("n_returns")
    need = limits.get("realized_vol_ewma_days")
    return (
        f"観測不足で判定無効(n={n if n is not None else '?'}/"
        f"{need if need is not None else '?'})"
    )


def _risk_bullets(metrics: dict[str, Any], limits: dict[str, Any]) -> list[viz.Bullet]:
    """スキーマに実在する測定値だけを bullet にする(無いものは作らない)。

    DD は 1 日目から有効(engine の drawdown はデータ 1 点から測れる)なので観測数に
    関わらず出す。vol/ES はエンジンが判定を保留している間 unknown に落とす。
    """
    note = _insufficiency_note(metrics, limits)
    return [
        viz.make_bullet(
            "DD(対 dd_soft)", metrics.get("drawdown"), limits.get("dd_soft_limit")
        ),
        _dd_bullet(metrics, limits, label="DD(対 dd_hard)"),
        viz.make_bullet(
            "実現ボラ(EWMA 年率)",
            None if note else metrics.get("ewma_vol_annual"),
            limits.get("realized_vol_limit"),
            note=note,
        ),
        viz.make_bullet(
            "日次 ES95(対 NAV)",
            None if note else metrics.get("es95_adopted"),
            limits.get("daily_es95_nav_max"),
            note=note,
        ),
    ]


_LATCH_LABELS = {
    "dd_soft": "DD ソフト(新規建て枠半減)",
    "dd_hard": "DD ハード(全新規発注停止・復帰は委員会のみ)",
    "vol_exceeded": "実現ボラ超過(新規建てブロック)",
    "es_exceeded": "日次 ES95 超過(新規建てブロック)",
}


def page_risk(conn) -> None:
    st.header("リスク")
    viz.page_question(
        "どのリミットにどれだけ近いか、いま発注をブロックしているフラグは何か"
    )
    limits = queries.load_ips_limits()
    states = queries.fetch_limits_state(conn)
    if not states:
        st.warning(
            "risk.limits_state に行が無い。発注ゲート G-10 は行が無ければ fail-closed で"
            "ブロックする(リスクエンジン未実行)。"
        )
    for state in states:
        st.subheader(f"帳簿 {state['book_id']}")
        event = queries.fetch_latest_risk_metrics(conn, book_id=state["book_id"])
        metrics = (event or {}).get("metrics") or {}
        if event is None:
            st.caption("測定値なし(risk.limits_state_events が空)— 使用率は出せない")
        else:
            st.caption(
                f"測定 {event['as_of']:%Y-%m-%d %H:%M} / {event['actor']} / "
                f"run {event['run_id']} / 事象 {event['event']}"
            )
        st.markdown("**リミット使用率(高い順)**")
        viz.render_bullets(_risk_bullets(metrics, limits))

        st.markdown("**ラッチ状態**")
        for key, label in _LATCH_LABELS.items():
            viz.render_state(label, "作動中" if state[key] else "未作動", alert=bool(state[key]))
        st.caption(
            f"状態時刻 {state['as_of']} / run {state['run_id']}。"
            "dd_hard は OR ラッチで自動解除されない(解除は委員会操作のみ — IPS §3.2)。"
        )
        if metrics.get("notes"):
            st.caption("測定上の注記: " + " / ".join(map(str, metrics["notes"])))


# ── ジョブ(概況からの details-on-demand)────────────────────────────────────────
def page_jobs(conn) -> None:
    st.header("ジョブ")
    viz.page_question("どのジョブがいつ動き、どれが失敗し、いま何が走っているか")

    st.subheader("実行中")
    running = queries.fetch_running_runs(conn)
    if not running:
        st.caption("実行中のジョブはない(meta.runs)")
    else:
        st.dataframe(_df(running), use_container_width=True, hide_index=True)

    st.subheader("直近の日次サイクル(段別の成否)")
    summary = queries.fetch_latest_daily_summary(conn)
    if summary is None:
        st.caption("日次サイクルの実行サマリはまだない")
    else:
        sent = summary["sent_at"] or "未配送"
        st.caption(f"outbox #{summary['id']} / 投入 {summary['created_at']} / 配送 {sent}")
        _render_embed(summary["embed_json"])
        st.caption(
            "段別の所要時間は未実装 — jobs.daily は段ごとの成否と要約だけを残し、"
            "開始・終了時刻を記録していない(meta.runs はサイクル全体で 1 行)。"
        )

    st.subheader("直近のジョブ実行(meta.runs・30 件)")
    runs = queries.fetch_recent_runs(conn, limit=30)
    if not runs:
        st.caption("実行記録なし")
    else:
        st.dataframe(_df(runs), use_container_width=True, hide_index=True)


# ── 取込 ──────────────────────────────────────────────────────────────────────
def page_ingest(conn) -> None:
    st.header("取込")
    viz.page_question("どのデータソースが鮮度 SLA を割っており、日々どれだけ入っているか")

    st.subheader("ソース別鮮度(SLA)")
    freshness = _df(queries.fetch_freshness(conn))
    breaches = freshness[freshness["status"] != "ok"]
    ok_count = int((freshness["status"] == "ok").sum())
    st.caption(f"SLA 充足 {ok_count}/{len(freshness)} ソース(違反 {len(breaches)} 件)")
    st.dataframe(
        freshness.style.map(
            # ok は着色しない: 緑は差異・リミット超過に予約してある(A12・中-7)。
            # 異常だけが色で立ち上がる方が、SLA 違反の発見も速い。
            # 色は DADS セマンティック。pandas の Styler は生の CSS を吐くため
            # Streamlit の色指定を経由できず、ここだけ hex を dads から引く
            # (CSS の色名 orange/red は DADS 外かつ orange は 4.5:1 を割る)。
            # 判定は "stale"/"no_data" という**語**で既に読めるので、色は冗長な符号化。
            lambda v: {
                "stale": f"color: {dads.WARNING}",
                "no_data": f"color: {dads.ERROR}",
            }.get(v, ""),
            subset=["status"],
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("日別取込件数(直近30日・as_of 基準)")
    counts = _ingest_daily_counts(30)
    if not counts:
        st.info("直近30日の取込なし")
    else:
        pivot = _df(counts).pivot_table(
            index="day", columns="table", values="count", fill_value=0
        )
        st.bar_chart(pivot)
        st.dataframe(pivot, use_container_width=True)


# ── 報道 ──────────────────────────────────────────────────────────────────────
def page_press(conn) -> None:
    st.header("報道(press.outbox)")
    viz.page_question("各チャンネルに直近どんな投稿が流れ、配送は済んでいるか")
    channel = st.selectbox(
        "チャンネル", ["press", "ops", "approval", "dev", "(全部)"], index=0
    )
    rows = queries.fetch_recent_outbox(
        conn, channel=None if channel == "(全部)" else channel, limit=10
    )
    if not rows:
        st.info("投稿なし")
        return
    for row in rows:
        sent = f"配送済み {row['sent_at']}" if row["sent_at"] else "未配送"
        urgent = " / 緊急" if row["urgent"] else ""
        st.caption(f"#{row['id']} [{row['channel']}] 投入 {row['created_at']} / {sent}{urgent}")
        _render_embed(row["embed_json"])


# ── コスト ────────────────────────────────────────────────────────────────────
def page_cost(conn) -> None:
    st.header("コスト(meta.runs.cost)")
    viz.page_question("LLM コストは月次予算のどれだけを食っており、1 実行あたりいくらか")
    st.caption(
        "cost jsonb に部門次元は無いため、部門は job_name の先頭セグメントで代理"
        "(ingest.jquants.daily → ingest)。金額はモデル階層別単価による概算。"
    )

    # 予算消化率(比率)を先に出す。単独の「30日合計」カードは比較文脈が無く、
    # 累計トークン数は行動を変えない vanity metric のため置かない(A9・A10)。
    summary = _cost_summary()
    budget = queries.load_llm_budget().get("monthly_jpy")
    st.subheader(f"月次予算の消化(当月・{summary['since']:%Y-%m} 起点)")
    viz.render_bullet(viz.make_bullet("消化", summary["total_cost"], budget, fmt=viz.fmt_jpy))
    cost_runs = int(summary["cost_runs"] or 0)
    per_run = float(summary["total_cost"]) / cost_runs if cost_runs else None
    st.markdown(
        f"1 実行あたり **{viz.fmt_jpy(per_run)}**"
        f"(コスト記録のある実行 {cost_runs} / 全実行 {int(summary['all_runs'] or 0)})"
    )
    st.caption(
        "窓は**暦月**で、比較対象の月次予算と期間を揃えてある(30日ローリングだと"
        "分子と分母の期間が食い違い消化率が意味を失う)。予算の既定値は config/llm.yaml の"
        "budget.monthly_jpy(根拠は同ファイルのコメント)。承認済み予算行"
        "(ledger.budgets)が入ればそちらが正になる。下の内訳は直近 30 日の集計。"
    )

    rows = _cost_daily(30)
    if not rows:
        st.info("直近30日にコスト記録のある実行なし(内訳は出せない)")
        return
    df = _df(rows)
    df["cost_estimate"] = df["cost_estimate"].astype(float)

    st.subheader("日別 × モデル階層")
    st.bar_chart(
        df.pivot_table(index="day", columns="tier", values="cost_estimate", aggfunc="sum")
    )
    st.subheader("部門 × モデル階層")
    st.dataframe(
        df.groupby(["dept", "tier"], as_index=False)[["calls", "tokens", "cost_estimate"]]
        .sum()
        .sort_values("cost_estimate", ascending=False),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("明細(日別)")
    st.dataframe(df, use_container_width=True, hide_index=True)


# ── 市場観 ────────────────────────────────────────────────────────────────────
# regime / key_risks / changes の実データ構造(リサーチ層の生成物)を人間可読に描画する。
# 未知のドメイン・スタンス・変更種別は生の値をそのまま出す(隠さない)。
_REGIME_DOMAIN_LABELS = {
    "jp_equity": "日本株",
    "us_equity": "米国株",
    "rates": "金利",
    "fx": "為替",
}
_REGIME_STANCE_LABELS = {
    "risk_on": "リスクオン",
    "risk_off": "リスクオフ",
    "neutral": "中立",
    "tightening": "引き締め",
    "easing": "緩和",
}
_CHANGE_KIND_LABELS = {
    "key_risk_confidence": "リスク確信度の更新",
    "key_risk_added": "リスクの追加",
    "key_risk_removed": "リスクの解除",
    "regime_shift": "レジーム変更",
}


def _render_key_risk(risk: dict) -> None:
    statement = risk.get("statement") or risk.get("risk_id", "(記述なし)")
    confidence = risk.get("confidence")
    with st.container(border=True):
        st.markdown(f"**{risk.get('risk_id', '')}**")
        st.write(statement)
        if confidence is not None:
            viz.render_ratio("確信度", confidence, suffix="(自己申告・発注に使わない)")
        if risk.get("observable"):
            st.caption(f"確認ポイント: {risk['observable']}")
        if risk.get("refs"):
            refs = ", ".join(map(str, risk["refs"]))
            st.caption(f"根拠文書: {len(risk['refs'])} 件(doc_id: {refs})")


def _render_change(change: dict) -> None:
    kind = change.get("kind", "")
    label = _CHANGE_KIND_LABELS.get(kind, kind)
    detail = change.get("detail", {})
    if kind == "key_risk_confidence":
        st.markdown(
            f"- **{label}**: `{detail.get('risk_id', '?')}` の確信度 "
            f"{detail.get('from', '?')} → {detail.get('to', '?')}"
        )
    elif detail.get("risk_id"):
        st.markdown(f"- **{label}**: `{detail['risk_id']}`")
    else:
        st.markdown(f"- **{label}**: {detail}")


def page_market_view(conn) -> None:
    st.header("市場観(docs.market_view)")
    viz.page_question("リサーチ層はいま市場をどう見ており、前版から何が変わったか")
    st.caption(
        "リサーチ層が文書・指標から更新している市場の見立て。"
        "確信度は自己申告値で、発注サイズには使われない(不変原則1)。"
    )
    view = queries.fetch_current_market_view(conn)
    if view is None:
        st.info("市場観は未初期化")
    else:
        st.caption(f"view_id {view['view_id']} / 版時刻 {view['ts']} / run {view['run_id']}")
        st.subheader("レジーム(市場の基調判断)")
        regime = view["regime"] or {}
        cols = st.columns(max(len(regime), 1))
        for col, (domain, stance) in zip(cols, sorted(regime.items()), strict=False):
            col.metric(
                _REGIME_DOMAIN_LABELS.get(domain, domain),
                _REGIME_STANCE_LABELS.get(stance, stance),
            )
        st.subheader("注目リスク")
        risks = view["key_risks"] or []
        if not risks:
            st.info("登録されたリスクなし")
        for risk in risks:
            _render_key_risk(risk)
        changes = view["changes"] or {}
        applied = changes.get("applied") or []
        rejected = changes.get("rejected") or []
        if applied or rejected:
            st.subheader("前版からの差分")
            for change in applied:
                _render_change(change)
            if rejected:
                st.caption(f"棄却された変更案: {len(rejected)} 件")

    st.subheader("日次スナップショット(確定版)")
    snapshots = queries.fetch_market_view_snapshots(conn, limit=14)
    if not snapshots:
        st.info("スナップショットなし")
    else:
        st.dataframe(_df(snapshots), use_container_width=True, hide_index=True)


# ── 開発ステータス(Issue #10: site/ 統合)──────────────────────────────────────
def page_dev_status() -> None:
    st.header("開発・運用ステータス")
    viz.page_question("マイルストーン・Issue・コミットの現況(site/build.py の生成物)")
    data = queries.load_site_status()
    if data is None:
        st.warning("site/data.js が見つからない。`python3 site/build.py` で生成する。")
        return
    st.caption(f"生成: {data.get('generated_at', '不明')} — 更新は `python3 site/build.py`")
    st.subheader(data.get("phase", ""))

    st.subheader("ロードマップ(GitHub Milestones)")
    status_labels = {"done": "完了", "doing": "進行中", "user": "ユーザー待ち", "todo": "未着手"}
    ms = _df(data.get("milestones", []))
    if not ms.empty:
        ms["status"] = ms["status"].map(lambda s: status_labels.get(s, s))
        st.dataframe(ms, use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Issues")
        issues = _df(data.get("issues", []))
        if not issues.empty:
            st.dataframe(issues, use_container_width=True, hide_index=True)
    with col2:
        st.subheader("直近コミット")
        commits = _df(data.get("commits", []))
        if not commits.empty:
            st.dataframe(commits, use_container_width=True, hide_index=True)


# ── 組織(組織サイト化 — 2026-08-03 代表指示)──────────────────────────────────
_TIER_LABELS = {"fable": "Fable(最上位)", "mid": "中位モデル", "light": "軽量/非LLM"}

#: 代表(人間)のカード色。台帳に載らない唯一のカードなので、ここで色を持つ。
#: 中性的なスレートで、キャラクター色の並びから浮かずに人間だと分かる程度に外す。
_REPRESENTATIVE_COLOR = "#64748B"

# 組織図・メンバーカードは Streamlit の部品では組めないため自前の HTML/CSS で描く。
# 2026-08-03 のデザイン改修で DADS トークンへ寄せた: 罫線は Solid Gray-420
# (#949494 = 白背景で 3:1 ちょうど。非テキスト要素の下限)、角丸は 6/8/12px、
# 余白は 8px グリッド。半透明の灰(rgba(128,128,128,.35) 等)は背景次第で 3:1 を
# 割るため実値のトークンに置き換えた。投資委員会の強調は dads.ACCENT
# (warning-yellow-2 = 4.54:1)で、旧値 #d9a441 は白背景で 2:1 前後しか出ていなかった。
#
# **font-size の下限**(独立役員審査 重要-2): 全 11 箇所を dads.MIN_FONT_REM
# (0.875rem = 14px)以上にした。改修前は 0.65〜0.85rem(10.4〜13.6px)で、
# config.toml 側が「DADS: 14px 未満は不許可」と宣言している一方この CSS だけが
# 例外になっていた —— 同じ改修で line-height と境界色は DADS へ寄せながら
# font-size を据え置いたための不整合である。密度は下がるが、読めない文字を
# 並べる方がダッシュボードとしては損。値をリテラルで書かず定数から埋めるのは、
# 下限を1箇所で動かせるようにするため(テストも同じ定数を見る)。
_ORG_CSS = f"""
<style>
.oc-apex {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:8px; }}
.oc-node {{ border:1px solid {dads.BORDER}; border-radius:8px; padding:8px 16px;
  font-size:{dads.MIN_FONT_REM}rem; }}
.oc-node b {{ display:block; }}
.oc-node small {{ opacity:.7; }}
.oc-ic {{ border-color:{dads.ACCENT}; border-width:2px; }}
.oc-aud {{ border-style:dashed; }}
.oc-vline {{ width:2px; height:16px; background:{dads.BORDER}; margin:0 0 8px 40px; }}
.oc-offices {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:8px; }}
.oc-office {{ border:1px solid {dads.BORDER}; border-radius:12px; padding:8px 12px; }}
.oc-office h4 {{ margin:0 0 8px; font-size:{dads.MIN_FONT_REM}rem; opacity:.7;
  letter-spacing:.06em; }}
.oc-office ul {{ list-style:none; margin:0; padding:0; }}
.oc-office li {{ font-size:{dads.MIN_FONT_REM}rem; padding:4px 8px; margin:4px 0;
  border-radius:6px; border:1px solid {dads.BORDER}; line-height:1.3; }}
.oc-office li small {{ display:block; opacity:.65; font-size:{dads.MIN_FONT_REM}rem; }}
.oc-flow {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center;
  font-size:{dads.MIN_FONT_REM}rem; margin-top:8px; }}
.oc-flow span.s {{ border:1px solid {dads.BORDER}; border-radius:6px; padding:2px 8px; }}
.oc-flow span.g {{ border-color:{dads.ACCENT}; color:{dads.ACCENT}; }}
.oc-flow span.a {{ opacity:.6; }}
.oc-members {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
  gap:12px; margin-top:8px; }}
.oc-card {{ border:1px solid {dads.BORDER}; border-radius:12px; padding:16px;
  display:flex; gap:12px; border-top:3px solid var(--mc,{dads.BORDER}); }}
.oc-avatar {{ width:64px; height:64px; border-radius:50%; flex:none; object-fit:cover; }}
/* 文字色は背景の輝度で黒/白を選ぶ(_avatar_html が style で個別に与える)。
   白固定だと淡いキャラクター色で 4.5:1 を割る(#a78bfa で 2.72:1)。 */
.oc-fallback {{ display:flex; align-items:center; justify-content:center;
  font-size:1.6rem; font-weight:600; }}
.oc-card .nm {{ font-size:1.05rem; font-weight:600; line-height:1.4; }}
.oc-card .src {{ font-size:{dads.MIN_FONT_REM}rem; opacity:.65; }}
.oc-card .ttl {{ font-size:{dads.MIN_FONT_REM}rem; margin:2px 0 4px; line-height:1.5; }}
.oc-card .tg {{ font-size:{dads.MIN_FONT_REM}rem; opacity:.75; margin-top:4px;
  line-height:1.5; }}
.oc-chip {{ font-size:{dads.MIN_FONT_REM}rem; border:1px solid {dads.BORDER};
  border-radius:10px; padding:1px 8px; margin-right:4px; white-space:nowrap; }}
</style>
"""

_ORG_OFFICES = [
    ("フロントオフィス", [
        ("リサーチ部門", "市場観ステートの継続更新"),
        ("報道部", "朝刊 10:00 JST・速報"),
        ("戦略部門", "ストラテジー動物園"),
        ("PM 部", "リスク予算・サイジング"),
        ("トレーディングデスク", "最良執行(デモ/実)"),
    ]),
    ("ミドルオフィス", [
        ("リスク管理部", "VaR/ES・リミット・ストレス"),
        ("コンプライアンス部", "発注前ゲート・表現チェック"),
    ]),
    ("バックオフィス", [
        ("ファンド会計部", "複式簿記・NAV・照合"),
        ("経営管理部", "実費会計・予算・LLM コスト"),
        ("パフォーマンス測定部", "TWR・要因分解・貢献度"),
        ("データ基盤部", "リネージ・point-in-time 収集"),
    ]),
    ("研究本部", [
        ("戦略研究班", "論文級の戦略研究"),
        ("情報分析班", "常時の情報分析"),
        ("システム研究班", "システム改善研究"),
    ]),
    ("開発部門", [
        ("設計リード+実装エージェント", "全変更 PR 経由・独立審査・CI"),
    ]),
]


def _org_chart_html() -> str:
    """00-system-design.md §3 の組織図を静的 HTML/CSS で再現(mermaid 非依存)。"""
    offices = "".join(
        "<div class='oc-office'><h4>" + _esc(name) + "</h4><ul>"
        + "".join(
            f"<li>{_esc(dept)}<small>{_esc(duty)}</small></li>" for dept, duty in depts
        )
        + "</ul></div>"
        for name, depts in _ORG_OFFICES
    )
    flow = (
        "<div class='oc-flow'>"
        "<span class='s'>リサーチ</span><span class='a'>→</span>"
        "<span class='s'>戦略</span><span class='a'>→</span>"
        "<span class='s'>PM 部</span><span class='a'>→</span>"
        "<span class='s g'>コンプラ発注前ゲート</span><span class='a'>→</span>"
        "<span class='s'>トレーディング</span><span class='a'>→</span>"
        "<span class='s'>ファンド会計</span><span class='a'>→</span>"
        "<span class='s'>パフォーマンス測定</span><span class='a'>→</span>"
        "<span class='s'>研究本部</span><span class='a'>→ 改善 PR・論文 →</span>"
        "<span class='s'>投資委員会</span></div>"
    )
    return (
        _ORG_CSS
        + "<div class='oc-apex'>"
        + "<div class='oc-node oc-ic'><b>投資委員会 = 代表(ユーザー)</b>"
        + "<small>IPS・予算・戦略昇格の承認/緊急停止</small></div>"
        + "<div class='oc-node oc-aud'><b>独立監査部門</b>"
        + "<small>read-only・委員会直属・別実行環境</small></div>"
        + "</div><div class='oc-vline'></div>"
        + f"<div class='oc-offices'>{offices}</div>"
        + flow
    )


def _avatar_html(name: str, color: str, icon_url: str | None) -> str:
    """アイコン画像、無ければキャラクター色の円に頭文字。

    **文字色は背景の輝度で黒/白を選ぶ**(独立役員審査 重要-2)。白固定だった旧実装は
    淡いキャラクター色でコントラストを割っていた(``#a78bfa`` で 2.72:1、``#059669``
    で 3.77:1)。台帳(config/org.yaml)は色を自由に決めてよい設計なので、可読性は
    描画側が機械的に担保する。

    ``color`` は ``dads.safe_color`` を通してから ``style`` に埋める(低-9)。
    ``html.escape`` は引用符を潰すだけで、``red;position:fixed`` のような**同じ
    style 属性内への CSS 宣言追記**は防げない。
    """
    if icon_url:
        return f"<img class='oc-avatar' src='{_esc(icon_url)}' alt='{_esc(name)}'>"
    background = dads.safe_color(color)
    return (
        f"<div class='oc-avatar oc-fallback' "
        f"style='background:{background};color:{dads.text_on(background)}'>"
        f"{_esc(name[0])}</div>"
    )


def _member_card_html(m: dict[str, Any]) -> str:
    tier = m.get("model_tier", "")
    chips = f"<span class='oc-chip'>{_esc(m.get('dept', ''))}</span>" + (
        f"<span class='oc-chip'>{_esc(_TIER_LABELS.get(tier, tier))}</span>" if tier else ""
    )
    src = (
        f"<div class='src'>出典: {_esc(m['source'])}</div>" if m.get("source") else ""
    )
    # 台帳・DB 由来の色は #RRGGBB 以外を受け付けない(低-9)。旧既定 '#888' は3桁形で
    # 検証を通らないため、既定も 6 桁のトークン(dads.FALLBACK_COLOR)へ揃えた。
    color = dads.safe_color(m.get("color"))
    return (
        f"<div class='oc-card' style='--mc:{color}'>"
        + _avatar_html(m.get("name", "?"), color, m.get("icon_url"))
        + f"<div><div class='nm'>{_esc(m.get('name', ''))}</div>{src}"
        + f"<div class='ttl'>{_esc(m.get('title', ''))}</div>"
        + f"<div>{chips}</div>"
        + f"<div class='tg'>{_esc(m.get('tagline', ''))}</div></div></div>"
    )


def _icon_editor(members: list[dict[str, Any]], overrides: dict[str, str]) -> None:
    """アイコン編集 UI(代表指示 2026-08-03)。上書きは ``ops.org_icon_overrides``(0020)。

    **認可**: 追加の認証は置かない。このダッシュボードは IAP の許可リスト
    (roles/iap.httpsResourceAccessor)で**代表1名**に限定されており、到達できる時点で
    代表であることが保証される(役員室の追記 UI と同じ根拠 — app.py 冒頭・
    ops/deploy-dashboard.sh)。したがって ``updated_by`` は固定で 'representative'。
    書込は読取接続ではなく役員室と同じ最小権限ロール ``ryza_boardroom``
    (``queries.connect_boardroom``)で行う。
    """
    st.subheader("アイコンの変更(代表のみ・DB 上書き)")
    st.caption(
        "台帳(config/org.yaml)の値を DB 側で上書きする(0020)。保存すると Discord の"
        "投稿(webhook の avatar)にも次の配送から反映される。「初期値に戻す」で上書きを"
        "削除すると台帳の値へ戻る。"
    )
    for m in members:
        member_id = str(m.get("id", ""))
        label = f"{m.get('name', '')}({m.get('title', '')})"
        overridden = member_id in overrides
        with st.expander(f"{label}{' — 上書き中' if overridden else ''}"):
            current = overrides.get(member_id) or m.get("icon_url") or ""
            cols = st.columns([1, 3])
            with cols[0]:
                if current:
                    st.image(current, width=96)
                else:
                    st.caption("(アイコン未設定)")
            with cols[1]:
                with st.form(f"icon_form_{member_id}"):
                    url = st.text_input(
                        "画像 URL(https の直リンク)", value=current, key=f"icon_url_{member_id}"
                    )
                    save = st.form_submit_button("保存")
                    reset = st.form_submit_button("初期値に戻す", disabled=not overridden)
                if save or reset:
                    _apply_icon_change(member_id, url, save=bool(save))


def _apply_icon_change(member_id: str, url: str, *, save: bool) -> None:
    """保存/リセットを実行し、結果を表示して再描画する(失敗時は保存しない)。

    接続は役員室と同じ ``_boardroom_conn()``(``@st.cache_resource``)を**再利用**する
    (独立役員審査 0020 C-9)。保存のたびに新規接続を開くと、Streamlit の再実行ごとに
    close されない接続が積み上がる。
    """
    try:
        wconn = _boardroom_conn()
    except Exception as exc:  # noqa: BLE001 - DB 停止時も UI は説明を出して生かす
        st.error(f"DB に接続できない: {exc}")
        return
    try:
        if save:
            org.update_icon(wconn, member_id, url, "representative")
            st.success("アイコンを更新した")
        elif org.clear_icon_override(wconn, member_id, "representative"):
            st.success("上書きを削除し台帳の初期値へ戻した")
        else:
            st.info("上書きは無い(既に台帳の初期値)")
    except org.IconUrlError as exc:
        st.error(f"保存しなかった(URL 検証に失敗): {exc}")
        return
    except KeyError as exc:
        st.error(f"保存しなかった: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 権限不足等も画面に出す(黙って失敗しない)
        st.error(f"保存に失敗: {exc}")
        return
    st.rerun()  # プレビュー・カードを即時更新する


def page_org(conn=None) -> None:
    st.header("組織")
    viz.page_question("どの部門が何を担い、誰(どのモデル階層)が座っているか")
    org_yaml = queries.load_org()
    # アイコンの DB 上書き(0020)を台帳より優先。DB に繋がらない場合も台帳だけで表示を続ける。
    overrides: dict[str, str] = {}
    db_error: str | None = None
    if conn is not None:
        try:
            overrides = org.icon_overrides(conn)
        except Exception as exc:  # noqa: BLE001 - 表示は台帳へフォールバック
            db_error = str(exc)

    st.subheader("組織図(00-system-design §3・14部門+開発部門)")
    st.markdown(_org_chart_html(), unsafe_allow_html=True)
    st.caption(
        "リスク管理部は PM を日次監視。独立監査部門は全部門を read-only で監査し、"
        "投資委員会へ直接報告する。"
    )

    st.subheader("メンバー(config/org.yaml が正・アイコンは DB 上書きを優先)")
    rep = org_yaml.get("representative", {})
    # 代表は台帳の members に載らない(人間なので model_tier を持たない)ため、
    # カードをここで組む。色は他のメンバーと同じ経路(safe_color → text_on)を通し、
    # 文字色を白に固定しない — 検査対象から外れる例外を作らないため。
    rep_color = dads.safe_color(_REPRESENTATIVE_COLOR)
    rep_card = (
        f"<div class='oc-card' style='--mc:{rep_color}'>"
        f"<div class='oc-avatar oc-fallback' "
        f"style='background:{rep_color};color:{dads.text_on(rep_color)}'>代</div>"
        "<div><div class='nm'>代表</div>"
        f"<div class='ttl'>{_esc(rep.get('note', 'ユーザー'))}</div>"
        "<div><span class='oc-chip'>人間</span>"
        "<span class='oc-chip'>投資委員会</span></div></div></div>"
    )
    members = [dict(m) for m in org_yaml.get("members", [])]
    for m in members:
        override = overrides.get(str(m.get("id", "")))
        if override:
            m["icon_url"] = override
    cards = rep_card + "".join(_member_card_html(m) for m in members)
    st.markdown(_ORG_CSS + f"<div class='oc-members'>{cards}</div>", unsafe_allow_html=True)
    st.caption(
        "モデル階層は「まず非LLM → 軽量 → 中位 → Fable」の原則(CLAUDE.md)。"
        "アイコン未設定のメンバーはカラーの頭文字で代替表示。"
    )
    if db_error is not None:
        st.warning(f"アイコン上書き(DB)を読めなかったため台帳の値で表示している: {db_error}")
    if conn is None:
        st.info("DB に接続できないため、アイコンの変更 UI は表示していない(表示は台帳の値)。")
        return
    _icon_editor(members, overrides)


# ── 承認・通知(組織サイト化)──────────────────────────────────────────────────
def page_approvals(conn) -> None:
    st.header("承認・通知")
    viz.page_question("いま代表の手を止めている未処理はいくつあり、最古はどれだけ待たされているか")

    # サマリ行(未処理は何件・最古は何時間前)。件数だけの単独表示にせず「最古の待ち時間」
    # を必ず添える — 承認は 48h でみなし承認が発効するため、経過時間が行動の引き金になる。
    pending = queries.fetch_outbox_pending(conn)
    if not pending:
        st.markdown("**未配送の通知**: 0 件")
    else:
        total = sum(int(r["pending"]) for r in pending)
        oldest = max(float(r["oldest_age_hours"] or 0) for r in pending)
        breakdown = " ・".join(f"{r['channel']} {r['pending']}" for r in pending)
        line = f"**未配送の通知**: {total} 件(最古 {viz.fmt_hours(oldest)} 前)— {breakdown}"
        st.markdown(f":red[{line}]" if oldest >= 48 else line)
        st.caption("48h 超過はみなし承認の期限(定款第3条)に触れる可能性がある。")

    st.subheader("承認・決定の現状(governance.current_decisions)")
    decisions = queries.fetch_decisions(conn, limit=50)
    if not decisions:
        st.info("決定記録はまだない(Discord 承認 UI・みなし承認が書く)")
    else:
        df = _df(decisions)
        # 「否認済み」を先頭列に出す: 代表が否認した承認を承認済みのまま見せないため
        # (独立役員審査 0021 C-5)。区分はみなし/明示の別(deemed_ratio の前提)。
        df.insert(0, "状態", df["is_vetoed"].map(lambda v: "否認済み" if v else "有効"))
        df.insert(1, "区分", df["decision"].map(lambda d: "みなし" if d == "deemed" else "明示"))
        vetoed = int(df["is_vetoed"].sum())
        st.caption(
            f"直近 {len(df)} 件(みなし {int((df['区分'] == 'みなし').sum())} / "
            f"明示 {int((df['区分'] == '明示').sum())}・うち否認済み {vetoed})— "
            "みなし承認は通知と同時発効・代表はいつでも否認可(定款第3条 v0.4)。"
            "否認は取消(revert_commit)と派生効果の #運営 報告を伴う"
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.subheader("#承認 への直近通知(press.outbox)")
    rows = queries.fetch_recent_outbox(conn, channel="approval", limit=5)
    if not rows:
        st.info("承認チャンネルへの通知はまだない")
    for row in rows:
        sent = f"配送済み {row['sent_at']}" if row["sent_at"] else "未配送"
        st.caption(f"#{row['id']} 投入 {row['created_at']} / {sent}")
        _render_embed(row["embed_json"])

    st.subheader("登録済みの将来アクション(ops/reminders.yaml)")
    st.caption(
        "「セッション内の約束は無効 — 将来アクションは必ずレジストリに登録」(CLAUDE.md)。"
        "pending が代表・システムが把握すべき未実行アクション。"
    )
    reminders = queries.load_reminders()
    remaining = [r for r in reminders if r["status"] == "pending"]
    # 単独カード(st.metric)にしない: 「pending 3」は比較文脈が無い。全登録数を分母に添える。
    st.markdown(f"**pending {len(remaining)} / 登録 {len(reminders)} 件**")
    if reminders:
        st.dataframe(_df(reminders), use_container_width=True, hide_index=True)


# ── 規則(組織サイト化)────────────────────────────────────────────────────────
_ENFORCEMENT_LABELS = {
    "schema": "schema(DB 制約)",
    "gate": "gate(実行時ゲート)",
    "ci": "ci(テスト・CI)",
    "audit": "audit(監査ジョブ)",
    "declaration": "宣言のみ(執行点なし)",
}


def page_rules() -> None:
    st.header("規則(定款の機械可読版 config/governance.yaml)")
    viz.page_question("どの条文に執行点があり、どれが宣言のみで実効性を欠いているか")
    gov = queries.load_governance()
    st.caption(
        f"version {gov.get('version')} / status {gov.get('status')} / "
        f"正: {gov.get('source')}(執行点に紐付かない条文は「宣言」と明示 — 定款第6条)"
    )

    st.subheader("統制テーブル(規則 × 執行点)")
    controls = gov.get("controls", [])
    decl = [c for c in controls if c.get("enforcement") == "declaration"]
    if decl:
        st.warning(
            f"宣言のみ(執行点未実装)の条文が {len(decl)} 件ある。"
            "A-18 が四半期棚卸しの対象とする(定款第6条)。"
        )
    df = _df(
        [
            {
                "規則": c.get("rule"),
                "執行点": _ENFORCEMENT_LABELS.get(c.get("enforcement"), c.get("enforcement")),
                "種別": c.get("kind"),
                "検証手続": c.get("verification"),
            }
            for c in controls
        ]
    )
    if not df.empty:
        st.dataframe(
            df.style.map(
                # schema/gate(最も強い執行点)は無着色。緑をカテゴリ識別に使わない
                # (A12・中-7)。色で立ち上がるのは弱い執行点と欠落だけにする。
                # 色は DADS トークン(理由は page_ingest と同じ — Styler は生 CSS)。
                # 執行点は列の**文字列そのもの**が種別なので、色は冗長な符号化。
                lambda v: {
                    "ci(テスト・CI)": f"color: {dads.PRIMARY}",
                    "audit(監査ジョブ)": f"color: {dads.WARNING}",
                    "宣言のみ(執行点なし)": f"color: {dads.ERROR}; font-weight: bold",
                }.get(v, ""),
                subset=["執行点"],
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("保護領域(定款第5条)")
    st.caption(
        "変更に承認記録(明示またはみなし)+ `Approved:` トレーラが必須。"
        "A-18 が git 履歴と突合する。"
    )
    pa = _df(gov.get("protected_areas", []))
    if not pa.empty:
        st.dataframe(pa, use_container_width=True, hide_index=True)

    st.subheader("統治文書(GitHub)")
    repo = github_api.DEFAULT_REPO
    blob = f"https://github.com/{repo}/blob/main"
    docs = [
        ("docs/design/06-constitution.md", "定款(最上位文書)"),
        ("config/governance.yaml", "権限マトリクス・統制テーブル(機械可読)"),
        ("docs/design/80-ips.md", "投資方針書(IPS)"),
        ("docs/design/81-fm-mandates.md", "FM マンデート"),
        ("docs/design/70-writing-standard.md", "執筆規格(保護領域)"),
        ("docs/design/05-governance.md", "ガバナンス設計"),
        ("docs/design/07-development.md", "開発管理規程"),
        ("docs/design/00-system-design.md", "全体システム設計書"),
        ("CLAUDE.md", "LLM 必読事項(不変原則・議論規約)"),
    ]
    st.markdown(
        "\n".join(f"- [{desc}]({blob}/{path}) — `{path}`" for path, desc in docs)
    )


# ── 計画(組織サイト化+2026-08-03 代表追加指示)───────────────────────────────
# GitHub REST は無認証(レート制限 60 req/h)のため全取得を 60 秒キャッシュする。
@st.cache_data(ttl=60)
def _github_open_pulls() -> list[dict[str, Any]]:
    return github_api.fetch_open_pulls()


@st.cache_data(ttl=60)
def _github_open_issues() -> list[dict[str, Any]]:
    return github_api.fetch_open_issues()


@st.cache_data(ttl=60)
def _github_merged_pulls() -> list[dict[str, Any]]:
    return github_api.fetch_merged_pulls()


@st.cache_data(ttl=60)
def _github_closed_issues() -> list[dict[str, Any]]:
    return github_api.fetch_closed_issues()


@st.cache_data(ttl=60)
def _github_ci_state(sha: str) -> str:
    return github_api.fetch_ci_state(sha)


_PHASE_LABELS = {"done": "完了", "doing": "進行中", "todo": "未着手", "future": "将来"}
_MS_ICONS = {"done": "✅", "doing": "🔧", "todo": "⬜"}
_CI_LABELS = {"success": "CI ✅", "failure": "CI ❌", "pending": "CI ⏳", "none": "CI —"}


def _within_days(iso: str | None, days: int) -> bool:
    if not iso:
        return False
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return ts >= datetime.now(UTC) - timedelta(days=days)


def _render_roadmap() -> None:
    roadmap = queries.load_roadmap()
    st.subheader("ロードマップ(config/roadmap.yaml)")
    st.caption(
        f"更新 {roadmap.get('updated')} — curated(計画の正はこのファイル。"
        "更新は設計リードの責務)。動的な状態は下段の Issues/PR/meta.runs と重ね合わせ。"
    )
    phases = roadmap.get("phases", [])
    all_ms = [m for p in phases for m in p.get("milestones", [])]
    done_ms = sum(1 for m in all_ms if m.get("status") == "done")
    if all_ms:
        viz.render_count_ratio("全体進捗(マイルストーン完了)", done_ms, len(all_ms))
    for p in phases:
        ms = p.get("milestones", [])
        p_done = sum(1 for m in ms if m.get("status") == "done")
        status = _PHASE_LABELS.get(p.get("status"), p.get("status"))
        note = f" — {p['note']}" if p.get("note") else ""
        with st.container(border=True):
            st.markdown(f"**{p.get('name')}**({status}{note})")
            st.caption(p.get("summary", ""))
            if ms:
                viz.render_count_ratio("完了", p_done, len(ms))
                st.markdown(
                    "\n".join(
                        f"- {_MS_ICONS.get(m.get('status'), '⬜')} {m.get('name')}"
                        + (f"({m['note']})" if m.get("note") else "")
                        for m in ms
                    )
                )


def _issue_lines(issues: list[dict[str, Any]]) -> str:
    # タイトルは第三者(public repo の Issue 作成者)が書いた文字列なので、リンク
    # テキストにせずリテラル化する(github_api.literal_md — 独立役員審査 低-9)。
    return "\n".join(
        f"- [#{i['number']}]({i['url']}) {github_api.literal_md(i['title'])}"
        + (f" `{', '.join(i['labels'])}`" if i["labels"] else "")
        for i in issues
    )


def page_plan(conn) -> None:
    st.header("計画")
    viz.page_question("何が代表待ちで詰まっており、いま何が動いていて、直近何が完了したか")
    _render_roadmap()

    try:
        open_issues = _github_open_issues()
        github_error = None
    except Exception as exc:  # noqa: BLE001 - レート制限・断線でも UI は生かす
        open_issues, github_error = [], exc
    if github_error is not None:
        st.warning(
            f"GitHub API から取得できない(レート制限/非公開の可能性): {github_error}"
        )

    # ── 詰んでいる・待ちタスク ──
    st.subheader("待ちタスク(詰んでいるもの)")
    roadmap = queries.load_roadmap()
    awaiting = [a.get("what") for a in roadmap.get("awaiting_representative", [])]
    user_action = [i for i in open_issues if "user-action" in i["labels"]]
    decision = [i for i in open_issues if "decision" in i["labels"] and i not in user_action]
    impl_queue = [i for i in open_issues if i not in user_action and i not in decision]

    st.markdown("**代表待ち(あなたの判断・作業が必要)**")
    if not awaiting and not user_action:
        st.info("代表待ちはない")
    else:
        for what in awaiting:
            st.markdown(f"- ⏸️ {what}(curated: config/roadmap.yaml)")
        if user_action:
            st.markdown(_issue_lines(user_action))

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**判断待ち(decision ラベル)**")
        st.markdown(_issue_lines(decision) if decision else "なし")
    with col2:
        st.markdown("**実装キュー(その他の open Issue)**")
        st.markdown(_issue_lines(impl_queue) if impl_queue else "なし")

    st.markdown("**登録済みの将来アクション(ops/reminders.yaml・pending)**")
    pending = [r for r in queries.load_reminders() if r["status"] == "pending"]
    if pending:
        st.dataframe(_df(pending), use_container_width=True, hide_index=True)
    else:
        st.info("pending の将来アクションはない")

    # ── 実行中 ──
    st.subheader("実行中")
    running = queries.fetch_running_runs(conn)
    if not running:
        st.info("実行中のジョブはない(meta.runs)")
    else:
        st.dataframe(_df(running), use_container_width=True, hide_index=True)
    st.markdown("**Open PR(CI 状態付き)**")
    try:
        pulls = _github_open_pulls()
        if not pulls:
            st.info("open PR なし")
        for p in pulls:
            state = _github_ci_state(p["head_sha"]) if p["head_sha"] else "none"
            ci = _CI_LABELS.get(state, "CI —")
            draft = "(draft)" if p["draft"] else ""
            st.markdown(
                f"- [#{p['number']}]({p['url']}) {github_api.literal_md(p['title'])}"
                f" {draft} — {ci}"
            )
    except Exception as exc:  # noqa: BLE001
        st.warning(f"GitHub API から取得できない: {exc}")

    # ── 完了 ──
    st.subheader("完了(直近14日)")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**マージ済み PR**")
        try:
            merged = [p for p in _github_merged_pulls() if _within_days(p["merged_at"], 14)]
            st.markdown(
                "\n".join(
                    f"- [#{p['number']}]({p['url']}) "
                    f"{github_api.literal_md(p['title'])}({p['merged_at'][:10]})"
                    for p in merged
                )
                if merged
                else "なし"
            )
        except Exception as exc:  # noqa: BLE001
            st.warning(f"GitHub API から取得できない: {exc}")
    with col2:
        st.markdown("**クローズした Issue**")
        try:
            closed = [i for i in _github_closed_issues() if _within_days(i["closed_at"], 14)]
            st.markdown(
                "\n".join(
                    f"- [#{i['number']}]({i['url']}) "
                    f"{github_api.literal_md(i['title'])}({i['closed_at'][:10]})"
                    for i in closed
                )
                if closed
                else "なし"
            )
        except Exception as exc:  # noqa: BLE001
            st.warning(f"GitHub API から取得できない: {exc}")

    st.markdown("**T タスクの完了履歴(site/data.js)**")
    site = queries.load_site_status()
    if site is None:
        st.info("site/data.js がない(`python3 site/build.py` で生成)")
    else:
        t_done = [
            i
            for i in site.get("issues", [])
            if i.get("state") == "CLOSED" and str(i.get("title", "")).startswith("T-")
        ]
        st.markdown(
            "\n".join(
                f"- ✅ #{i['number']} {github_api.literal_md(i['title'])}" for i in t_done
            )
            if t_done
            else "なし"
        )
    st.caption("GitHub 由来の情報は 60 秒キャッシュ(無認証 REST・レート制限対策)。")


# ── 役員室(Issue #9・05-governance §5)────────────────────────────────────────
@st.cache_resource
def _boardroom_conn():
    """役員室専用の**書込可**接続(``queries.connect_boardroom``)。

    設計判断: 読取ページの接続は流用しない。分離の実体は**別 DB ロール**であり
    (読取 = ``ryza_dashboard`` / 役員室 = ``ryza_boardroom``。詳細は queries.py と
    ops/deploy-dashboard.sh)、役員室が侵害されても書込先は追記オンリーの
    governance 3テーブルと meta.runs に限られる。
    """
    return queries.connect_boardroom()


@st.cache_resource
def _llm_config() -> LLMConfig:
    """モデル階層 → モデル ID・単価(config/llm.yaml)。"""
    return LLMConfig.load()


def _provider_for(tier: str) -> AnthropicProvider:
    """階層別の AnthropicProvider(max_tokens が階層で違うためセッション内でキャッシュ)。

    API キーは渡さない — providers.load_api_key の既定(env → Secret Manager)に任せる。
    """
    providers = st.session_state.setdefault("br_providers", {})
    if tier not in providers:
        cfg = _llm_config()
        providers[tier] = AnthropicProvider(
            api_version=cfg.api_version, max_tokens=cfg.max_tokens_for(tier)
        )
    return providers[tier]


def _boardroom_llm(run, tier: str) -> StructuredLLM:
    """コスト記録付きクライアント(部門タグ governance — 予算科目「役員」の集計元)。"""
    return StructuredLLM(
        _provider_for(tier), run, dept_tag="governance",
        price_per_1k=_llm_config().price_map(),
    )


def _role_member(role: str) -> org.Member | None:
    """役職キー → 台帳メンバー(config/org.yaml)。未登録の役職は None(既定表示で継続)。"""
    try:
        return org.member_for_role(role)
    except KeyError:
        return None


@st.cache_data(ttl=10)
def _icon_overrides() -> dict[str, str]:
    """代表が設定したアイコン上書き(0020・``ops.org_icon_overrides``)。

    DB に繋がらない場合は空(台帳のアイコンで表示を続ける — 組織ページと同じ方針)。
    発言ごとに引くため 10 秒だけキャッシュする(組織ページの即時反映は別経路)。
    """
    try:
        return org.icon_overrides(_boardroom_conn())
    except Exception:  # noqa: BLE001 - 表示は台帳へフォールバック
        return {}


def _role_display(role: str) -> str:
    """役職の表示は「名前(役職)」(代表指示 2026-08-03)。例:「エミリア(CIO)」。

    役職名は会議の役職ラベル(``BOARDROOM_ROLES``)を優先する。台帳の title は
    「CIO(執行統括)」のように括弧を含み、入れ子になって読みにくいため。
    台帳に無い役職は台帳の表記(``Member.display_name``)→ 役職キーの順に落とす。
    """
    member = _role_member(role)
    label = boardroom.BOARDROOM_ROLES.get(role)
    if member is None:
        return label or role
    return f"{member.name}({label})" if label else member.display_name


def _role_avatar(role: str) -> str | None:
    """チャット吹き出しのアバター。

    代表が設定したアイコン上書き(0020・``ops.org_icon_overrides``)を最優先し、
    無ければリポジトリ内の SVG(Streamlit は表示可)→ 台帳の icon_url の順に落とす。
    """
    member = _role_member(role)
    if member is None:
        return None
    override = _icon_overrides().get(member.id)
    if override:
        return override
    path = member.icon_repo_path
    if path.exists():
        return str(path)
    return member.icon_url or None


def _render_chat_turn(turn: boardroom.ChatTurn) -> None:
    """1 発言を吹き出し表示する。役職側は名前(役職)+キャラクター色+アバター。

    発言しなかった役員は単に現れない(会議の進行役が発言者を選ぶ方式のため — 05 §5)。
    進行役の定型応答(LLM ではない)はキャラクターを持たないため小さく表示する。
    決定論ガードで呼ばれた発言はその旨をキャプションに出す(選定経路の可視化)。
    """
    if turn.speaker == "representative":
        with st.chat_message("user"):
            st.markdown(turn.text)
        return
    if turn.speaker == boardroom.FACILITATOR_SPEAKER:
        st.caption(turn.text)
        return
    member = _role_member(turn.speaker)
    with st.chat_message("assistant", avatar=_role_avatar(turn.speaker)):
        st.markdown(
            f'<span style="color:{member.color if member else "inherit"};font-weight:600">'
            f"{_role_display(turn.speaker)}</span>",
            unsafe_allow_html=True,
        )
        if turn.source == "guard":
            st.caption("重要決定の兆候を検出したため、決定論ガードが発言を要求(05 §3)")
        st.markdown(turn.text)


# 役員の発言は 05 §3 の設計階層(CIO・独立役員=fable)どおり fable 固定 — 2026-08-03
# 代表指示。誰が発言するかを選ぶルータ段は安価な mid 固定(交通整理に高階層は要らない)。
# 1ターンの fable 呼び出しは boardroom.MAX_SPEECHES_PER_TURN 件で打ち切る。
_BOARDROOM_TIER = "fable"
_ROUTER_TIER = "mid"


def page_boardroom() -> None:
    st.header("役員室")
    viz.page_question(
        "提起した論点に役員はどう反応するか(会議 → 議事録 → 決議マーク)"
    )
    st.caption(
        "経営レベルの審議の場(05-governance §5)。代表が発言すると、進行役が"
        "**反応すべきと判断した役員だけ**が順に応答する会議形式(2026-08-03 代表指示)。"
        "対話は判断材料であり、何も自動執行しない(不変原則1)。"
        "発効する決定は「決議」マークのみ。"
    )
    try:
        wconn = _boardroom_conn()
    except Exception as exc:  # noqa: BLE001 - DB 停止時も UI は説明を出して生かす
        st.error(f"DB に接続できない: {exc}")
        return

    st.caption(
        "出席: 代表、"
        + "、".join(_role_display(r) for r in boardroom.MEETING_ORDER)
        + f" / 発言={_BOARDROOM_TIER}・進行役={_ROUTER_TIER}(固定)"
        + f" / 1ターン最大 {boardroom.MAX_SPEECHES_PER_TURN} 発言"
    )

    turns: list[boardroom.ChatTurn] = st.session_state.setdefault("br_turns", [])
    for turn in turns:
        _render_chat_turn(turn)

    text = st.chat_input("会議での発言(あなたは代表として話す)")
    if text:
        turns.append(boardroom.ChatTurn("representative", text))
        _render_chat_turn(turns[-1])
        spoke_before = len(turns)

        def _append_and_render(turn: boardroom.ChatTurn) -> None:
            """発言が出るたびに会話へ追記し、その場で描画する(逐次表示)。"""
            turns.append(turn)
            _render_chat_turn(turn)

        try:
            # LLM 呼び出しは会議1ターン(ルータ段+発言段+反応ラウンド)で 1 Run に
            # まとめる(コスト記録の単位 — ルータ呼び出しも同 Run に入る)。セッション
            # 単位の Run にしない理由: Streamlit にはセッション終了フックがなく、
            # ブラウザを閉じると 'running' 行が漏れ残るため。
            with run_ctx(
                "dashboard.boardroom.meeting",
                {"speaker_tier": _BOARDROOM_TIER, "router_tier": _ROUTER_TIER},
                conn=wconn,
            ) as r:
                with st.spinner(
                    f"進行役({_ROUTER_TIER})が発言者を選び、役員({_BOARDROOM_TIER})が"
                    "順に発言中…"
                ):
                    result = boardroom.conduct_meeting(
                        router_llm=_boardroom_llm(r, _ROUTER_TIER),
                        speaker_llm=_boardroom_llm(r, _BOARDROOM_TIER),
                        # 着任プロンプトは役職ごとに毎回組み立てる(永続記憶は役職別 —
                        # 05 §2・§6-2。会議で共有されるのはトランスクリプトのみ)。
                        onboarding_for_role=lambda role: personas.assume_role(wconn, role),
                        turns=turns,
                        router_model=_llm_config().model_for(_ROUTER_TIER),
                        router_tier=_ROUTER_TIER,
                        speaker_model=_llm_config().model_for(_BOARDROOM_TIER),
                        speaker_tier=_BOARDROOM_TIER,
                        # 発言が出るたびに追記・描画する(途中で失敗しても既に得た
                        # 発言は残す — 会議で実際にあった発言を握り潰さない)。
                        on_reply=_append_and_render,
                    )
                # ルータ・ガードの選定結果を Run の runtime 名前空間に残す
                # (入力証跡は書き換えない — provenance.Run.record_runtime)。
                r.record_runtime({
                    "rounds": len(result.rounds),
                    "roles": result.rounds,
                    "guard_fired": result.guard_fired,
                })
        except Exception as exc:  # noqa: BLE001 - API 失敗時も会議を壊さず継続
            if len(turns) == spoke_before:
                turns.pop()  # 誰も発言できなかった場合は代表の発言ごと取り消す
                st.error(f"応答の生成に失敗: {exc}")
            else:
                st.error(f"会議は途中で中断した(既出の発言は保持): {exc}")
            return

    st.divider()
    if st.button("議事録として保存(主張・懸念も蓄積)", disabled=not turns):
        try:
            roles = boardroom.speaking_roles(turns)  # 発言した役職のみ要約する
            with st.spinner("議事録を保存し、主張・懸念を要約中…"):
                with run_ctx(
                    "dashboard.boardroom.save", {"roles": roles}, conn=wconn
                ) as r:
                    held_at = datetime.now(UTC)
                    saved = boardroom.save_office_chat_minute(
                        wconn, turns=turns, run_id=r.run_id, held_at=held_at
                    )
                    # 要約は mid 固定(応答の階層とは独立 — 要約に fable は不要)。
                    # 入力は当該役職+代表の発言のみに決定論フィルタ(記憶の分離 —
                    # 05 §6-2。他役職の主張が永続記憶へ混入する経路を構造的に塞ぐ)。
                    stance_ids: list[int] = []
                    for role in roles:
                        digest = boardroom.digest_stances(
                            _boardroom_llm(r, "mid"),
                            role=role,
                            transcript_md=boardroom.role_digest_input(
                                turns, role, held_at=held_at
                            ),
                            model=_llm_config().model_for("mid"),
                            model_tier="mid",
                        )
                        stance_ids += boardroom.record_chat_stances(
                            wconn, role=role, stances=digest,
                            minute_id=saved.minute_id, run_id=r.run_id,
                        )
            st.session_state["br_minute_id"] = saved.minute_id
            st.success(
                f"議事録 #{saved.minute_id} を保存(stances へ {len(stance_ids)} 件追記)"
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"保存に失敗: {exc}")

    minute_id = st.session_state.get("br_minute_id")
    if minute_id:
        st.subheader(f"決議マーク(議事録 #{minute_id})")
        # 決議ボタンは代表のみ押せる建前(05 §5)。本ダッシュボードは Cloud Run + IAP で
        # 公開されており、操作者=代表とみなせる根拠は**IAP 許可リストが代表1名のみ**で
        # あること(ops/deploy-dashboard.sh が set-iam-policy で DASHBOARD_USER 1名へ
        # 宣言的に収束させる — 独立役員審査 2026-08-03 中-5)。許可リストを増やすと
        # この前提は崩れ、増えた人物が代表名義で決議をマークできる。増やす場合は
        # 操作者の識別(IAP の X-Goog-Authenticated-User-Email)を先に実装すること。
        # resolved_by='representative' は 0013 の CHECK でも DB 側から強制される。
        with st.form("resolution_form", clear_on_submit=True):
            title = st.text_input("決議タイトル")
            body = st.text_area("決議本文(反対意見・却下理由も残す — 05 §6-3)")
            proposal_ref = st.text_input(
                "proposal_ref(承認事項なら governance.decisions と突合。任意)"
            )
            # 決定論チェック(05 §3): **最後の代表発言より後に**独立役員が発言して
            # いない議事録の決議には明示確認を求める(批判の鮮度 — 再確認審査 懸念A)。
            # ブロックではなく摩擦であり、決議権は代表に残る(定款第3条)。確認して
            # 通した決議は confirmed_without_critic=true で残り(0025)、連続は
            # 形骸化アラートの対象になる(05 §6-5)。
            confirm = st.checkbox(
                "独立役員の批判(最後の代表発言より後の発言)を経ていない議事録でも"
                "決議する(内容を理解した上で)"
            )
            if st.form_submit_button("決議としてマーク(代表として)"):
                if not title.strip() or not body.strip():
                    st.warning("タイトルと本文は必須")
                else:
                    try:
                        rid = boardroom.mark_resolution(
                            wconn, minute_id=minute_id, title=title.strip(),
                            resolution_md=body,
                            proposal_ref=proposal_ref.strip() or None,
                            confirmed_without_critic=confirm,
                        )
                        st.success(f"決議 #{rid} をマークした")
                    except boardroom.CriticAbsentError as exc:
                        st.warning(
                            f"{exc}。上のチェックボックスで明示確認するか、"
                            "独立役員に発言させてから保存し直すこと。"
                        )
        for res in boardroom.fetch_resolutions(wconn, minute_id):
            ref = f" / proposal_ref: {res['proposal_ref']}" if res["proposal_ref"] else ""
            mark = " ⚠ 批判を経ない決議" if res["confirmed_without_critic"] else ""
            st.caption(
                f"決議 {res['seq']}: {res['title']}(#{res['resolution_id']}{ref}){mark}"
            )
        # 形骸化の監査(05 §6-5)を決議の現場にも出す。週次ダイジェストだけに置くと、
        # 「毎回チェックを外す」当人が自分の連続数を見ないまま運用できてしまう。
        stats = boardroom.resolution_confirmation_stats(wconn)
        if stats.confirmed:
            line = boardroom.confirmation_status_line(stats)
            (st.warning if stats.alert else st.caption)(f"確認付き決議: {line}")


# ── 開発室(代表 ⇄ 設計リード — 代表指示 2026-08-03)────────────────────────────
# 役員室が「経営レベルの審議」の場なのに対し、ここは**開発の連絡窓口**である。
# 代表の発言は ``ops.dev_chat``(0024・追記オンリー)に入り、Bot が Discord の #dev へ
# 中継する。設計リード(Claude Code セッション)は
# ``python -m ryza.governance.devchat --reply`` で同じスレッドへ返す。
# 書込は役員室と同じ最小権限ロール ``ryza_boardroom``(``_boardroom_conn``)で行い、
# 操作者=代表とみなせる根拠も役員室と同じ(IAP 許可リストが代表1名 — app.py 冒頭)。
_DEV_CHAT_LIMIT = 200
_DEV_CHAT_REFRESH_SECONDS = 10

#: 設計リードの役職キー(``config/org.yaml`` の persona=personas/dev-lead)。
_DEV_LEAD_ROLE = "dev_lead"

#: この秒数を超えて未中継の発言は「滞留」として警告する(独立役員審査 中-7)。
#: 正常時の中継は Bot の 5 秒ループで数秒以内に終わるため、2 分は十分に余裕がある。
#: 中継が全滅しても UI が「中継待ち」と表示し続けて障害が沈黙する経路を塞ぐ。
_DEV_CHAT_STALE_SECONDS = 120


def _dev_relay_caption(msg: devchat.DevChatMessage) -> str:
    """発言の時刻と中継状態(独立役員審査 中-7 の滞留表示を含む)。

    中継状態を必ず添える。中継前は相手の目にまだ触れていないため、「送ったのに反応が
    無い」を「まだ届いていない」と区別できないと、同じ連絡を二度書くことになる。
    """
    when = f"{msg.created_at:%m-%d %H:%M}"
    if msg.relayed:
        return f"{when} / Discord へ中継済み"
    age = (datetime.now(UTC) - msg.created_at).total_seconds()
    if age > _DEV_CHAT_STALE_SECONDS:
        return f"{when} / :red[**中継されていない**({viz.fmt_hours(age / 3600)}経過)]"
    return f"{when} / 中継待ち(数秒)"


def _render_dev_turn(msg: devchat.DevChatMessage) -> None:
    """1 発言を吹き出し表示する(代表は user 側、設計リードはキャラクター付き)。"""
    if msg.sender == devchat.REPRESENTATIVE:
        with st.chat_message("user"):
            st.markdown(msg.body)
            st.caption(_dev_relay_caption(msg))
        return
    member = _role_member(_DEV_LEAD_ROLE)
    with st.chat_message("assistant", avatar=_role_avatar(_DEV_LEAD_ROLE)):
        st.markdown(
            f'<span style="color:{member.color if member else "inherit"};font-weight:600">'
            f"{_role_display(_DEV_LEAD_ROLE)}</span>",
            unsafe_allow_html=True,
        )
        st.markdown(msg.body)
        st.caption(_dev_relay_caption(msg))


def _dev_chat_stale_warning(conn) -> None:
    """中継が滞留していることを画面で名指しする(独立役員審査 中-7)。

    Bot が落ちる・DB 権限が欠ける等で中継が全滅しても、個々の吹き出しが「中継待ち」と
    出るだけでは異常に見えない。**滞留件数と最古の経過時間**を警告として最上部に出す。
    """
    stale = devchat.stale_unrelayed(conn, older_than_seconds=_DEV_CHAT_STALE_SECONDS)
    if not stale:
        return
    oldest = (datetime.now(UTC) - stale[0].created_at).total_seconds() / 3600
    st.warning(
        f"**{len(stale)} 件が Discord へ中継されていない**(最古 {viz.fmt_hours(oldest)} 前)。"
        "Bot の配送ループ(bot.devchat.relay)が止まっているか、中継が失敗し続けている。"
        "「ジョブ」ページで直近の bot.devchat.relay の status を確認すること。"
    )


@st.fragment(run_every=_DEV_CHAT_REFRESH_SECONDS)
def _dev_chat_thread(conn) -> None:
    """スレッド表示。**fragment なのでページ全体を再実行せずに自動更新する**。

    設計リードの返信は数十秒〜数分後に別プロセス(CLI)から入るため、代表が画面を
    手動で更新しなければ届いたことに気付けない。``run_every`` はこの fragment だけを
    再実行するので、LLM を呼ぶ他ページと違い再取得のコストは SELECT 2 本に収まる。

    **例外は必ず捕まえる**(独立役員審査 軽-9)。``@st.cache_resource`` が保持する接続は
    DB の再起動やアイドル切断で死ぬことがあり、fragment の中で例外が出ると自動更新が
    その場で止まって「返信が来ない」ように見える。接続キャッシュを捨てて次の周期で
    開き直させる。
    """
    try:
        _dev_chat_stale_warning(conn)
        messages = devchat.fetch_thread(conn, limit=_DEV_CHAT_LIMIT)
    except Exception as exc:  # noqa: BLE001 - 自動更新を止めない(次周期で再接続)
        _boardroom_conn.clear()
        st.error(f"スレッドを取得できなかった(次の自動更新で再接続する): {exc}")
        return
    if not messages:
        st.info("まだ連絡はない。下の入力欄から設計リードへ送る。")
    for msg in messages:
        _render_dev_turn(msg)
    st.caption(
        f"直近 {len(messages)} 件(上限 {_DEV_CHAT_LIMIT})/ "
        f"{_DEV_CHAT_REFRESH_SECONDS} 秒ごとに自動更新"
    )


def page_dev_chat() -> None:
    st.header("開発室")
    viz.page_question(
        "設計リードへ何を伝え、設計リードから何が返ってきたか(開発の連絡と進捗)"
    )
    st.caption(
        "設計リードへの連絡窓口。応答は非同期(数十秒〜数分)。"
        "実装・PR・デプロイの実働はセッション側で行われる。"
    )
    try:
        conn = _boardroom_conn()
    except Exception as exc:  # noqa: BLE001 - DB 停止時も UI は説明を出して生かす
        st.error(f"DB に接続できない: {exc}")
        return
    st.caption(
        f"投稿は ops.dev_chat(追記オンリー・0024)に残り、Bot が Discord の #dev へ"
        f"「{devchat.RELAY_PREFIX}…」の形で中継する。設計リードの返信は "
        "`python -m ryza.governance.devchat --reply` で同じスレッドへ入り、"
        "**双方向とも Discord に出る**(外出中は Discord をミラーとして読める)。"
        "**ここでの連絡は指示であって承認ではない** — 保護領域の変更には"
        "従来どおり承認記録が要る(定款第5条)。"
    )
    _dev_chat_thread(conn)
    text = st.chat_input("設計リードへの連絡(あなたは代表として書く)")
    if text:
        try:
            devchat.post_representative(conn, text)
        except Exception as exc:  # noqa: BLE001 - 権限不足等も画面に出す
            st.error(f"投稿に失敗: {exc}")
            return
        st.rerun()  # 追記した発言を即座にスレッドへ出す


# ── エントリポイント ──────────────────────────────────────────────────────────


# ── エントリポイント(st.navigation・2026-08-03 デザイン改修)─────────────────
# 旧実装はサイドバーの ``st.radio`` 1 本に 14 ページを平積みし、選択後に if/dict で
# 分岐していた。代表の指摘「ページ切替ボタンが小さい」は、ラジオのクリック領域が
# 14 行ぶん圧縮されて WCAG 2.1 SC 2.5.5(44×44 px)を大きく割っていたことによる。
# ``st.navigation`` へ移すと (a) Streamlit がページリンクとして描くため CSS で
# タップターゲットを確保でき、(b) sections で意味的にグルーピングでき、(c) 現在地が
# 強調表示され、(d) ページごとに URL が付く(ブックマーク可能になる)。
# 根拠: docs/research/dads-streamlit-application.md §4・§5。
#
# **ページ関数の構造は変えていない** — ``page_overview(conn)`` 等はそのまま残し、
# 接続の解決だけを下のラッパが担う。st.Page が受け取れるのは引数なしの callable のため。
def _with_conn(page_fn):
    """読取接続を解決して ``page_fn(conn)`` を呼ぶ、引数なしラッパを作る。

    DB に繋がらないときはページを落とさず、説明を出して空のページを描く(旧 ``main``
    と同じ挙動)。ナビゲーション自体は描画済みなので、代表は他のページへ移動できる。
    """

    def _run() -> None:
        try:
            conn = _conn()
        except Exception as exc:  # noqa: BLE001 - DB 停止時も UI は説明を出して生かす
            st.error(f"DB に接続できない: {exc}")
            st.caption("compose.yaml の PostgreSQL 起動と RYZA_DATABASE_URL を確認。")
            return
        page_fn(conn)

    _run.__name__ = page_fn.__name__
    _run.__doc__ = page_fn.__doc__
    return _run


def _page_org() -> None:
    """組織ページ。台帳(config/org.yaml)が主で DB は従。

    アイコンの上書き(0020)の読取にだけ DB を使うため、接続できなくても台帳だけで
    ページを出す(編集 UI は隠す)。``_with_conn`` は使えない — 接続失敗を
    エラー表示で終わらせず ``None`` として先へ進める必要がある。
    """
    try:
        org_conn = _conn()
    except Exception:  # noqa: BLE001 - DB 停止時も組織ページは表示する
        org_conn = None
    page_org(org_conn)


#: サイドバーの構成。``{セクション名: [(タイトル, url_path, アイコン, 描画関数)]}``。
#:
#: グルーピングの軸は「代表がその画面を開く動機」。①いま止めるべき事象が起きていないか
#: (監視)②数字はどうなっているか(成績・リスク)③誰が何を決めたか(組織・統治)
#: ④開発は進んでいるか(開発)。概況からのドリルダウン先(ジョブ・取込)は概況と同じ
#: セクションに置き、Shneiderman の overview first → details-on-demand を崩さない。
#:
#: ``url_path`` はブックマーク可能な URL になるうえ、**ページの同一性そのもの**である
#: (Streamlit はページのハッシュを url_path から導出する)。テストもこれでページを
#: 指名するため、**タイトルを変えても url_path は変えない**こと。
NAV_SECTIONS: dict[str, list[tuple[str, str, str, Any]]] = {
    "監視": [
        # 概況は default=True(url_path は "" に固定され、アプリのトップになる)。
        ("概況", "overview", ":material/monitor_heart:", _with_conn(page_overview)),
        ("ジョブ", "jobs", ":material/schedule:", _with_conn(page_jobs)),
        ("取込", "ingest", ":material/download:", _with_conn(page_ingest)),
        ("報道", "press", ":material/newspaper:", _with_conn(page_press)),
        ("市場観", "market-view", ":material/insights:", _with_conn(page_market_view)),
    ],
    "成績・リスク": [
        ("成績", "performance", ":material/trending_up:", _with_conn(page_performance)),
        ("リスク", "risk", ":material/warning:", _with_conn(page_risk)),
        ("コスト", "cost", ":material/payments:", _with_conn(page_cost)),
    ],
    "組織・統治": [
        ("組織", "org", ":material/groups:", _page_org),
        ("規則", "rules", ":material/gavel:", page_rules),  # DB 不要(governance.yaml のみ)
        ("承認・通知", "approvals", ":material/how_to_reg:", _with_conn(page_approvals)),
        # 役員室は書込可の専用接続を自前で持つ(READ ONLY 接続は使わない)。
        ("役員室", "boardroom", ":material/forum:", page_boardroom),
    ],
    "開発": [
        # 開発室(#68)は「代表 → 設計リードの連絡窓口」なので開発グループに置く。
        # 役員室(経営レベルの審議)とは場が違うため組織・統治には入れない。
        # 役員室と同じく書込可の専用接続を自前で持つ(_boardroom_conn)。
        ("開発室", "dev-chat", ":material/chat:", page_dev_chat),
        ("計画", "plan", ":material/checklist:", _with_conn(page_plan)),
        ("開発ステータス", "dev-status", ":material/code:", page_dev_status),
    ],
}

#: 概況を既定ページにする(url_path が "" になり、ルート URL で開く)。
DEFAULT_URL_PATH = "overview"


def _with_dads_css(page_fn):
    """ページ描画の**先頭で** DADS の CSS 層を注入するラッパ。

    ``main()`` で1回注入するのでは効かない —— ``page.run()`` がメインコンテナを
    リセットするため、それより前に書いた ``st.html`` はブラウザに届かない
    (2026-08-03 の実ブラウザ検証で判明。AppTest では緑のまま検出できなかった)。
    ページ自身の描画パスの中で書く必要があるので、全ページに一律で被せる。
    詳細と代替案(サイドバーへ逃がす案が失敗する理由)は ``dads.inject`` の docstring。
    """

    def _run() -> None:
        dads.inject()
        page_fn()

    _run.__name__ = page_fn.__name__
    _run.__doc__ = page_fn.__doc__
    return _run


def _build_pages() -> dict[str, list[Any]]:
    """``NAV_SECTIONS`` を ``st.navigation`` が取る ``{セクション: [st.Page]}`` にする。"""
    return {
        section: [
            st.Page(
                _with_dads_css(fn),
                title=title,
                icon=icon,
                url_path=url_path,
                default=url_path == DEFAULT_URL_PATH,
            )
            for title, url_path, icon, fn in items
        ]
        for section, items in NAV_SECTIONS.items()
    }

def main() -> None:
    # CSS 層(44px タップターゲット・行間・フォーカスリング)は**ここで注入しない**。
    # page.run() がメインコンテナをリセットするため、ここで書いてもブラウザには届かない
    # (実ブラウザ検証で判明 — _with_dads_css と dads.inject の docstring)。
    # 注入は _build_pages が全ページに被せる _with_dads_css が行う。
    # expanded=True。既定(False)は 13 ページ以上で 10 件に折り畳み「View 4 more」を
    # 出すが、監視面は**全ページが常に見えている**ことが要件(どこに何があるかを
    # 探させない)。折り畳みボタン自体もタップターゲットを1つ増やす。
    page = st.navigation(_build_pages(), position="sidebar", expanded=True)
    # サイドバーへの追記は必ずナビゲーションの**下**に出る(st.navigation はサイドバー
    # 先頭に固定される)。旧実装の st.sidebar.title はここでは脚注の位置に落ちて
    # 読み手を混乱させるため置かない —— アプリ名はブラウザのタブ(set_page_config)と
    # 各ページの見出しが担っており、サイドバーで名乗り直す必要がない。
    # 残すのは「この画面で何ができないか」の断り書きだけで、脚注として妥当な内容。
    st.sidebar.divider()
    st.sidebar.caption(
        "Cloud Run + IAP で公開(許可アカウントのみ。認証は IAP に全面委譲)+ローカル。"
        "役員室以外は読み取り専用。操作(Kill Switch 等)は Discord から。"
    )
    page.run()


main()
