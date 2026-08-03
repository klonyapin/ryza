"""dashboard/queries.py のテスト(Issue #10)。

クエリ関数(読み取り専用 DB 層)をテスト専用 DB で検証する。Streamlit UI
(app.py)自体はテスト対象外。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
import queries
import viz
from psycopg.types.json import Jsonb

from ryza.ingest.freshness import FreshnessSLA
from ryza.ledger.posting import post_entry
from ryza.risk.daily import load_nav_series
from ryza.risk.engine import book_returns

NOW = datetime.now(UTC)


# ── 挿入ヘルパー ──────────────────────────────────────────────────────────────
def _insert_document(conn, run, *, source_name: str, title: str, as_of: datetime) -> int:
    digest = hashlib.sha256(f"{source_name}:{title}".encode()).digest()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.documents
                (source_type, source_name, title, as_of, content_hash, run_id)
            VALUES ('news', %s, %s, %s, %s, %s)
            RETURNING doc_id
            """,
            (source_name, title, as_of, digest, run.run_id),
        )
        return cur.fetchone()[0]


def _insert_outbox(
    conn, run, *, channel: str, title: str, fields: list | None = None, urgent: bool = False
) -> int:
    embed = {"title": title, "color": 0x2B6CB0, "fields": fields or []}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (channel, Jsonb(embed), urgent, run.run_id),
        )
        return cur.fetchone()[0]


# ── 概況 ──────────────────────────────────────────────────────────────────────
def test_fetch_trading_state(conn, run):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, reason, updated_by)
            VALUES ('frozen', 'テスト凍結', 'test:dashboard')
            ON CONFLICT (singleton) DO UPDATE
                SET state = EXCLUDED.state, reason = EXCLUDED.reason,
                    updated_by = EXCLUDED.updated_by, updated_at = now()
            """
        )
    state = queries.fetch_trading_state(conn)
    assert state is not None
    assert state["state"] == "frozen"
    assert state["reason"] == "テスト凍結"
    assert state["updated_by"] == "test:dashboard"


def test_fetch_recent_runs_with_cost(conn, run):
    run.add_cost("mid", tokens=1000, cost_estimate=0.6)
    run.add_cost("mid", tokens=500, cost_estimate=0.3)
    rows = queries.fetch_recent_runs(conn, limit=50)
    mine = next(r for r in rows if r["run_id"] == run.run_id)
    assert mine["job_name"] == "test.dashboard"
    assert mine["status"] == "running"
    assert mine["total_tokens"] == 1500
    assert float(mine["total_cost_estimate"]) == pytest.approx(0.9)
    # 並びは run_id 降順。
    ids = [r["run_id"] for r in rows]
    assert ids == sorted(ids, reverse=True)


def test_fetch_latest_daily_summary(conn, run):
    assert queries.fetch_latest_daily_summary(conn) is None
    stage_fields = [
        {"name": "ingest", "value": "OK ok=2 skipped=7 failed=0", "inline": False},
        {"name": "morning", "value": "OK outbox_id=12", "inline": False},
    ]
    daily_id = _insert_outbox(
        conn, run, channel="ops", title="日次サイクル 2026-08-03 07:00 JST",
        fields=stage_fields,
    )
    # 後から別の ops 通知(鮮度警告)が入っても、日次サイクルの最新を返す。
    _insert_outbox(conn, run, channel="ops", title="鮮度 SLA 違反: TDnet", urgent=True)

    summary = queries.fetch_latest_daily_summary(conn)
    assert summary is not None
    assert summary["id"] == daily_id
    assert summary["sent_at"] is None
    names = [f["name"] for f in summary["embed_json"]["fields"]]
    assert names == ["ingest", "morning"]


# ── 取込 ──────────────────────────────────────────────────────────────────────
_TDNET_SLA = FreshnessSLA("TDnet 適時開示", "documents", "TDnet", timedelta(minutes=30))


def test_fetch_freshness_ok_stale_no_data(conn, run):
    _insert_document(conn, run, source_name="TDnet", title="開示A", as_of=NOW)
    slas = [_TDNET_SLA, FreshnessSLA("EDINET 開示", "documents", "EDINET", timedelta(hours=26))]

    rows = queries.fetch_freshness(conn, slas=slas, now=NOW)
    by_label = {r["label"]: r for r in rows}
    assert by_label["TDnet 適時開示"]["status"] == "ok"
    assert by_label["TDnet 適時開示"]["last_as_of"] is not None
    assert by_label["EDINET 開示"]["status"] == "no_data"
    assert by_label["EDINET 開示"]["last_as_of"] is None

    # 2時間後を「現在」とすると 30 分 SLA の TDnet は stale。
    stale = queries.fetch_freshness(conn, slas=[_TDNET_SLA], now=NOW + timedelta(hours=2))
    assert stale[0]["status"] == "stale"
    assert stale[0]["age_hours"] == pytest.approx(2.0, abs=0.1)


def test_fetch_ingest_daily_counts(conn, run):
    _insert_document(conn, run, source_name="TDnet", title="開示B", as_of=NOW)
    _insert_document(conn, run, source_name="EDINET", title="有報", as_of=NOW)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.bars
                (instrument_id, ts, timeframe, close, source, as_of, run_id)
            VALUES (901, %s, '1d', 100.0, 'jquants', %s, %s)
            """,
            (NOW, NOW, run.run_id),
        )
        cur.execute(
            """
            INSERT INTO market.indicators (series_code, ts, value, as_of, run_id)
            VALUES ('TEST_SERIES', %s, 1.5, %s, %s)
            """,
            (NOW, NOW, run.run_id),
        )
    rows = queries.fetch_ingest_daily_counts(conn, days=7)
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["table"]] = totals.get(r["table"], 0) + r["count"]
    assert totals["docs.documents"] == 2
    assert totals["market.bars"] == 1
    assert totals["market.indicators"] == 1


