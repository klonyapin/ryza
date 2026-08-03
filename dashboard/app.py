"""dashboard/app — Ryza 運用ダッシュボード(Streamlit・Issue #10)。

**ローカル専用。役員室タブを除き読み取り専用。** 公開ホスティングはしない(Cloud Run
公開版はユーザー指摘で撤去済み・2026-08-02)。Kill Switch 等の操作系 UI は置かない
(Discord Bot の管轄)。唯一の例外が「役員室」(Issue #9・05-governance §5)で、
議事録・決議マーク・stances の**追記**だけを行う(発注・設定変更の経路は持たない)。

起動: ``.venv/bin/streamlit run dashboard/app.py``(README 参照)。
接続先: env ``RYZA_DATABASE_URL``(既定 postgresql://ryza:ryza@localhost:5432/ryza)。
役員室の LLM 呼び出しは Anthropic API キーが必要(env RYZA_ANTHROPIC_API_KEY /
ANTHROPIC_API_KEY、または Secret Manager — providers.load_api_key の既定に任せる)。

DB アクセスは ``queries.py``(読取)と ``ryza.governance.boardroom``(役員室の書込・
テスト対象)に分離し、本ファイルは表示だけを担う。
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

from ryza import org  # noqa: E402
from ryza.db.conn import connect  # noqa: E402
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


# ── 役員室(Issue #9・05-governance §5)────────────────────────────────────────
@st.cache_resource
def _boardroom_conn():
    """役員室専用の**書込可**接続(autocommit)。

    設計判断: ``queries.connect_readonly()`` は流用しない。既存ページの READ ONLY
    原則はセッションを read-only に固定する防御の第二層であり(queries.py)、それを
    緩めると全ページが書込可能になってしまう。書込はこの別接続だけに閉じることで、
    読取ページに誤って書込コードが紛れても従来どおり DB 側で拒否される。
    autocommit なのは実ジョブの Run と同じ流儀(即時永続化)。書込先(minutes /
    minute_resolutions / stances)は追記オンリーのため、途中失敗しても改竄は起きない。
    """
    return connect(autocommit=True)


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
    """役職の表示は「名前(役職)」(代表指示 2026-08-03)。台帳に無ければ従来ラベル。"""
    member = _role_member(role)
    return member.display_name if member else boardroom.BOARDROOM_ROLES.get(role, role)


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
    """1 発言を吹き出し表示する。役職側は名前(役職)+キャラクター色+アバター。"""
    if turn.speaker == "representative":
        with st.chat_message("user"):
            st.markdown(turn.text)
        return
    member = _role_member(turn.speaker)
    with st.chat_message("assistant", avatar=_role_avatar(turn.speaker)):
        if member is not None:
            st.markdown(
                f'<span style="color:{member.color};font-weight:600">'
                f"{member.display_name}</span>",
                unsafe_allow_html=True,
            )
        st.markdown(turn.text)


def page_boardroom() -> None:
    st.header("役員室")
    st.caption(
        "経営レベルの対話・審議の場(05-governance §5)。対話は判断材料であり、"
        "何も自動執行しない(不変原則1)。発効する決定は「決議」マークのみ。"
    )
    try:
        wconn = _boardroom_conn()
    except Exception as exc:  # noqa: BLE001 - DB 停止時も UI は説明を出して生かす
        st.error(f"DB に接続できない: {exc}")
        return

    role = st.selectbox(
        "役職", list(boardroom.BOARDROOM_ROLES),
        format_func=lambda r: boardroom.BOARDROOM_ROLES[r],
    )
    if st.session_state.get("br_role") != role:
        # 役職を切り替えたら会話をリセット(役職間で記憶・文脈を共有しない — 05 §6-2)。
        st.session_state["br_role"] = role
        st.session_state["br_turns"] = []
        st.session_state["br_minute_id"] = None
    # CIO/独立役員の設計階層は fable(05 §3)だが、コスト配慮で既定は mid。
    use_fable = st.toggle("fable で応答(既定は mid — コスト配慮。設計上の階層は fable)")
    tier = "fable" if use_fable else "mid"

    turns: list[boardroom.ChatTurn] = st.session_state["br_turns"]
    for turn in turns:
        avatar = "user" if turn.speaker == "representative" else "assistant"
        with st.chat_message(avatar):
            st.markdown(turn.text)

    role_label = boardroom.BOARDROOM_ROLES[role]
    text = st.chat_input(f"{role_label} への発言(あなたは代表として話す)")
    if text:
        turns.append(boardroom.ChatTurn("representative", text))
        with st.chat_message("user"):
            st.markdown(text)
        try:
            with st.spinner(f"{role_label} が応答中({tier})…"):
                # LLM 1 呼び出しごとに Run を開閉する(コスト記録の受け皿)。セッション
                # 単位の Run にしない理由: Streamlit にはセッション終了フックがなく、
                # ブラウザを閉じると 'running' 行が漏れ残るため。
                with run_ctx(
                    "dashboard.boardroom.chat", {"role": role, "tier": tier}, conn=wconn
                ) as r:
                    reply = boardroom.chat_reply(
                        _boardroom_llm(r, tier),
                        # 着任プロンプトは毎回組み立てる(直近の stances を反映 — 05 §2)。
                        onboarding_prompt=personas.assume_role(wconn, role),
                        turns=turns,
                        model=_llm_config().model_for(tier),
                        model_tier=tier,
                    )
        except Exception as exc:  # noqa: BLE001 - API 失敗時は発言を取り消して継続
            turns.pop()
            st.error(f"応答の生成に失敗: {exc}")
            return
        turns.append(boardroom.ChatTurn(role, reply))
        with st.chat_message("assistant"):
            st.markdown(reply)

    st.divider()
    if st.button("議事録として保存(主張・懸念も蓄積)", disabled=not turns):
        try:
            with st.spinner("議事録を保存し、主張・懸念を要約中…"):
                with run_ctx("dashboard.boardroom.save", {"role": role}, conn=wconn) as r:
                    saved = boardroom.save_office_chat_minute(
                        wconn, role=role, turns=turns, run_id=r.run_id
                    )
                    # 要約は mid 固定(応答トグルとは独立 — 要約に fable は不要)。
                    digest = boardroom.digest_stances(
                        _boardroom_llm(r, "mid"),
                        role=role,
                        transcript_md=saved.body_md,
                        model=_llm_config().model_for("mid"),
                        model_tier="mid",
                    )
                    stance_ids = boardroom.record_chat_stances(
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
        # 決議ボタンは代表のみ押せる建前(05 §5)。本ダッシュボードはローカル専用で
        # 公開ホスティングを持たない(冒頭 docstring)ため、操作者=代表とみなす。
        # resolved_by='representative' は 0013 の CHECK でも DB 側から強制される。
        with st.form("resolution_form", clear_on_submit=True):
            title = st.text_input("決議タイトル")
            body = st.text_area("決議本文(反対意見・却下理由も残す — 05 §6-3)")
            proposal_ref = st.text_input(
                "proposal_ref(承認事項なら governance.decisions と突合。任意)"
            )
            if st.form_submit_button("決議としてマーク(代表として)"):
                if not title.strip() or not body.strip():
                    st.warning("タイトルと本文は必須")
                else:
                    rid = boardroom.mark_resolution(
                        wconn, minute_id=minute_id, title=title.strip(),
                        resolution_md=body, proposal_ref=proposal_ref.strip() or None,
                    )
                    st.success(f"決議 #{rid} をマークした")
        for res in boardroom.fetch_resolutions(wconn, minute_id):
            ref = f" / proposal_ref: {res['proposal_ref']}" if res["proposal_ref"] else ""
            st.caption(f"決議 {res['seq']}: {res['title']}(#{res['resolution_id']}{ref})")


# ── エントリポイント ──────────────────────────────────────────────────────────
def main() -> None:
    st.sidebar.title("Ryza 運用ダッシュボード")
    st.sidebar.caption(
        "ローカル専用。役員室以外は読み取り専用。操作(Kill Switch 等)は Discord から。"
    )
    page = st.sidebar.radio(
        "ページ", ["概況", "取込", "報道", "コスト", "市場観", "役員室", "開発ステータス"]
    )
    if page == "開発ステータス":
        page_dev_status()
        return
    if page == "役員室":
        page_boardroom()  # 書込可の専用接続を自前で持つ(READ ONLY 接続は使わない)
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
