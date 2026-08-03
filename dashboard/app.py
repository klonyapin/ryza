"""dashboard/app — Ryza 運用ダッシュボード(Streamlit・Issue #10 → 組織サイト化)。

**Cloud Run + IAP で公開(2026-08-03 代表指示)+ローカル実行。役員室タブを除き
読み取り専用。** アクセス制御は IAP の許可リスト(roles/iap.httpsResourceAccessor)に
全面委譲し、アプリ内に認証コードは置かない(2026-08-02 の無認証 Cloud Run 公開版とは
異なり、IAP が Google アカウント認証を強制する。デプロイ: ops/deploy-dashboard.sh)。
Kill Switch 等の操作系 UI は置かない(Discord Bot の管轄)。唯一の例外が「役員室」
(Issue #9・05-governance §5)で、議事録・決議マーク・stances の**追記**だけを行う
(発注・設定変更の経路は持たない)。

起動: ``.venv/bin/streamlit run dashboard/app.py``(README 参照)。
接続先は用途で2本に分かれる(独立役員審査 2026-08-03 重大-2 の是正):
読取は env ``RYZA_DATABASE_URL``(読取専用ロール ``ryza_dashboard``)、役員室の書込は
env ``RYZA_BOARDROOM_DATABASE_URL``(最小権限ロール ``ryza_boardroom``)。ローカルでは
後者を省略でき、その場合は前者にフォールバックする(``queries.connect_boardroom``)。
役員室の LLM 呼び出しは Anthropic API キーが必要(env RYZA_ANTHROPIC_API_KEY /
ANTHROPIC_API_KEY、または Secret Manager — providers.load_api_key の既定に任せる)。

DB アクセスは ``queries.py``(接続と読取)と ``ryza.governance.boardroom``(役員室の
書込・テスト対象)に分離し、本ファイルは表示だけを担う。
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

import github_api  # noqa: E402
import queries  # noqa: E402

from ryza import org  # noqa: E402
from ryza.governance import boardroom, personas  # noqa: E402
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


# ── 概況 ──────────────────────────────────────────────────────────────────────
def page_overview(conn) -> None:
    st.header("概況")

    state = queries.fetch_trading_state(conn)
    col1, col2 = st.columns([1, 2])
    with col1:
        st.subheader("取引状態")
        if state is None:
            st.info("ops.trading_state 未初期化(Bot 未起動)")
        else:
            label = _STATE_LABELS.get(state["state"], state["state"])
            if state["state"] == "normal":
                st.success(f"状態: {label}")
            else:
                st.error(f"状態: {label}")
            st.caption(
                f"更新: {state['updated_at']} / {state['updated_by']}"
                + (f" — {state['reason']}" if state["reason"] else "")
            )
    with col2:
        st.subheader("直近の日次サイクル")
        summary = queries.fetch_latest_daily_summary(conn)
        if summary is None:
            st.info("日次サイクルの実行サマリはまだない")
        else:
            sent = summary["sent_at"] or "未配送"
            st.caption(f"outbox #{summary['id']} / 投入 {summary['created_at']} / 配送 {sent}")
            _render_embed(summary["embed_json"])

    st.subheader("直近のジョブ実行(meta.runs)")
    runs = queries.fetch_recent_runs(conn, limit=30)
    if not runs:
        st.info("実行記録なし")
    else:
        st.dataframe(_df(runs), use_container_width=True, hide_index=True)


# ── 取込 ──────────────────────────────────────────────────────────────────────
def page_ingest(conn) -> None:
    st.header("取込")

    st.subheader("ソース別鮮度(SLA)")
    freshness = _df(queries.fetch_freshness(conn))
    breaches = freshness[freshness["status"] != "ok"]
    ok_count = int((freshness["status"] == "ok").sum())
    st.caption(f"SLA 充足 {ok_count}/{len(freshness)} ソース(違反 {len(breaches)} 件)")
    st.dataframe(
        freshness.style.map(
            lambda v: {
                "ok": "color: green",
                "stale": "color: orange",
                "no_data": "color: red",
            }.get(v, ""),
            subset=["status"],
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("日別取込件数(直近30日・as_of 基準)")
    counts = queries.fetch_ingest_daily_counts(conn, days=30)
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
    st.caption(
        "cost jsonb に部門次元は無いため、部門は job_name の先頭セグメントで代理"
        "(ingest.jquants.daily → ingest)。金額はモデル階層別単価による概算。"
    )
    rows = queries.fetch_cost_daily(conn, days=30)
    if not rows:
        st.info("直近30日にコスト記録のある実行なし")
        return
    df = _df(rows)
    df["cost_estimate"] = df["cost_estimate"].astype(float)

    total = df["cost_estimate"].sum()
    tokens = int(df["tokens"].sum())
    col1, col2, col3 = st.columns(3)
    col1.metric("30日合計(概算)", f"¥{total:,.2f}")
    col2.metric("トークン合計", f"{tokens:,}")
    col3.metric("呼び出し回数", f"{int(df['calls'].sum()):,}")

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
            pct = min(max(float(confidence), 0.0), 1.0)
            st.progress(pct, text=f"確信度 {pct:.0%}")
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

_ORG_CSS = """
<style>
.oc-apex { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:6px; }
.oc-node { border:1px solid rgba(128,128,128,.45); border-radius:8px; padding:8px 14px;
  font-size:.85rem; }
