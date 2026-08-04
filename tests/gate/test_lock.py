"""gate_and_record の帳簿単位直列化(pg_advisory_xact_lock — 審査条件2)の検証。

並行する2接続が同じ「約定前状態」を読んで二重に枠を消費する TOCTOU を、帳簿単位の
advisory lock で封鎖していることを確認する。前提行(trading_state・limits_state)は
両接続から見える必要があるため一時的に commit し、teardown で原状復帰する
(test_store.py の autouse フィクスチャとは独立の接続を使うため専用ファイルに置く)。

**分離の明示**(F-14b / Issue #124 pass5-4)。このファイルのテストは他の DB テストと
異なり、``committed_prereqs`` が **autocommit 接続で commit する**(advisory lock を
複数接続で検証するには両接続から前提行が見える必要があるため)。他テストは通常
「1 テスト = 1 トランザクション + 最後に rollback」で隔離するが、本ファイルは
その原則を意図的に外している(原状復帰は finally で行う)。並行実行(pytest-xdist
等)を導入する場合、このファイルは ``commits_shared_state`` マーカーで直列化対象と
して選別できるようにしてある(pyproject.toml `markers`)。現状の実行構成は
逐次実行のため変更不要だが、干渉リスクの存在をコード上に固定しておく。
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from ryza.db.conn import connect
from ryza.gate.orders import gate_and_record
from ryza.ledger import create_run

from .conftest import jp_stock_proposal

# ファイル全体を ``commits_shared_state`` として印字。将来 xdist 等を入れるときに
# ``-m "not commits_shared_state"`` で並列テストから外し、単独ワーカーで直列に流せる。
pytestmark = pytest.mark.commits_shared_state

_NAV = Decimal(10_000_000)
_CASH = Decimal(5_000_000)


@pytest.fixture
def committed_prereqs(migrated_db):
    """trading_state=normal と limits_state を commit で用意し、終了時に原状復帰する。

    **commit を伴う理由**: 本ファイルのテストは並行する2接続(c1/c2)から同じ帳簿の
    ``gate_and_record`` を呼び、advisory lock の直列化を観測する。両接続から前提行
    (``ops.trading_state`` / ``risk.limits_state``)が見える必要があるため、通常の
    「1 テスト = 1 トランザクション」隔離では検証できず、autocommit 接続で **一時的に
    commit** する。この commit は同一 DB を使う他テスト・他セッションに漏れうる。

    **干渉し得る対象**:

    - ``ops.trading_state`` は **singleton**(migrations/0007 の ``singleton BOOLEAN
      PRIMARY KEY DEFAULT TRUE`` — 1 行制約)。テスト中に ``state='normal'`` に固定
      するため、同時に走る他テストが別の値(``halted`` 等)を要求しているとどちらか
      が落ちる。本フィクスチャが finally で ``prior_state`` に戻すことで、通常の
      「連続実行」では干渉しないが、**並行実行**では復元前に他ワーカーが読むと崩れる。
    - ``risk.limits_state`` の ``DEMO_FUND`` 行は追記(INSERT ... ON CONFLICT DO
      NOTHING)。本フィクスチャが挿入前に存在しなかった場合のみ finally で削除する
      ため、既存行は温存される。

    **原状復帰の仕組み**: try/finally で ``prior_state`` / ``prior_limits`` を保存し、
    テスト成功・失敗いずれの経路でも復元する。例外時も finally は実行される(pytest の
    fixture teardown 契約)。それでも並行実行では復元前の隙間があるため、**このファ
    イルは ``commits_shared_state`` マーカーで直列化対象として印字してある**。
    """
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
