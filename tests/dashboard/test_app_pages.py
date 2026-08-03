"""dashboard/app.py のヘッドレス描画テスト(T-018)。

Streamlit の ``AppTest`` で全ページを実際に描画し、**例外ゼロ**を確認する。従来
「UI はテスト対象外」としていたが、可視化再設計でページ実装が ``viz.py`` ヘルパと
新規クエリに依存するようになり、描画時にしか出ない型・キー名の不整合(``metrics``
jsonb のキー、``st.progress`` の値域など)を CI で捕まえる必要が生じた。

**DB 隔離**: ``queries.connect_readonly`` / ``connect_boardroom`` をテストの
トランザクション内接続に差し替える(``st.cache_resource`` はテストごとにクリア)。
これによりアプリは commit された実データを見ず、投入したテストデータは rollback で
消える。**GitHub REST** は「計画」ページが叩くためスタブ化する(CI をネットワークに
依存させない)。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

pytest.importorskip("streamlit", reason="streamlit 未導入(.[dashboard] を入れると走る)")

import github_api  # noqa: E402
import queries  # noqa: E402
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

_APP = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"

#: サイドバーに出る全ページ(順序は app.main の radio と一致させること)。
ALL_PAGES = [
    "概況",
    "成績",
    "リスク",
    "ジョブ",
    "コスト",
    "取込",
    "承認・通知",
    "報道",
    "市場観",
    "計画",
    "組織",
    "規則",
    "役員室",
    "開発ステータス",
]

_NOW = datetime.now(UTC)


# ── テストデータ(すべて呼び出し側のトランザクション内。rollback で消える)──────
def _metrics(*, sufficient: bool) -> dict:
    """リスクエンジンの測定値スナップショット(ryza.risk.state.state_metrics 相当)。"""
    return {
        "drawdown": "0.25",
        "n_returns": 20 if sufficient else 3,
        "sufficient": sufficient,
        "ewma_vol_annual": 0.11,
        "es95_adopted": 0.012,
        "nav": "900000",
        "peak_nav": "1200000",
        "notes": ["価格欠測: 9999"],
    }


def _seed(conn, run, *, sufficient: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, reason, updated_by)
            VALUES ('normal', NULL, 'test:app')
            ON CONFLICT (singleton) DO UPDATE
                SET state = EXCLUDED.state, reason = EXCLUDED.reason,
                    updated_by = EXCLUDED.updated_by, updated_at = now()
            """
        )
        # NAV 系列: 100万 → 120万 → 90万(設定来ピーク比 -25% の水没を作る)。
        for i, nav in enumerate((1_000_000, 1_200_000, 900_000)):
            cur.execute(
                """
                INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
                VALUES ('DEMO_FUND', %s, %s, 'confirmed', '{}'::jsonb)
                ON CONFLICT (book_id, snap_date) DO UPDATE SET nav = EXCLUDED.nav
                """,
                (date(2026, 8, 1) + timedelta(days=i), nav),
            )
        cur.execute(
            """
            INSERT INTO risk.limits_state
                (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of, run_id)
            VALUES ('DEMO_FUND', true, false, false, false, %s, %s)
            ON CONFLICT (book_id) DO UPDATE
                SET dd_soft = EXCLUDED.dd_soft, as_of = EXCLUDED.as_of
            """,
            (_NOW, run.run_id),
        )
        cur.execute(
            """
            INSERT INTO risk.limits_state_events
                (book_id, event, dd_soft, dd_hard, vol_exceeded, es_exceeded,
                 metrics, actor, as_of, run_id)
            VALUES ('DEMO_FUND', 'engine_update', true, false, false, false,
                    %s, 'risk.daily', %s, %s)
            """,
            (Jsonb(_metrics(sufficient=sufficient)), _NOW, run.run_id),
        )
        cur.execute(
            """
            INSERT INTO meta.runs
                (job_name, code_version, started_at, finished_at, status, params, cost)
            VALUES ('jobs.daily', 'test', %s, %s, 'success', '{}'::jsonb, %s)
            """,
            (
                _NOW - timedelta(minutes=12),
                _NOW,
                Jsonb(
                    {
                        "total_tokens": 1500,
                        "total_cost_estimate": 0.9,
                        "by_tier": {"mid": {"calls": 2, "tokens": 1500, "cost_estimate": 0.9}},
                    }
                ),
            ),
        )
        cur.execute(
            """
            INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
            VALUES ('ops', %s, false, %s)
            """,
            (
                Jsonb(
                    {
                        "title": "日次サイクル 2026-08-03 07:00 JST",
                        "fields": [
                            {"name": "ingest", "value": "✅ ok=2", "inline": False},
                            {"name": "morning", "value": "⚠️ 失敗: RuntimeError", "inline": False},
                        ],
                    }
                ),
                run.run_id,
            ),
        )
        cur.execute(
            """
            INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
            VALUES ('approval', %s, false, %s)
            """,
            (Jsonb({"title": "承認依頼: T-018"}), run.run_id),
        )


