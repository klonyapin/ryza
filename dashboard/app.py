"""dashboard/app — Ryza 運用ダッシュボード(Streamlit・Issue #10)。

**ローカル専用・読み取り専用。** 公開ホスティングはしない(Cloud Run 公開版は
ユーザー指摘で撤去済み・2026-08-02)。書込・操作系 UI は置かない — Kill Switch 等の
操作は Discord Bot の管轄で、ここは閲覧のみ。

起動: ``.venv/bin/streamlit run dashboard/app.py``(README 参照)。
接続先: env ``RYZA_DATABASE_URL``(既定 postgresql://ryza:ryza@localhost:5432/ryza)。

DB アクセスは ``queries.py``(テスト対象)に分離し、本ファイルは表示だけを担う。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# streamlit run はスクリプトの親ディレクトリを sys.path に足すが、実行環境差への保険。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import queries  # noqa: E402

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
def page_market_view(conn) -> None:
    st.header("市場観(docs.market_view)")
    view = queries.fetch_current_market_view(conn)
    if view is None:
        st.info("市場観は未初期化")
    else:
        st.caption(f"view_id {view['view_id']} / 版時刻 {view['ts']} / run {view['run_id']}")
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("レジーム")
            st.json(view["regime"])
        with col2:
            st.subheader("注目リスク")
            st.json(view["key_risks"])
        if view["changes"]:
            st.subheader("前版からの差分")
            st.json(view["changes"])

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


# ── エントリポイント ──────────────────────────────────────────────────────────
def main() -> None:
    st.sidebar.title("Ryza 運用ダッシュボード")
    st.sidebar.caption("ローカル専用・読み取り専用。操作(Kill Switch 等)は Discord から。")
    page = st.sidebar.radio(
        "ページ", ["概況", "取込", "報道", "コスト", "市場観", "開発ステータス"]
    )
    if page == "開発ステータス":
        page_dev_status()
        return
    try:
        conn = _conn()
    except Exception as exc:  # noqa: BLE001 - DB 停止時も UI は説明を出して生かす
        st.error(f"DB に接続できない: {exc}")
        st.caption("compose.yaml の PostgreSQL 起動と RYZA_DATABASE_URL を確認。")
        return
    {
        "概況": page_overview,
        "取込": page_ingest,
        "報道": page_press,
        "コスト": page_cost,
        "市場観": page_market_view,
    }[page](conn)


main()