# ── 報道 ──────────────────────────────────────────────────────────────────────
def test_fetch_recent_outbox_filter_and_order(conn, run):
    press_id = _insert_outbox(conn, run, channel="press", title="Ryza 朝刊")
    ops_id = _insert_outbox(conn, run, channel="ops", title="日次サイクル")

    press_only = queries.fetch_recent_outbox(conn, channel="press", limit=10)
    assert [r["id"] for r in press_only] == [press_id]
    assert press_only[0]["embed_json"]["title"] == "Ryza 朝刊"

    both = queries.fetch_recent_outbox(conn, channel=None, limit=10)
    assert [r["id"] for r in both] == [ops_id, press_id]  # id 降順


# ── コスト ────────────────────────────────────────────────────────────────────
def test_fetch_cost_daily_by_dept_and_tier(conn, run):
    run.add_cost("mid", tokens=1000, cost_estimate=0.6)
    run.add_cost("mid", tokens=1000, cost_estimate=0.6)
    run.add_cost("light", tokens=400, cost_estimate=0.02)

    rows = [r for r in queries.fetch_cost_daily(conn, days=7) if r["dept"] == "test"]
    by_tier = {r["tier"]: r for r in rows}
    assert by_tier["mid"]["tokens"] == 2000
    assert by_tier["mid"]["calls"] == 2
    assert float(by_tier["mid"]["cost_estimate"]) == pytest.approx(1.2)
    assert by_tier["light"]["tokens"] == 400
    assert by_tier["light"]["calls"] == 1