def _app_factory(conn, monkeypatch):
    monkeypatch.setattr(queries, "connect_readonly", lambda: conn)
    monkeypatch.setattr(queries, "connect_boardroom", lambda: conn)
    for name in (
        "fetch_open_pulls",
        "fetch_open_issues",
        "fetch_merged_pulls",
        "fetch_closed_issues",
    ):
        monkeypatch.setattr(github_api, name, lambda: [])
    monkeypatch.setattr(github_api, "fetch_ci_state", lambda sha: "none")
    st.cache_data.clear()
    st.cache_resource.clear()

    def _open(page: str | None = None) -> AppTest:
        # cache_data(ttl=60) はページ間で持ち越さない — テストは毎回実データを見る。
        st.cache_data.clear()
        at = AppTest.from_file(str(_APP), default_timeout=120)
        at.run()
        assert not at.exception, f"初期描画で例外: {at.exception}"
        if page is not None:
            at.sidebar.radio[0].set_value(page).run()
            assert not at.exception, f"{page} の描画で例外: {at.exception}"
        return at

    return _open


@pytest.fixture
def app(conn, run, monkeypatch):
    """テストトランザクションに束縛した AppTest ファクトリ(観測数は十分)。"""
    _seed(conn, run, sufficient=True)
    yield _app_factory(conn, monkeypatch)
    st.cache_data.clear()
    st.cache_resource.clear()


@pytest.fixture
def app_insufficient(conn, run, monkeypatch):
    """観測不足(engine の sufficient=False)の系統。vol/ES 判定が無効な経路。"""
    _seed(conn, run, sufficient=False)
    yield _app_factory(conn, monkeypatch)
    st.cache_data.clear()
    st.cache_resource.clear()




def _texts(at: AppTest) -> str:
    """描画された markdown/caption/info/warning の全文(存在アサーション用)。"""
    parts = [el.value for el in at.markdown]
    parts += [el.value for el in at.info] + [el.value for el in at.warning]
    parts += [el.value for el in at.error] + [el.value for el in at.success]
    parts += [el.value for el in at.header] + [el.value for el in at.subheader]
    return "\n".join(str(p) for p in parts)


# ── 全ページのヘッドレス描画(例外ゼロ)─────────────────────────────────────────
@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_renders_without_exception(app, page):
    at = app(page)
    assert not at.exception


def test_sidebar_lists_every_page_in_declared_order(app):
    at = app()
    assert at.sidebar.radio[0].options == ALL_PAGES


def test_every_page_declares_the_question_it_answers(app):
    """Say 原則: 各ページ冒頭に「このページで答えられる問い」が 1 行ある。"""
    for page in ALL_PAGES:
        at = app(page)
        captions = [str(c.value) for c in at.caption]
        assert any(c.startswith("このページで答えられる問い:") for c in captions), page


# ── 概況: 6 ブロック固定の監視面 ───────────────────────────────────────────────
def test_overview_has_six_fixed_blocks(app):
    text = _texts(app("概況"))
    for block in (
        "① 取引状態",
        "② NAV",
        "③ DD 使用率",
        "④ 直近の日次サイクル",
        "⑤ 未処理の承認・通知",
        "⑥ LLM コスト予算消化",
    ):
        assert block in text, block


