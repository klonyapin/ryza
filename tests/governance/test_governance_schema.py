"""0013_governance_assets.sql とローダの DB 依存部分の受け入れテスト。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行する。
接続不可なら skip(Docker 未導入環境向け)。テストは commit せず rollback で隔離。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.db.conn import connect
from ryza.governance.personas import assume_role, recent_stances, record_stance


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _new_minute(cur, meeting: str = "investment_committee") -> int:
    cur.execute(
        """
        INSERT INTO governance.minutes (meeting, held_at, attendees, body_md, run_id)
        VALUES (%s, now(), %s, '# 議事録\n対話全文', 0)
        RETURNING minute_id
        """,
        (meeting, ["representative", "cio", "independent_officer"]),
    )
    return cur.fetchone()[0]


# ── スキーマの存在 ──────────────────────────────────────────────────────────
def test_governance_tables_exist(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'governance'
            """
        )
        tables = {r[0] for r in cur.fetchall()}
    # decisions は 0007、minutes/minute_resolutions/stances は 0013。
    assert {"decisions", "minutes", "minute_resolutions", "stances"}.issubset(tables)


# ── 議事録と決議マーク ──────────────────────────────────────────────────────
def test_minutes_and_resolution_roundtrip(conn):
    with conn.cursor() as cur:
        minute_id = _new_minute(cur)
        cur.execute(
            """
            INSERT INTO governance.minute_resolutions
                (minute_id, seq, title, resolution_md, proposal_ref, resolved_by)
            VALUES (%s, 1, 'IPS 改訂第1号', '決議本文(反対意見含む)',
                    'ips-rev-2026-08', 'representative')
            RETURNING resolution_id
            """,
            (minute_id,),
        )
        assert cur.fetchone()[0] > 0
        # 同一議事録内の決議番号は一意(二重マーク防止)。
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """
                INSERT INTO governance.minute_resolutions
                    (minute_id, seq, title, resolution_md, resolved_by)
                VALUES (%s, 1, '重複', 'x', 'representative')
                """,
                (minute_id,),
            )
    conn.rollback()


def test_unknown_meeting_rejected(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            _new_minute(cur, meeting="watercooler_chat")
    conn.rollback()


def test_minutes_are_append_only(conn):
    """議事録・決議は証憑(05 §4)— UPDATE / DELETE は禁止。"""
    with conn.cursor() as cur:
        minute_id = _new_minute(cur)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.minutes SET body_md = '改竄' WHERE minute_id = %s",
                (minute_id,),
            )
    conn.rollback()
    with conn.cursor() as cur:
        minute_id = _new_minute(cur)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "DELETE FROM governance.minutes WHERE minute_id = %s", (minute_id,)
            )
    conn.rollback()


# ── stances の書込/読出とローダ ─────────────────────────────────────────────
def test_stance_write_read_roundtrip(conn):
    sid = record_stance(
        conn, role="independent_officer", kind="concern",
        summary="バックテスト期間がカットオフ前を含む懸念", run_id=0,
    )
    assert sid > 0
    got = recent_stances(conn, "independent_officer")
    assert [s.stance_id for s in got][:1] == [sid]
    assert got[0].kind == "concern"
    assert got[0].role == "independent_officer"
    conn.rollback()


def test_stances_isolated_by_role(conn):
    """独立役員の着任時に執行側(CIO)の記憶が混ざらない(05 §6-2)。"""
    record_stance(conn, role="cio", kind="claim", summary="CIO の主張", run_id=0)
    record_stance(
        conn, role="independent_officer", kind="concern", summary="独立役員の懸念", run_id=0
    )
    ind = recent_stances(conn, "independent_officer", limit=50)
    assert all(s.role == "independent_officer" for s in ind)
    assert not any("CIO の主張" in s.summary for s in ind)
    conn.rollback()


def test_recent_stances_limit_and_order(conn):
    for i in range(5):
        record_stance(conn, role="audit", kind="claim", summary=f"指摘 {i}", run_id=0)
    got = recent_stances(conn, "audit", limit=3)
    assert len(got) == 3
    # 新しい順(stated_at 同時刻でも stance_id 降順で安定)。
    assert [s.summary for s in got] == ["指摘 4", "指摘 3", "指摘 2"]
    conn.rollback()


def test_stance_unknown_kind_rejected(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        record_stance(conn, role="cio", kind="applause", summary="拍手", run_id=0)
    conn.rollback()


def test_assume_role_end_to_end(conn):
    """実 charter/system + DB の stances から着任プロンプトが組み上がる。"""
    record_stance(
        conn, role="independent_officer", kind="concern",
        summary="デモ資金スケールの外挿懸念", run_id=0,
    )
    prompt = assume_role(conn, "independent_officer", limit=5)
    assert "独立役員" in prompt
    assert "職務規程(charter)" in prompt
    assert "デモ資金スケールの外挿懸念" in prompt
    conn.rollback()