.oc-node b { display:block; }
.oc-node small { opacity:.7; }
.oc-ic { border-color:#c9a24b; border-width:2px; }
.oc-aud { border-style:dashed; }
.oc-vline { width:2px; height:16px; background:rgba(128,128,128,.45); margin:0 0 6px 40px; }
.oc-offices { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
  gap:10px; }
.oc-office { border:1px solid rgba(128,128,128,.35); border-radius:10px; padding:10px 12px; }
.oc-office h4 { margin:0 0 6px; font-size:.75rem; opacity:.7; letter-spacing:.06em; }
.oc-office ul { list-style:none; margin:0; padding:0; }
.oc-office li { font-size:.8rem; padding:4px 8px; margin:4px 0; border-radius:6px;
  border:1px solid rgba(128,128,128,.3); }
.oc-office li small { display:block; opacity:.65; font-size:.68rem; }
.oc-flow { display:flex; flex-wrap:wrap; gap:6px; align-items:center; font-size:.78rem;
  margin-top:10px; }
.oc-flow span.s { border:1px solid rgba(128,128,128,.4); border-radius:6px; padding:2px 9px; }
.oc-flow span.g { border-color:#d9a441; color:#d9a441; }
.oc-flow span.a { opacity:.6; }
.oc-members { display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
  gap:12px; margin-top:6px; }
.oc-card { border:1px solid rgba(128,128,128,.35); border-radius:12px; padding:14px;
  display:flex; gap:12px; border-top:3px solid var(--mc,#888); }
.oc-avatar { width:64px; height:64px; border-radius:50%; flex:none; object-fit:cover; }
.oc-fallback { display:flex; align-items:center; justify-content:center; color:#fff;
  font-size:1.6rem; font-weight:600; }
.oc-card .nm { font-size:1.05rem; font-weight:600; }
.oc-card .src { font-size:.7rem; opacity:.65; }
.oc-card .ttl { font-size:.8rem; margin:2px 0 4px; }
.oc-card .tg { font-size:.75rem; opacity:.75; margin-top:4px; }
.oc-chip { font-size:.65rem; border:1px solid rgba(128,128,128,.4); border-radius:10px;
  padding:1px 8px; margin-right:4px; white-space:nowrap; }
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
    if icon_url:
        return f"<img class='oc-avatar' src='{_esc(icon_url)}' alt='{_esc(name)}'>"
    return (
        f"<div class='oc-avatar oc-fallback' style='background:{_esc(color)}'>"
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
    return (
        f"<div class='oc-card' style='--mc:{_esc(m.get('color', '#888'))}'>"
        + _avatar_html(m.get("name", "?"), m.get("color", "#888"), m.get("icon_url"))
        + f"<div><div class='nm'>{_esc(m.get('name', ''))}</div>{src}"
        + f"<div class='ttl'>{_esc(m.get('title', ''))}</div>"
        + f"<div>{chips}</div>"
        + f"<div class='tg'>{_esc(m.get('tagline', ''))}</div></div></div>"
    )


def page_org() -> None:
    st.header("組織")
    org = queries.load_org()

    st.subheader("組織図(00-system-design §3・14部門+開発部門)")
    st.markdown(_org_chart_html(), unsafe_allow_html=True)
    st.caption(
        "リスク管理部は PM を日次監視。独立監査部門は全部門を read-only で監査し、"
        "投資委員会へ直接報告する。"
    )

    st.subheader("メンバー(config/org.yaml が正)")
    rep = org.get("representative", {})
    rep_card = (
        "<div class='oc-card' style='--mc:#64748b'>"
        "<div class='oc-avatar oc-fallback' style='background:#64748b'>代</div>"
        "<div><div class='nm'>代表</div>"
        f"<div class='ttl'>{_esc(rep.get('note', 'ユーザー'))}</div>"
        "<div><span class='oc-chip'>人間</span>"
        "<span class='oc-chip'>投資委員会</span></div></div></div>"
    )
    cards = rep_card + "".join(_member_card_html(m) for m in org.get("members", []))
    st.markdown(_ORG_CSS + f"<div class='oc-members'>{cards}</div>", unsafe_allow_html=True)
    st.caption(
        "モデル階層は「まず非LLM → 軽量 → 中位 → Fable」の原則(CLAUDE.md)。"
        "アイコン未設定のメンバーはカラーの頭文字で代替表示(icon_url 設定タスクは別途)。"
    )


# ── 承認・通知(組織サイト化)──────────────────────────────────────────────────
def page_approvals(conn) -> None:
    st.header("承認・通知")

    st.subheader("承認・決定の履歴(governance.decisions)")
    decisions = queries.fetch_decisions(conn, limit=50)
    if not decisions:
        st.info("決定記録はまだない(Discord 承認 UI・みなし承認が書く)")
    else:
        df = _df(decisions)
        df.insert(0, "区分", df["decision"].map(lambda d: "みなし" if d == "deemed" else "明示"))
        st.caption(
            f"直近 {len(df)} 件(みなし {int((df['区分'] == 'みなし').sum())} / "
            f"明示 {int((df['区分'] == '明示').sum())})— "
            "みなし承認は通知と同時発効・代表はいつでも否認可(定款第3条 v0.4)"
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
    pending = [r for r in reminders if r["status"] == "pending"]
    st.metric("pending", len(pending), delta=None)
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
            "A-13 が四半期棚卸しの対象とする(定款第6条)。"
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
                lambda v: {
                    "schema(DB 制約)": "color: green",
                    "gate(実行時ゲート)": "color: green",
                    "ci(テスト・CI)": "color: #2c7be5",
                    "audit(監査ジョブ)": "color: orange",
                    "宣言のみ(執行点なし)": "color: red; font-weight: bold",
                }.get(v, ""),
                subset=["執行点"],
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("保護領域(定款第5条)")
    st.caption(
        "変更に承認記録(明示またはみなし)+ `Approved:` トレーラが必須。"
        "A-13 が git 履歴と突合する。"
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
        st.progress(
            done_ms / len(all_ms),
            text=f"全体進捗: {done_ms} / {len(all_ms)} マイルストーン完了",
        )
    for p in phases:
        ms = p.get("milestones", [])
        p_done = sum(1 for m in ms if m.get("status") == "done")
        status = _PHASE_LABELS.get(p.get("status"), p.get("status"))
        note = f" — {p['note']}" if p.get("note") else ""
        with st.container(border=True):
            st.markdown(f"**{p.get('name')}**({status}{note})")
            st.caption(p.get("summary", ""))
            if ms:
                st.progress(p_done / len(ms), text=f"{p_done} / {len(ms)}")
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
    """チャット吹き出しのアバター。ローカル SVG(Streamlit は表示可)→ 台帳の
    icon_url(リモート)→ 既定アイコンの順でフォールバックする。"""
    member = _role_member(role)
    if member is None:
        return None
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
            # 決定論チェック(05 §3): 独立役員の発言が無い議事録の決議には明示確認を
            # 求める。ブロックではなく摩擦であり、決議権は代表に残る(定款第3条)。
            confirm = st.checkbox(
                "独立役員の批判を経ていない議事録でも決議する(内容を理解した上で)"
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
            st.caption(f"決議 {res['seq']}: {res['title']}(#{res['resolution_id']}{ref})")


# ── エントリポイント ──────────────────────────────────────────────────────────
def main() -> None:
    st.sidebar.title("Ryza 運用ダッシュボード")
    st.sidebar.caption(
        "Cloud Run + IAP で公開(許可アカウントのみ。認証は IAP に全面委譲)+ローカル。"
        "役員室以外は読み取り専用。操作(Kill Switch 等)は Discord から。"
    )
    page = st.sidebar.radio(
        "ページ",
        [
            "概況",
            "組織",
            "承認・通知",
            "規則",
            "計画",
            "取込",
            "報道",
            "コスト",
            "市場観",
            "役員室",
            "開発ステータス",
        ],
    )
    if page == "開発ステータス":
        page_dev_status()
        return
    if page == "役員室":
        page_boardroom()  # 書込可の専用接続を自前で持つ(READ ONLY 接続は使わない)
        return
    if page == "組織":
        page_org()  # config/org.yaml のみ(DB 不要)
        return
    if page == "規則":
        page_rules()  # config/governance.yaml のみ(DB 不要)
        return
    try:
        conn = _conn()
    except Exception as exc:  # noqa: BLE001 - DB 停止時も UI は説明を出して生かす
        st.error(f"DB に接続できない: {exc}")
        st.caption("compose.yaml の PostgreSQL 起動と RYZA_DATABASE_URL を確認。")
        return
    {
        "概況": page_overview,
        "承認・通知": page_approvals,
        "計画": page_plan,
        "取込": page_ingest,
        "報道": page_press,
        "コスト": page_cost,
        "市場観": page_market_view,
    }[page](conn)


main()