# ── 市場観 ────────────────────────────────────────────────────────────────────
def test_fetch_market_view_and_snapshots(conn, run):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.market_view (ts, regime, key_risks, changes, basis_refs, run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING view_id
            """,
            (NOW, Jsonb({"jp_equity": "risk_on"}), Jsonb([{"risk": "円急伸", "prob": 0.3}]),
             Jsonb({"jp_equity": {"from": "neutral", "to": "risk_on"}}), [], run.run_id),
        )
        view_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO docs.market_view_snapshots
                (snapshot_date, view_id, ts, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (NOW.date(), view_id, NOW, NOW, run.run_id),
        )

    view = queries.fetch_current_market_view(conn)
    assert view is not None
    assert view["view_id"] == view_id
    assert view["regime"] == {"jp_equity": "risk_on"}
    assert view["key_risks"][0]["risk"] == "円急伸"

    snaps = queries.fetch_market_view_snapshots(conn, limit=5)
    assert any(s["view_id"] == view_id for s in snaps)


# ── 成績・リスク・未処理通知(T-018)──────────────────────────────────────────
def _insert_nav(conn, *, day: date, nav: int, book_id: str = "DEMO_FUND") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES (%s, %s, %s, 'confirmed', '{}'::jsonb)
            ON CONFLICT (book_id, snap_date) DO UPDATE SET nav = EXCLUDED.nav
            """,
            (book_id, day, nav),
        )


def test_fetch_nav_series_joins_external_flows(conn, run):
    """出資仕訳(capital)が当日の net_flow として NAV 系列に付く。

    これを引かないと増資日のリターンが跳ねる(TWR にならない)。集計式は
    ryza.risk.daily.load_nav_series と同一。
    """
    _insert_nav(conn, day=date(2026, 7, 1), nav=1_000_000)
    _insert_nav(conn, day=date(2026, 7, 2), nav=1_500_000)
    post_entry(
        conn,
        book_id="DEMO_FUND",
        entry_date=date(2026, 7, 2),
        description="追加出資",
        lines=[
            {"account_id": "cash", "debit": 500_000, "currency": "JPY"},
            {"account_id": "capital", "credit": 500_000, "currency": "JPY"},
        ],
        evidence={
            "kind": "invoice", "payload_ref": "test://capital",
            "sha256": hashlib.sha256(b"capital").digest(),
            "source": "test", "retrieved_at": NOW,
        },
        run_id=run.run_id,
    )
    series = queries.fetch_nav_series(conn)
    assert [r["day"] for r in series] == [date(2026, 7, 1), date(2026, 7, 2)]
    assert float(series[0]["net_flow"]) == 0.0
    assert float(series[1]["net_flow"]) == 500_000.0
    # 出資を除けばリターンは 0(NAV は 100万 → 150万 だが中身は増えていない)。
    prev, cur = series
    ret = (float(cur["nav"]) - float(cur["net_flow"]) - float(prev["nav"])) / float(prev["nav"])
    assert ret == pytest.approx(0.0)


def _post_capital(conn, run, *, day: date, amount: int, book_id: str = "DEMO_FUND") -> None:
    """出資(+)/払戻(−)の仕訳を 1 本入れる。"""
    cash, capital = (
        ({"account_id": "cash", "debit": amount}, {"account_id": "capital", "credit": amount})
        if amount > 0
        else (
            {"account_id": "cash", "credit": -amount},
            {"account_id": "capital", "debit": -amount},
        )
    )
    post_entry(
        conn,
        book_id=book_id,
        entry_date=day,
        description="テスト外部フロー",
        lines=[{**cash, "currency": "JPY"}, {**capital, "currency": "JPY"}],
        evidence={
            "kind": "invoice", "payload_ref": f"test://flow/{day}/{amount}",
            "sha256": hashlib.sha256(f"{day}{amount}".encode()).digest(),
            "source": "test", "retrieved_at": NOW,
        },
        run_id=run.run_id,
    )


