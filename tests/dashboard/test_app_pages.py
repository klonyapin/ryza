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
def _seed(conn, run) -> None:
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
            (
                Jsonb(
                    {
                        "drawdown": "0.25",
                        "ewma_vol_annual": 0.11,
                        "es95_adopted": 0.012,
                        "nav": "900000",
                        "peak_nav": "1200000",
                        "notes": ["価格欠測: 9999"],
                    }
                ),
                _NOW,
                run.run_id,
            ),
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


@pytest.fixture
def app(conn, run, monkeypatch):
    """テストトランザクションに束縛した AppTest ファクトリ。"""
    _seed(conn, run)
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
        at = AppTest.from_file(str(_APP), default_timeout=120)
        at.run()
        assert not at.exception, f"初期描画で例外: {at.exception}"
        if page is not None:
            at.sidebar.radio[0].set_value(page).run()
            assert not at.exception, f"{page} の描画で例外: {at.exception}"
        return at

    yield _open
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
    assert list(frame["期間"])[:3] == ["1W", "1M", "設定来"]


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


# ── コスト: 累計 vanity を出さず比率で見せる ────────────────────────────────────
def test_cost_page_shows_budget_ratio_and_per_run(app):
    at = app("コスト")
    text = _texts(at)
    assert "月次予算の消化(直近30日)" in text
    assert "1 実行あたり" in text
    assert "トークン合計" not in text  # 累計 vanity は廃止
    assert len(at.get("progress")) == 1


# ── 承認・通知 ────────────────────────────────────────────────────────────────
def test_approvals_summary_row_counts_and_oldest_age(app):
    text = _texts(app("承認・通知"))
    assert "未配送の通知" in text
    assert "最古" in text
