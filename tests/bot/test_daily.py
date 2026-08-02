"""日報骨格のテスト(§5「日報」: 当面は稼働状況のみ)。

build_status_report / enqueue_daily をライブ DB で検証する。各テストは rollback で隔離する。
"""

from __future__ import annotations

import datetime as dt

from ryza.bot import daily, killswitch
from ryza.bot.outbox import claim_pending

NOW = dt.datetime(2026, 8, 3, 9, 0, tzinfo=dt.UTC)


def test_status_report_shape_and_kill_switch(conn):
    report = daily.build_status_report(conn, NOW)
    assert report["title"].startswith("日報")
    names = {f["name"] for f in report["fields"]}
    assert {"Kill Switch", "未送キュー", "本日のジョブ実行"}.issubset(names)
    # 既定では Kill Switch は通常表示。
    ks = next(f["value"] for f in report["fields"] if f["name"] == "Kill Switch")
    assert "通常" in ks


def test_status_report_reflects_engaged_kill_switch(conn):
    killswitch.engage(conn, "1001", ["1001"], reason="test")
    report = daily.build_status_report(conn, NOW)
    ks = next(f["value"] for f in report["fields"] if f["name"] == "Kill Switch")
    assert "有効" in ks


def test_status_report_counts_pending_outbox(conn, run_id):
    from ryza.bot.outbox import enqueue

    before = int(
        next(
            f["value"]
            for f in daily.build_status_report(conn, NOW)["fields"]
            if f["name"] == "未送キュー"
        )
    )
    enqueue(conn, "ops", {"title": "x"}, run_id)
    after = int(
        next(
            f["value"]
            for f in daily.build_status_report(conn, NOW)["fields"]
            if f["name"] == "未送キュー"
        )
    )
    assert after == before + 1


def test_enqueue_daily_posts_to_ops(conn, run_id):
    oid = daily.enqueue_daily(conn, NOW, run_id)
    msg = next(m for m in claim_pending(conn) if m.id == oid)
    assert msg.channel == "ops"  # #運営 へ
    assert msg.embed["title"].startswith("日報")
