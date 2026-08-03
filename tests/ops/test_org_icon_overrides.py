"""アイコン上書き(migration 0020)の DB 層テスト。

代表指示 2026-08-03「キャラクターアイコンをダッシュボードから再設定できるように」。
現在値表 ``ops.org_icon_overrides`` と追記オンリーの履歴表
``ops.org_icon_override_log`` の対(0020 の方式 B)が、上書き・更新・削除の
どの経路でも履歴を残すことを検証する。テスト DB に対して実行し commit しない。
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from ryza import org
from ryza.db.conn import connect

_URL_A = "https://example.test/a.png"
_URL_B = "https://example.test/b.png"


@pytest.fixture
def conn(migrated_db):
    """rollback で隔離する接続。

    **先に 1 文実行してトランザクションを開いておく**のが要点。psycopg の
    ``conn.transaction()`` は、トランザクションが未開始なら BEGIN して**ブロック脱出時に
    COMMIT する**(= テストの rollback 隔離が効かなくなる)一方、既にトランザクション中なら
    SAVEPOINT として振る舞う。書込ヘルパ(C-1 で transaction ブロックを持つ)を
    rollback 隔離下で検証するため、後者の状態にしてから渡す。
    """
    c = connect()
    c.execute("SELECT 1")  # トランザクションを開始させる(上記の理由)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def autocommit_conn(migrated_db):
    """**本番と同じ autocommit=True の接続**(``queries.connect_boardroom`` 相当)。

    独立役員審査 0020 C-1: 非 autocommit の接続では、ヘルパが transaction ブロックを
    持たなくてもテスト側の rollback で原子性があるように見えてしまい、本番経路の欠陥を
    検出できない。autocommit では各文が即時確定するため、``conn.transaction()`` が
    無ければ「現在値だけ残る」がそのまま観測される。

    rollback による隔離が効かないので、後片付けは明示的に行う(履歴表は追記オンリー
    トリガで DELETE できないため、テーブル所有ロールでトリガを一時的に無効化して消す)。
    """
    c = connect(autocommit=True)
    try:
        yield c
    finally:
        with c.cursor() as cur:
            cur.execute("DELETE FROM ops.org_icon_overrides WHERE member_id = 'aya'")
            cur.execute("ALTER TABLE ops.org_icon_override_log DISABLE TRIGGER USER")
            cur.execute("DELETE FROM ops.org_icon_override_log WHERE member_id = 'aya'")
            cur.execute("ALTER TABLE ops.org_icon_override_log ENABLE TRIGGER USER")
        c.close()


@contextmanager
def _log_writes_blocked(conn):
    """履歴表への INSERT を失敗させる(現在値がロールバックされることの検証用)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE FUNCTION ops._test_block_log() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'test: 履歴表への書込を意図的に失敗させた'; END; $$
            """
        )
        cur.execute(
            "CREATE TRIGGER _test_block_log BEFORE INSERT ON ops.org_icon_override_log"
            " FOR EACH ROW EXECUTE FUNCTION ops._test_block_log()"
        )
    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TRIGGER IF EXISTS _test_block_log ON ops.org_icon_override_log")
            cur.execute("DROP FUNCTION IF EXISTS ops._test_block_log()")


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
def _headers(content_type: str) -> dict[str, str]:
    return {"content-type": content_type, "content-length": "1024"}


def test_update_icon_saves_after_validation(conn):
    org.update_icon(
        conn, "aya", _URL_A, "representative",
        opener=lambda url, method, timeout: _headers("image/png"),
    )
    assert org.icon_overrides(conn)["aya"] == _URL_A


def test_update_icon_does_not_write_when_validation_fails(conn):
    with pytest.raises(org.IconUrlError):
        org.update_icon(
            conn, "aya", "https://example.test/page", "representative",
            opener=lambda url, method, timeout: _headers("text/html"),
        )
    assert org.icon_overrides(conn) == {}
    assert _log(conn, "aya") == []


# ── スキーマ制約 ──────────────────────────────────────────────────────────────
def _expect_rejected(conn, sql: str, error):
    """``sql`` が ``error`` で拒否されることを検証する(SAVEPOINT で外側の tx を守る)。

    ``conn.rollback()`` で復旧すると外側のトランザクションごと閉じてしまい、以降の
    書込ヘルパの ``transaction()`` が「未開始 → BEGIN → 脱出時 COMMIT」に化けて
    テストデータがテスト DB に残る。エラーからの復旧は必ず SAVEPOINT で行う。
    """
    with pytest.raises(error), conn.transaction():
        conn.execute(sql)


def test_https_check_blocks_plain_http(conn):
    """アプリを迂回した書込でも http は入らない(最後の防壁)。"""
    _expect_rejected(
        conn,
        "INSERT INTO ops.org_icon_overrides (member_id, icon_url, updated_by)"
        " VALUES ('aya', 'http://x/a.png', 'representative')",
        psycopg.errors.CheckViolation,
    )


def test_append_only_log_rejects_update_and_delete(conn):
    """履歴表は所有ロールでも書き換えられない(トリガで強制 — C-3)。"""
    org.set_icon_override(conn, "aya", _URL_A, "representative")
    _expect_rejected(
        conn,
        "UPDATE ops.org_icon_override_log SET icon_url = 'https://x/z.png'",
        psycopg.errors.RaiseException,
    )
    _expect_rejected(
        conn, "DELETE FROM ops.org_icon_override_log", psycopg.errors.RaiseException
    )
    assert _log(conn, "aya") == [("set", _URL_A, "representative")]  # 履歴は無傷


def test_truncate_is_blocked_on_both_tables(conn):
    """TRUNCATE は行トリガを迂回するため文トリガで塞ぐ(0018 の先例 — C-3)。"""
    for table in ("ops.org_icon_override_log", "ops.org_icon_overrides"):
        _expect_rejected(conn, f"TRUNCATE {table}", psycopg.errors.RaiseException)  # noqa: S608


def test_log_action_and_url_must_agree(conn):
    _expect_rejected(
        conn,
        "INSERT INTO ops.org_icon_override_log (member_id, action, icon_url, actor)"
        " VALUES ('aya', 'reset', 'https://x/a.png', 'representative')",
        psycopg.errors.CheckViolation,
    )
    _expect_rejected(
        conn,
        "INSERT INTO ops.org_icon_override_log (member_id, action, icon_url, actor)"
        " VALUES ('aya', 'set', NULL, 'representative')",
        psycopg.errors.CheckViolation,
    )


# ── 原子性(独立役員審査 0020 C-1)──────────────────────────────────────────
# 本番の役員室接続は autocommit=True。ヘルパが conn.transaction() で囲まないと
# 「現在値だけ確定し履歴が無い」状態が残る(方式 B の担保が不成立になる)。
def test_set_rolls_back_current_value_when_log_write_fails(autocommit_conn):
    conn = autocommit_conn
    with _log_writes_blocked(conn), pytest.raises(psycopg.errors.RaiseException):
        org.set_icon_override(conn, "aya", _URL_A, "representative")
    assert "aya" not in org.icon_overrides(conn)  # 現在値も残っていない
    assert _log(conn, "aya") == []


def test_update_rolls_back_to_previous_value_when_log_write_fails(autocommit_conn):
    """既に上書きがある場合、失敗した更新は**前の値のまま**でなければならない。"""
    conn = autocommit_conn
    org.set_icon_override(conn, "aya", _URL_A, "representative")
    with _log_writes_blocked(conn), pytest.raises(psycopg.errors.RaiseException):
        org.set_icon_override(conn, "aya", _URL_B, "representative")
    assert org.icon_overrides(conn)["aya"] == _URL_A
    assert _log(conn, "aya") == [("set", _URL_A, "representative")]


def test_clear_rolls_back_deletion_when_log_write_fails(autocommit_conn):
    conn = autocommit_conn
    org.set_icon_override(conn, "aya", _URL_A, "representative")
    with _log_writes_blocked(conn), pytest.raises(psycopg.errors.RaiseException):
        org.clear_icon_override(conn, "aya", "representative")
    assert org.icon_overrides(conn)["aya"] == _URL_A  # 削除が巻き戻っている


def test_autocommit_writes_persist_without_explicit_commit(autocommit_conn):
    """transaction() で囲んでも autocommit 接続では抜けた時点で確定する(別接続から見える)。"""
    org.set_icon_override(autocommit_conn, "aya", _URL_A, "representative")
    other = connect()
    try:
        assert org.icon_overrides(other)["aya"] == _URL_A
    finally:
        other.close()
