"""日報骨格(§5「日報」)。

会計・リスクの確定値を 18:00 JST に ``#運営``(logical=ops)へ投稿する。データが揃うまでは
**稼働状況のみ**(Bot 稼働・未送キュー・当日ジョブ実行・Kill Switch 状態・検証期限到来の予兆件数)。

会計/リスクの確定値配線は T-002 以降の会計エンジン・データ層に依存するため本タスクでは骨格に留め、
``build_status_report`` が返す embed に「稼働状況」セクションのみを載せる。純ロジック(DB のみ)。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from ryza.bot import COLOR_NORMAL, DISCLAIMER
from ryza.bot.killswitch import is_engaged
from ryza.bot.outbox import enqueue

JST = ZoneInfo("Asia/Tokyo")


def _operational_status(conn: psycopg.Connection, now: datetime) -> dict[str, int | bool]:
    """稼働状況の集計値を返す。"""
    today = now.astimezone(JST).date()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM press.outbox WHERE sent_at IS NULL")
        pending = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM meta.runs WHERE started_at::date = %s",
            (today,),
        )
        runs_today = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM meta.runs WHERE status = 'failed' AND started_at::date = %s",
            (today,),
        )
        failed_today = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM press.predictions WHERE outcome = 'pending' AND verify_by <= %s",
            (now,),
        )
        predictions_due = cur.fetchone()[0]
    return {
        "pending_outbox": pending,
        "runs_today": runs_today,
        "failed_today": failed_today,
        "predictions_due": predictions_due,
        "kill_switch": is_engaged(conn),
    }


def build_status_report(conn: psycopg.Connection, now: datetime) -> dict:
    """日報 embed(稼働状況のみ)を組み立てる。"""
    st = _operational_status(conn, now)
    jst_str = now.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    ks = "⛔ 有効(発注停止中)" if st["kill_switch"] else "✅ 通常"
    return {
        "title": f"日報 {jst_str}",
        "description": "会計・リスクの確定値は未配線のため、当面は稼働状況のみを報告する。",
        "color": COLOR_NORMAL,
        "fields": [
            {"name": "Kill Switch", "value": ks, "inline": True},
            {"name": "未送キュー", "value": str(st["pending_outbox"]), "inline": True},
            {"name": "本日のジョブ実行", "value": str(st["runs_today"]), "inline": True},
            {"name": "本日の失敗ジョブ", "value": str(st["failed_today"]), "inline": True},
            {"name": "検証期限到来の予兆", "value": str(st["predictions_due"]), "inline": True},
        ],
        "footer": {"text": DISCLAIMER},
    }


def enqueue_daily(conn: psycopg.Connection, now: datetime, run_id: int) -> int:
    """日報を ``press.outbox``(channel=ops → #運営)に投入し outbox id を返す。"""
    embed = build_status_report(conn, now)
    return enqueue(conn, "ops", embed, run_id)
