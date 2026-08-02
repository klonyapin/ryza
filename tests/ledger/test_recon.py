"""照合(recon.py)の検証。

受け入れ基準: 照合一致で matched、意図的に壊した snapshot で break_open + 通知フック発火。
現金総額は照合対象外(設計書 §9)。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ryza.ledger import posting, recon

D = Decimal
DAY = date(2026, 8, 3)


def _buy(conn, run_id, iid, qty, price):
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=qty, price=price, entry_date=DAY, run_id=run_id)


def test_reconcile_all_matched(conn, run_id):
    _buy(conn, run_id, 1001, 100, 500)
    _buy(conn, run_id, 1002, 50, 200)
    snapshot = {
        "positions": {1001: 100, 1002: 50},
        "valuation": {1001: 50000, 1002: 10000},  # ours 取得原価と一致(MTM前)
    }
    result = recon.reconcile(conn, book_id="DEMO_FUND", date=DAY,
                             broker_snapshot=snapshot, run_id=run_id)
    assert result.all_matched
    assert result.breaks == []
    # reconciliations に matched で記録される。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ledger.reconciliations "
            "WHERE book_id='DEMO_FUND' AND status='matched'"
        )
        assert cur.fetchone()[0] == len(result.rows)


def test_reconcile_position_break_fires_callback(conn, run_id):
    _buy(conn, run_id, 1001, 100, 500)
    snapshot = {"positions": {1001: 90}, "valuation": {1001: 50000}}
    seen = []
    result = recon.reconcile(conn, book_id="DEMO_FUND", date=DAY,
                             broker_snapshot=snapshot, run_id=run_id,
                             on_break=seen.append)
    assert not result.all_matched
    assert len(seen) == 1
    assert seen[0]["item"] == "position:1001"
    assert seen[0]["ours"] == D(100)
    assert seen[0]["theirs"] == D(90)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM ledger.reconciliations "
            "WHERE book_id='DEMO_FUND' AND item='position:1001'"
        )
        assert cur.fetchone()[0] == "break_open"


def test_reconcile_valuation_break(conn, run_id):
    _buy(conn, run_id, 1001, 100, 500)
    # 評価額を意図的にずらす。
    snapshot = {"positions": {1001: 100}, "valuation": {1001: 55000}}
    result = recon.reconcile(conn, book_id="DEMO_FUND", date=DAY,
                             broker_snapshot=snapshot, run_id=run_id)
    assert not result.all_matched
    val_break = [b for b in result.breaks if b["item"] == "valuation"]
    assert len(val_break) == 1
    assert val_break[0]["ours"] == D(50000)
    assert val_break[0]["theirs"] == D(55000)