def test_overview_nav_carries_comparison_context(app):
    text = _texts(app("概況"))
    assert "前日比" in text and "設定来" in text


def test_overview_dd_bullet_shows_usage_against_limit(app):
    at = app("概況")
    # DD 0.25 / dd_hard 0.25 → 使用率 100%・リミット到達で赤。
    bars = [b.proto.text for b in at.get("progress")]
    assert any("使用率 100%" in b and b.startswith(":red[") for b in bars), bars


def test_overview_names_the_active_latches(app):
    """重要-4: DD 使用率だけでなく、いま発注を止めているラッチを概況で名指しする。"""
    text = _texts(app("概況"))
    assert "リスクフラグ" in text
    assert "作動中: dd_soft" in text  # fixture は dd_soft のみ true


def test_overview_cost_uses_calendar_month_and_shows_recorded_share(app):
    """中-9: 予算(月次)と分子(当月)の期間を揃え、コスト記録率を添える。"""
    text = _texts(app("概況"))
    assert "⑥ LLM コスト予算消化(当月)" in text
    assert "コスト記録のある実行" in text


def test_overview_has_no_raw_runs_table(app):
    """meta.runs の 30 行テーブルは「ジョブ」へ移した(概況は一画面に収める)。"""
    assert len(app("概況").dataframe) == 0
    assert len(app("ジョブ").dataframe) > 0


# ── 成績 ──────────────────────────────────────────────────────────────────────
def test_performance_shows_nav_line_and_underwater(app):
    at = app("成績")
    text = _texts(at)
    assert "NAV 推移" in text
    assert "アンダーウォーター(設定来ピーク比の下落率)" in text
    # ライン(NAV)の直下にエリア(underwater)。どちらも vega-lite で出るため
    # spec の mark 種別で見分ける。
    marks = [c.proto.spec for c in at.get("vega_lite_chart")]
    assert len(marks) == 2
    assert '"line"' in marks[0]
    assert '"area"' in marks[1]


def test_performance_declares_missing_benchmark_instead_of_faking_it(app):
    """無い比較(等配分 BH 対照)を推定値で埋めず、明示行として出す。"""
    frame = app("成績").dataframe[0].value
    column = list(frame["対照(等配分 buy-and-hold)"])
    assert set(column[:3]) == {"未実装"}
    assert column[-1] == "対照系列は未実装(T-019 候補)"


def test_performance_period_return_table_has_all_periods(app):
    frame = app("成績").dataframe[0].value
    assert list(frame["期間"])[:3] == ["1W(7日)", "1M(30日)", "設定来"]


def test_performance_table_declares_window_base_day(app):
    """重大-1/2: 起点日を併記し、窓を満たさない期間は値を出さない。

    fixture の NAV は 8/1〜8/3 の 3 日分。1W(7日)・1M(30日)は cutoff 以前の
    スナップショットが無いため「期間未充足」で値なし、設定来のみ起点 8/1 で有効。
    """
    frame = app("成績").dataframe[0].value
    rows = frame.set_index("期間")
    assert rows.loc["1W(7日)", "リターン"] == "—"
    assert rows.loc["1W(7日)", "起点日"] == "—"
    assert "期間未充足" in rows.loc["1M(30日)", "注記"]
    assert rows.loc["設定来", "起点日"] == "2026-08-01"
    assert rows.loc["設定来", "リターン"].startswith("-")  # 100万 → 90万


def test_performance_flags_external_flow_days_under_the_chart(app):
    """重要-6: 出資・払戻の日を underwater 図の直下で名指しする。

    フローは fixture ではなくマイグレーション由来(0006 の初期出資 8/2・0011 の
    増資 8/3)。NAV スナップショットの日付をそれに合わせてある。
    """
    captions = [str(c.value) for c in app("成績").caption]
    flow_note = next((c for c in captions if "外部フロー発生日" in c), None)
    assert flow_note is not None, captions
    assert "2026-08-02 出資" in flow_note and "2026-08-03 出資" in flow_note
    assert "損益ではない" in flow_note


