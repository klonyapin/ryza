"""dashboard/queries.py のテスト(Issue #10)。

クエリ関数(読み取り専用 DB 層)をテスト専用 DB で検証する。Streamlit UI
(app.py)自体はテスト対象外。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import queries
from psycopg.types.json import Jsonb

from ryza.ingest.freshness import FreshnessSLA

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