def test_fetch_nav_series_rolls_forward_holiday_flow(conn, run):
    """スナップショットの無い日(休日)の出資は次の測定日に寄る(独立審査 重要-5)。

    修正前は entry_date 完全一致で結合していたため 1/3 の出資が落ち、1/2 → 1/5 の
    リターンが +50% と表示されていた(実際の運用損益は 0%)。
    """
    _insert_nav(conn, day=date(2030, 1, 2), nav=1_000_000)
    _insert_nav(conn, day=date(2030, 1, 5), nav=1_500_000)
    _post_capital(conn, run, day=date(2030, 1, 3), amount=500_000)
    series = [r for r in queries.fetch_nav_series(conn) if r["day"].year == 2030]
    assert [r["day"] for r in series] == [date(2030, 1, 2), date(2030, 1, 5)]
    assert float(series[1]["net_flow"]) == 500_000.0
    prev, cur = series
    ret = (float(cur["nav"]) - float(cur["net_flow"]) - float(prev["nav"])) / float(prev["nav"])
    assert ret == pytest.approx(0.0)


def test_fetch_pending_flows_after_last_snapshot(conn, run):
    """系列最終日より後のフローは NAV 系列に載らないので別枠で返す(黙って落とさない)。"""
    _insert_nav(conn, day=date(2030, 1, 2), nav=1_000_000)
    before_pending = queries.fetch_pending_flows(conn)
    before_series = queries.fetch_nav_series(conn)
    _post_capital(conn, run, day=date(2030, 1, 6), amount=500_000)
    added = [r for r in queries.fetch_pending_flows(conn) if r not in before_pending]
    assert added == [{"day": date(2030, 1, 6), "amount": Decimal(500_000)}]
    # 系列側は変わらない(未反映フローは点にできない)。
    assert queries.fetch_nav_series(conn) == before_series


def test_nav_series_matches_risk_engine(conn, run):
    """ダッシュボードとリスクエンジンの日次リターンが同一 fixture で一致する。

    重要-5 は「同じフロー突合の定義を 2 箇所に持っていた」ことが根本原因だった。
    定義は ``ryza.risk.navflow`` に一本化してあり、この test がその一致を固定する。
    """
    for day, nav in ((2, 1_000_000), (5, 1_400_000), (7, 1_540_000), (8, 1_940_000)):
        _insert_nav(conn, day=date(2030, 1, day), nav=nav)
    _post_capital(conn, run, day=date(2030, 1, 3), amount=500_000)  # 休日(snapshot なし)
    _post_capital(conn, run, day=date(2030, 1, 4), amount=-100_000)  # 同じ点に寄る払戻
    _post_capital(conn, run, day=date(2030, 1, 8), amount=400_000)  # 測定日当日

    rows = queries.fetch_nav_series(conn)
    points = load_nav_series(conn, "DEMO_FUND").points
    assert [r["day"] for r in rows] == [p.day for p in points]
    assert [Decimal(str(r["net_flow"])) for r in rows] == [p.net_flow for p in points]
    dash = [r for _, r in viz.flow_adjusted_returns(rows)]
    assert dash == pytest.approx(book_returns(points))
    # 期待値: 1/5 は純増 40 万で運用損益 0%、1/7 は +10%、1/8 は 40 万出資で 0%。
    assert dash == pytest.approx([0.0, 0.1, 0.0])


def test_fetch_nav_series_filters_by_book(conn, run):
    _insert_nav(conn, day=date(2026, 7, 1), nav=1_000_000, book_id="DEMO_FUND")
    _insert_nav(conn, day=date(2026, 7, 1), nav=7_777, book_id="OPS")
    assert [float(r["nav"]) for r in queries.fetch_nav_series(conn)] == [1_000_000.0]


