"""risk.limits_state 更新の DB テスト: dd_hard ラッチ・非自動解除・冪等性・台帳(T-015)。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from psycopg import errors

from ryza.risk.engine import ESResult, RiskState
from ryza.risk.state import release_dd_hard, upsert_limits_state

_AS_OF = datetime(2030, 1, 10, tzinfo=UTC)


def make_state(**flags) -> RiskState:
    """テスト用の RiskState(測定値は最小限・フラグだけ指定)。"""
    defaults = dict(dd_soft=False, dd_hard=False, vol_exceeded=False, es_exceeded=False)
    defaults.update(flags)
    return RiskState(
        as_of_day=date(2030, 1, 10),
        nav=Decimal(9_000_000),
        peak_nav=Decimal(10_000_000),
        drawdown=Decimal("0.1"),
        n_returns=30,
        sufficient=True,
        ewma_vol_annual=0.10,
        es95=ESResult(0.01, 0.012, 0.012, 40),
        notes=(),
        **defaults,
    )


def _row(conn, book="DEMO_FUND"):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of, run_id
            FROM risk.limits_state WHERE book_id = %s
            """,
            (book,),
        )
        return cur.fetchone()


def _events(conn, book="DEMO_FUND"):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event, dd_hard, actor, reason FROM risk.limits_state_events
            WHERE book_id = %s ORDER BY id
            """,
            (book,),
        )
        return cur.fetchall()


def test_upsert_creates_row_with_as_of_and_run_id(conn, run_id):
    effective = upsert_limits_state(
        conn, "DEMO_FUND", make_state(dd_soft=True), as_of=_AS_OF, run_id=run_id
    )
    assert effective == {
        "dd_soft": True, "dd_hard": False, "vol_exceeded": False, "es_exceeded": False,
    }
    row = _row(conn)
    assert row[0] is True and row[1] is False
    assert row[4] == _AS_OF and row[5] == run_id
    events = _events(conn)
    assert len(events) == 1 and events[0][0] == "engine_update"


def test_upsert_idempotent_single_row(conn, run_id):
    upsert_limits_state(conn, "DEMO_FUND", make_state(), as_of=_AS_OF, run_id=run_id)
    upsert_limits_state(conn, "DEMO_FUND", make_state(), as_of=_AS_OF, run_id=run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM risk.limits_state WHERE book_id = 'DEMO_FUND'")
        assert cur.fetchone()[0] == 1
    assert len(_events(conn)) == 2  # 台帳は追記(実行履歴)、状態は同値上書き


def test_dd_hard_latches_against_engine_clear(conn, run_id):
    """dd_hard は一度立ったらエンジン更新(測定値 false)では消えない(OR ラッチ)。"""
    upsert_limits_state(
        conn, "DEMO_FUND", make_state(dd_hard=True, dd_soft=True), as_of=_AS_OF, run_id=run_id
    )
    effective = upsert_limits_state(
        conn, "DEMO_FUND", make_state(), as_of=_AS_OF, run_id=run_id
    )
    assert effective["dd_hard"] is True  # 実効値はラッチ済み
    assert _row(conn)[1] is True


def test_dd_hard_direct_update_blocked_by_trigger(conn, run_id):
    """任意接続の直接 UPDATE では dd_hard を消せない(0015 トリガ — 引き継ぎ事項1)。"""
    upsert_limits_state(
        conn, "DEMO_FUND", make_state(dd_hard=True), as_of=_AS_OF, run_id=run_id
    )
    with pytest.raises(errors.RaiseException, match="dd_hard の解除は委員会"):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE risk.limits_state SET dd_hard = false WHERE book_id = 'DEMO_FUND'"
                )
    assert _row(conn)[1] is True


def test_limits_state_delete_blocked_while_dd_hard(conn, run_id):
    """DELETE→再INSERT による dd_hard 消去の迂回も封鎖(0015 トリガ)。"""
    upsert_limits_state(
        conn, "DEMO_FUND", make_state(dd_hard=True), as_of=_AS_OF, run_id=run_id
    )
    with pytest.raises(errors.RaiseException, match="削除できない"):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM risk.limits_state WHERE book_id = 'DEMO_FUND'")


def test_limits_state_delete_allowed_when_not_latched(conn, run_id):
    """dd_hard=false の行の削除はラッチと無関係のため許される(テスト原状復帰等)。"""
    upsert_limits_state(conn, "DEMO_FUND", make_state(), as_of=_AS_OF, run_id=run_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM risk.limits_state WHERE book_id = 'DEMO_FUND'")
        assert cur.rowcount == 1


def test_release_dd_hard_committee_path(conn, run_id):
    """委員会の明示操作(actor・reason 必須)だけが解除できる。台帳に記録が残る。"""
    upsert_limits_state(
        conn, "DEMO_FUND", make_state(dd_hard=True), as_of=_AS_OF, run_id=run_id
    )
    ok = release_dd_hard(
        conn,
        "DEMO_FUND",
        actor="investment_committee",
        reason="復帰条件充足(委員会決議 2030-01-15)",
        run_id=run_id,
    )
    assert ok is True
    assert _row(conn)[1] is False
    events = _events(conn)
    assert events[-1][0] == "dd_hard_release"
    assert events[-1][2] == "investment_committee"
    # 解除済みならもう一度呼んでも False(冪等)。
    assert not release_dd_hard(
        conn, "DEMO_FUND", actor="investment_committee", reason="再実行", run_id=run_id
    )


def test_release_requires_actor_and_reason(conn, run_id):
    with pytest.raises(ValueError, match="actor と reason が必須"):
        release_dd_hard(conn, "DEMO_FUND", actor=" ", reason="x", run_id=run_id)
    with pytest.raises(ValueError, match="actor と reason が必須"):
        release_dd_hard(conn, "DEMO_FUND", actor="x", reason="", run_id=run_id)


def test_release_guc_does_not_leak(conn, run_id):
    """解除キーは release の中で即時撤去され、同一 Tx の後続 UPDATE には効かない。"""
    upsert_limits_state(
        conn, "DEMO_FUND", make_state(dd_hard=True), as_of=_AS_OF, run_id=run_id
    )
    release_dd_hard(
        conn, "DEMO_FUND", actor="committee", reason="解除", run_id=run_id
    )
    upsert_limits_state(
        conn, "DEMO_FUND", make_state(dd_hard=True), as_of=_AS_OF, run_id=run_id
    )
    with pytest.raises(errors.RaiseException):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE risk.limits_state SET dd_hard = false WHERE book_id = 'DEMO_FUND'"
                )


def test_events_append_only(conn, run_id):
    upsert_limits_state(conn, "DEMO_FUND", make_state(), as_of=_AS_OF, run_id=run_id)
    with pytest.raises(errors.RaiseException, match="追記オンリー"):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("UPDATE risk.limits_state_events SET actor = 'tamper'")


def test_release_event_requires_reason_in_schema(conn, run_id):
    """台帳スキーマ側でも dd_hard_release の reason 必須を強制(CHECK)。"""
    with pytest.raises(errors.CheckViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO risk.limits_state_events
                        (book_id, event, dd_soft, dd_hard, vol_exceeded, es_exceeded,
                         actor, reason, as_of, run_id)
                    VALUES ('DEMO_FUND', 'dd_hard_release', false, false, false, false,
                            'x', '  ', now(), %s)
                    """,
                    (run_id,),
                )
