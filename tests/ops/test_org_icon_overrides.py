"""アイコン上書き(migration 0020)の DB 層テスト。

代表指示 2026-08-03「キャラクターアイコンをダッシュボードから再設定できるように」。
現在値表 ``ops.org_icon_overrides`` と追記オンリーの履歴表
``ops.org_icon_override_log`` の対(0020 の方式 B)が、上書き・更新・削除の
どの経路でも履歴を残すことを検証する。テスト DB に対して実行し commit しない。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza import org
from ryza.db.conn import connect

_URL_A = "https://example.test/a.png"
_URL_B = "https://example.test/b.png"


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _log(conn, member_id: str) -> list[tuple[str, str | None, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT action, icon_url, actor FROM ops.org_icon_override_log
             WHERE member_id = %s ORDER BY id
            """,
            (member_id,),
        )
        return cur.fetchall()


# ── 保存・更新・削除 ──────────────────────────────────────────────────────────
def test_set_override_is_visible_to_loader(conn):
    org.set_icon_override(conn, "aya", _URL_A, "representative")
    assert org.icon_overrides(conn)["aya"] == _URL_A
    assert org.effective_members(conn)["aya"].icon_url == _URL_A


def test_update_overwrites_current_value_but_log_keeps_history(conn):
    """UPDATE で現在値は 1 行に保たれるが、履歴は両方残る(0020 の方式 B の要件)。"""
    org.set_icon_override(conn, "aya", _URL_A, "representative")
    org.set_icon_override(conn, "aya", _URL_B, "representative")
    assert org.icon_overrides(conn)["aya"] == _URL_B
    assert _log(conn, "aya") == [
        ("set", _URL_A, "representative"),
        ("set", _URL_B, "representative"),
    ]


def test_clear_restores_ledger_value_and_logs_reset(conn):
    org.set_icon_override(conn, "aya", _URL_A, "representative")
    assert org.clear_icon_override(conn, "aya", "representative") is True
    assert "aya" not in org.icon_overrides(conn)
    # 台帳(config/org.yaml)の初期値へ戻る。
    assert org.effective_members(conn)["aya"].icon_url == org.members()["aya"].icon_url
    assert _log(conn, "aya")[-1] == ("reset", None, "representative")


def test_clear_without_override_is_noop(conn):
    assert org.clear_icon_override(conn, "aya", "representative") is False
    assert _log(conn, "aya") == []


def test_set_rejects_member_id_absent_from_ledger(conn):
    """台帳に無い id の上書きは作らない(存在しないキャラの行を残さない)。"""
    with pytest.raises(KeyError):
        org.set_icon_override(conn, "ghost", _URL_A, "representative")
    assert org.icon_overrides(conn) == {}


# ── update_icon(検証 → 保存の結線)──────────────────────────────────────────
def test_update_icon_saves_after_validation(conn):
    org.update_icon(
        conn, "aya", _URL_A, "representative",
        opener=lambda url, method, timeout: "image/png",
    )
    assert org.icon_overrides(conn)["aya"] == _URL_A


def test_update_icon_does_not_write_when_validation_fails(conn):
    with pytest.raises(org.IconUrlError):
        org.update_icon(
            conn, "aya", "https://example.test/page", "representative",
            opener=lambda url, method, timeout: "text/html",
        )
    assert org.icon_overrides(conn) == {}
    assert _log(conn, "aya") == []


# ── スキーマ制約 ──────────────────────────────────────────────────────────────
def test_https_check_blocks_plain_http(conn):
    """アプリを迂回した書込でも http は入らない(最後の防壁)。"""
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO ops.org_icon_overrides (member_id, icon_url, updated_by)"
            " VALUES ('aya', 'http://x/a.png', 'representative')"
        )
    conn.rollback()


def test_log_action_and_url_must_agree(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO ops.org_icon_override_log (member_id, action, icon_url, actor)"
            " VALUES ('aya', 'reset', 'https://x/a.png', 'representative')"
        )
    conn.rollback()
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO ops.org_icon_override_log (member_id, action, icon_url, actor)"
            " VALUES ('aya', 'set', NULL, 'representative')"
        )
    conn.rollback()