def test_fetch_limits_state_and_latest_metrics(conn, run):
    assert queries.fetch_latest_risk_metrics(conn) is None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk.limits_state
                (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of, run_id)
            VALUES ('DEMO_FUND', true, false, false, false, %s, %s)
            ON CONFLICT (book_id) DO UPDATE SET dd_soft = EXCLUDED.dd_soft
            """,
            (NOW, run.run_id),
        )
        for dd in ("0.10", "0.18"):
            cur.execute(
                """
                INSERT INTO risk.limits_state_events
                    (book_id, event, dd_soft, dd_hard, vol_exceeded, es_exceeded,
                     metrics, actor, as_of, run_id)
                VALUES ('DEMO_FUND', 'engine_update', true, false, false, false,
                        %s, 'risk.daily', %s, %s)
                """,
                (Jsonb({"drawdown": dd}), NOW, run.run_id),
            )
    state = next(s for s in queries.fetch_limits_state(conn) if s["book_id"] == "DEMO_FUND")
    assert state["dd_soft"] is True and state["dd_hard"] is False
    latest = queries.fetch_latest_risk_metrics(conn)
    assert latest is not None
    assert latest["metrics"]["drawdown"] == "0.18"  # 最新イベントを返す


def test_fetch_outbox_pending_counts_and_oldest(conn, run):
    assert queries.fetch_outbox_pending(conn) == []
    _insert_outbox(conn, run, channel="approval", title="承認1")
    _insert_outbox(conn, run, channel="approval", title="承認2")
    sent = _insert_outbox(conn, run, channel="ops", title="配送済み")
    with conn.cursor() as cur:
        cur.execute("UPDATE press.outbox SET sent_at = now() WHERE id = %s", (sent,))
    rows = queries.fetch_outbox_pending(conn)
    assert [(r["channel"], int(r["pending"])) for r in rows] == [("approval", 2)]
    assert float(rows[0]["oldest_age_hours"]) >= 0


def test_fetch_latest_daily_run_reports_duration(conn, run):
    assert queries.fetch_latest_daily_run(conn) is None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.runs
                (job_name, code_version, started_at, finished_at, status, params)
            VALUES ('jobs.daily', 'test', %s, %s, 'success', '{}'::jsonb)
            """,
            (NOW - timedelta(minutes=10), NOW),
        )
    latest = queries.fetch_latest_daily_run(conn)
    assert latest is not None
    assert latest["status"] == "success"
    assert float(latest["duration_seconds"]) == pytest.approx(600, abs=1)


def test_fetch_cost_summary_returns_ratio_inputs(conn, run):
    """比率(1 実行あたり)の分母になる実行数を返し、累計トークンは返さない。"""
    run.add_cost("mid", tokens=1000, cost_estimate=0.6)
    summary = queries.fetch_cost_summary(conn)
    assert int(summary["cost_runs"]) >= 1
    assert int(summary["all_runs"]) >= int(summary["cost_runs"])
    assert float(summary["total_cost"]) >= 0.6
    assert "tokens" not in summary


def test_load_llm_budget_and_ips_limits_from_repo_config():
    budget = queries.load_llm_budget()
    assert float(budget["monthly_jpy"]) > 0  # config/llm.yaml の既定値
    limits = queries.load_ips_limits()
    assert limits["dd_hard_limit"] == 0.25
    assert limits["dd_soft_limit"] < limits["dd_hard_limit"]


def test_load_llm_budget_absent_key_returns_empty(tmp_path):
    path = tmp_path / "llm.yaml"
    path.write_text("version: '1'\n", encoding="utf-8")
    assert queries.load_llm_budget(path) == {}


# ── 開発ステータス(site/data.js)───────────────────────────────────────────────
def test_load_site_status_parses_data_js(tmp_path):
    data = {"generated_at": "2026-08-03 12:00 JST", "phase": "実装フェーズ",
            "milestones": [], "issues": [], "commits": []}
    path = tmp_path / "data.js"
    path.write_text("window.RYZA_DATA = " + json.dumps(data) + ";\n", encoding="utf-8")
    assert queries.load_site_status(path) == data


def test_load_site_status_missing_file(tmp_path):
    assert queries.load_site_status(tmp_path / "nai.js") is None


def test_load_site_status_real_file():
    data = queries.load_site_status()
    assert data is not None and "milestones" in data  # リポジトリ同梱の site/data.js


# ── 読み取り専用の強制 ─────────────────────────────────────────────────────────
def test_connect_readonly_rejects_writes(migrated_db):
    conn = queries.connect_readonly()
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(
                "INSERT INTO ops.flags (name, enabled, updated_by) "
                "VALUES ('dashboard_test', false, 'test')"
            )
    finally:
        conn.close()
