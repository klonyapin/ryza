"""gate_and_record の帳簿単位直列化(pg_advisory_xact_lock — 審査条件2)の検証。

並行する2接続が同じ「約定前状態」を読んで二重に枠を消費する TOCTOU を、帳簿単位の
advisory lock で封鎖していることを確認する。前提行(trading_state・limits_state)は
両接続から見える必要があるため一時的に commit し、teardown で原状復帰する
(test_store.py の autouse フィクスチャとは独立の接続を使うため専用ファイルに置く)。
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from ryza.db.conn import connect
from ryza.gate.orders import gate_and_record
from ryza.ledger import create_run

from .conftest import jp_stock_proposal

_NAV = Decimal(10_000_000)
_CASH = Decimal(5_000_000)


@pytest.fixture
def committed_prereqs(migrated_db):
    """trading_state=normal と limits_state を commit で用意し、終了時に原状復帰する。"""
    admin = connect(autocommit=True)
    with admin.cursor() as cur:
        cur.execute("SELECT state FROM ops.trading_state")
        prior_state = cur.fetchone()
        cur.execute("SELECT 1 FROM risk.limits_state WHERE book_id = 'DEMO_FUND'")
        prior_limits = cur.fetchone() is not None
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, updated_by) VALUES ('normal', 'test.lock')
            ON CONFLICT (singleton) DO UPDATE SET state = 'normal', updated_by = 'test.lock'
            """
        )
        cur.execute(
            """
            INSERT INTO risk.limits_state (book_id, as_of) VALUES ('DEMO_FUND', now())
            ON CONFLICT (book_id) DO NOTHING
            """
        )
    try:
        yield
    finally:
        with admin.cursor() as cur:
            if prior_state is None:
                cur.execute("DELETE FROM ops.trading_state")
            else:
                cur.execute(
                    "UPDATE ops.trading_state SET state = %s, updated_by = 'test.lock'",
                    (prior_state[0],),
                )
            if not prior_limits:
                cur.execute("DELETE FROM risk.limits_state WHERE book_id = 'DEMO_FUND'")
        admin.close()


def test_gate_serialized_per_book(committed_prereqs):
    """接続1がゲート判定中(未 commit)の間、同一帳簿の接続2は待たされる。"""
    c1 = connect()
    c2 = connect()
    try:
        run1 = create_run(c1, "test.gate.lock1")
        _, _, result1 = gate_and_record(
            c1, jp_stock_proposal(), nav=_NAV, cash=_CASH, run_id=run1
        )
        assert result1.verdict == "pass"  # c1 が advisory lock を保持したまま

        with c2.cursor() as cur:
            cur.execute("SET lock_timeout = '300ms'")
        run2 = create_run(c2, "test.gate.lock2")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            gate_and_record(c2, jp_stock_proposal(), nav=_NAV, cash=_CASH, run_id=run2)
        c2.rollback()

        # c1 が終了(rollback)すればロックは解放され、c2 は通る。
        c1.rollback()
        with c2.cursor() as cur:
            cur.execute("SET lock_timeout = '5s'")
        run2 = create_run(c2, "test.gate.lock3")
        _, _, result2 = gate_and_record(
            c2, jp_stock_proposal(), nav=_NAV, cash=_CASH, run_id=run2
        )
        assert result2.verdict == "pass"
    finally:
        c1.rollback()
        c2.rollback()
        c1.close()
        c2.close()