# ── リスク ────────────────────────────────────────────────────────────────────
def test_risk_lists_bullets_sorted_by_usage(app):
    at = app("リスク")
    usages = [b.proto.value for b in at.get("progress")]
    assert len(usages) == 4  # dd_soft / dd_hard / 実現ボラ / ES95
    assert usages == sorted(usages, reverse=True)


def test_risk_shows_latch_state_as_text(app):
    text = _texts(app("リスク"))
    assert "DD ソフト(新規建て枠半減)" in text and "作動中" in text
    assert "DD ハード(全新規発注停止・復帰は委員会のみ)" in text and "未作動" in text


def test_risk_bullets_carry_limits_from_ips(app):
    bars = [b.proto.text for b in app("リスク").get("progress")]
    joined = "\n".join(bars)
    assert "上限 25.0%" in joined  # dd_hard_limit
    assert "上限 15.0%" in joined  # dd_soft_limit / realized_vol_limit
    assert "上限 3.0%" in joined  # daily_es95_nav_max


def test_risk_vol_and_es_are_unknown_when_observations_insufficient(app_insufficient):
    """重大-3: エンジンが判定を保留している間、vol/ES に赤 breach を出さない。

    ラッチが「未作動」なのに使用率が超過を示す、同一画面での矛盾を潰す。
    """
    bars = [b.proto.text for b in app_insufficient("リスク").get("progress")]
    unknown = [b for b in bars if b.startswith(":gray[")]
    assert len(unknown) == 2, bars  # 実現ボラ / ES95
    assert all("観測不足で判定無効(n=3/20)" in b for b in unknown)
    assert not any("実現ボラ" in b and b.startswith(":red[") for b in bars)
    # DD は 1 日目から有効なので観測不足でも出す。
    assert any("DD(対 dd_hard)" in b and b.startswith(":red[") for b in bars)


def test_risk_unknown_bullets_are_listed_first(app_insufficient):
    """低-12: 測れていないリミットは「安全」ではないので最下段に沈めない。"""
    bars = [b.proto.text for b in app_insufficient("リスク").get("progress")]
    assert bars[0].startswith(":gray[") and bars[1].startswith(":gray[")


def test_risk_vol_and_es_are_measured_when_sufficient(app):
    bars = [b.proto.text for b in app("リスク").get("progress")]
    assert not any(b.startswith(":gray[") for b in bars)
    assert any("実現ボラ(EWMA 年率)" in b and "11.0%" in b for b in bars), bars


# ── コスト: 累計 vanity を出さず比率で見せる ────────────────────────────────────
def test_cost_page_shows_budget_ratio_and_per_run(app):
    at = app("コスト")
    text = _texts(at)
    assert "月次予算の消化(当月・" in text  # 暦月起点(中-9)
    assert "1 実行あたり" in text
    assert "トークン合計" not in text  # 累計 vanity は廃止
    assert len(at.get("progress")) == 1


# ── 禁止記法が構造的に混入しないこと(中-8)──────────────────────────────────
def test_app_source_has_no_raw_progress_or_json():
    """ページ実装は表示形を viz ヘルパ経由でのみ作る(CI の grep と同じ規約)。"""
    source = _APP.read_text(encoding="utf-8")
    assert not re.search(r"(^|[^.\w])st\.(progress|json)\(", source)


def test_plan_page_uses_helper_progress_with_denominator(app):
    """生 st.progress を置き換えた進捗バーが分母つきで出ている。"""
    bars = [b.proto.text for b in app("計画").get("progress")]
    assert bars, "ロードマップの進捗バーが無い"
    assert all("/" in b for b in bars)
    assert any("全体進捗(マイルストーン完了)" in b for b in bars)


# ── 承認・通知 ────────────────────────────────────────────────────────────────
def test_approvals_summary_row_counts_and_oldest_age(app):
    text = _texts(app("承認・通知"))
    assert "未配送の通知" in text
    assert "最古" in text
