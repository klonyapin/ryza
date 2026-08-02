"""チャンネル ensure 計画・解決記録のテスト(§1 改訂: 4チャンネルをカテゴリ配下に ensure)。

plan_ensure は純関数(Discord API 非依存)。record_channel/resolve はライブ DB。
各テストは rollback で隔離する。
"""

from __future__ import annotations

import pytest

from ryza.bot import CHANNEL_NAMES, channels


def test_plan_all_create_when_empty():
    plans = channels.plan_ensure({})
    assert {p.logical for p in plans} == set(CHANNEL_NAMES)
    assert all(p.action == "create" and p.channel_id is None for p in plans)


def test_plan_reuses_existing_by_name():
    existing = {"報道": "111", "承認": "222"}
    by_logical = {p.logical: p for p in channels.plan_ensure(existing)}
    assert by_logical["press"].action == "reuse"
    assert by_logical["press"].channel_id == "111"
    assert by_logical["approval"].action == "reuse"
    # 未作成の運営/dev は create。
    assert by_logical["ops"].action == "create"
    assert by_logical["dev"].action == "create"


def test_record_and_resolve(conn):
    channels.record_channel(conn, "press", "報道", "555", "9000")
    assert channels.resolve(conn, "press") == "555"
    # 未解決の論理チャンネルは None。
    assert channels.resolve(conn, "dev") is None


def test_record_channel_upsert_follows_rename(conn):
    """手動リネーム追従: 同一 logical への再記録は channel_id を上書きする。"""
    channels.record_channel(conn, "ops", "運営", "100", "9000")
    channels.record_channel(conn, "ops", "運営-new", "200", "9000")
    assert channels.resolve(conn, "ops") == "200"
    with conn.cursor() as cur:
        cur.execute("SELECT channel_name FROM ops.discord_channels WHERE logical = 'ops'")
        assert cur.fetchone()[0] == "運営-new"


def test_record_unknown_logical_rejected(conn):
    with pytest.raises(ValueError):
        channels.record_channel(conn, "bogus", "x", "1", "9000")
