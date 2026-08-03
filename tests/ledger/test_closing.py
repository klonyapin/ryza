"""日次締め(closing.py)と財務諸表(statements.py)の統合検証。

受け入れ基準:
- フィクスチャで 記帳→締め→試算表がゼロバランス、BS 資産=負債+資本、NAV=資産-負債
- 照合一致で NAV confirmed、意図的に壊した snapshot で break_open + provisional のまま
- 未記帳の約定の検出(冪等: 記帳済み fill はスキップ)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ryza.ledger import closing, posting, statements

D = Decimal
DAY = date(2026, 8, 3)


def _setup_two_positions(conn, run_id):
    """2 銘柄を買い建てる。1001: 100@500、1002: 50@1000(手数料100)。"""
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=100, price=500, entry_date=DAY, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1002, side="buy",
                      qty=50, price=1000, fee=100, entry_date=DAY, run_id=run_id)


def test_close_zero_balance_and_bs_identity(conn, run_id):
    _setup_two_positions(conn, run_id)
    price_source = {1001: 600, 1002: 900}  # 1001 値上がり、1002 値下がり
    snapshot = {
        "positions": {1001: 100, 1002: 50},
        "valuation": {1001: 60000, 1002: 45000},  # ours 帳簿価額と一致
    }
    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source=price_source,
        run_id=run_id, broker_snapshot=snapshot,
    )

    # 試算表がゼロバランス
    tb = statements.trial_balance(conn, "DEMO_FUND", DAY)
    total = tb[tb["account_id"] == "_TOTAL"].iloc[0]
    assert total["balance"] == D(0)
    assert total["debit"] == total["credit"]

    # BS 恒等式: 資産 = 負債 + 資本 + 当期純損益、NAV = 資産 - 負債
    t = statements.book_totals(conn, "DEMO_FUND", DAY)
    assert t["assets"] == t["liabilities"] + t["equity"] + t["net_income"]
    assert t["nav"] == t["assets"] - t["liabilities"]

    # 具体値: 未実現 = +10000(1001) -5000(1002) = 5000、手数料 100
    # NAV = 出資 10,000,000(0006+0011)+ 4900
    assert t["net_income"] == D(4900)
    assert t["nav"] == D(10004900)
    assert result["nav"] == D(10004900)

    # 照合一致 → confirmed
    assert result["status"] == "confirmed"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, nav FROM ledger.nav_snapshots WHERE book_id='DEMO_FUND' "
            "AND snap_date=%s", (DAY,),
        )
        status, nav = cur.fetchone()
    assert status == "confirmed"
    assert nav == D(10004900)


def test_close_broken_snapshot_stays_provisional(conn, run_id):
    _setup_two_positions(conn, run_id)
    price_source = {1001: 600, 1002: 900}
    broken = {
        "positions": {1001: 99, 1002: 50},  # 1001 の数量が不一致
        "valuation": {1001: 60000, 1002: 45000},
    }
    breaks_seen = []
    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source=price_source,
        run_id=run_id, broker_snapshot=broken, on_break=breaks_seen.append,
    )

    assert result["status"] == "provisional"
    assert not result["recon"].all_matched
    assert len(breaks_seen) == 1
    assert breaks_seen[0]["item"] == "position:1001"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM ledger.nav_snapshots WHERE book_id='DEMO_FUND' "
            "AND snap_date=%s", (DAY,),
        )
        assert cur.fetchone()[0] == "provisional"
        cur.execute(
            "SELECT count(*) FROM ledger.reconciliations WHERE status='break_open' "
            "AND book_id='DEMO_FUND'",
        )
        assert cur.fetchone()[0] == 1


def test_close_idempotent_fill_detection(conn, run_id):
    """trade.fills を仕込み、run_daily_close が未記帳の約定を1回だけ記帳することを確認。"""
    # trade チェーンを最小構成で作る。
    with conn.cursor() as cur:
        ev = _mk_evidence(cur)
        cur.execute(
            """INSERT INTO trade.signals
               (strategy_id, strategy_ver, instrument_id, direction, rationale_refs, ts, run_id)
               VALUES ('s','v1', 2001, 'long', '{}'::jsonb, %s, %s) RETURNING signal_id""",
            (datetime(2026, 8, 3, tzinfo=UTC), run_id),
        )
        signal_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO trade.order_intents
               (track, signal_ids, instrument_id, side, qty, order_type, sizing_calc,
                risk_snapshot, gate_verdict, gate_detail, ts, run_id)
               VALUES ('demo', %s, 2001, 'buy', 20, 'market', '{}'::jsonb, '{}'::jsonb,
                       'pass', '{}'::jsonb, %s, %s) RETURNING intent_id""",
            ([signal_id], datetime(2026, 8, 3, tzinfo=UTC), run_id),
        )
        intent_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO trade.orders
               (intent_id, track, broker, state, state_history, ts)
               VALUES (%s, 'demo', 'sim', 'filled', '[]'::jsonb, %s) RETURNING order_id""",
            (intent_id, datetime(2026, 8, 3, tzinfo=UTC)),
        )
        order_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO trade.fills (order_id, qty, price, fee, filled_at, evidence_id)
               VALUES (%s, 20, 300, 0, %s, %s) RETURNING fill_id""",
            (order_id, datetime(2026, 8, 3, tzinfo=UTC), ev),
        )

    price_source = {2001: 300}
    r1 = closing.run_daily_close(conn, book_id="DEMO_FUND", date=DAY,
                                 price_source=price_source, run_id=run_id)
    assert len(r1["fills_recorded"]) == 1  # 1 件記帳

    r2 = closing.run_daily_close(conn, book_id="DEMO_FUND", date=DAY,
                                 price_source=price_source, run_id=run_id)
    assert len(r2["fills_recorded"]) == 0  # 冪等: 既記帳はスキップ


# ── 再締め(独立審査 重要-2)と nav_snapshots のリネージ ───────────────────────
def _post_contribution(conn, run_id, day: date, amount: Decimal) -> int:
    """指定日付で出資を記帳する(拠出資本 = navflow が外部フローとして拾う勘定)。"""
    return posting.post_entry(
        conn,
        book_id="DEMO_FUND",
        entry_date=day,
        description="テスト用の出資",
        lines=[
            {"account_id": "cash", "debit": amount, "currency": "JPY"},
            {"account_id": "capital", "credit": amount, "currency": "JPY"},
        ],
        evidence={"kind": "decision", "payload": {"test": "reclose"}, "source": "test"},
        run_id=run_id,
        posted_by="test.ledger",
    )


def _snapshot(conn, day: date) -> tuple[Decimal, str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT nav, status, detail FROM ledger.nav_snapshots "
            "WHERE book_id = 'DEMO_FUND' AND snap_date = %s",
            (day,),
        )
        return cur.fetchone()


def test_reclose_absorbs_entry_posted_after_close(conn, run_id):
    """締めの後に同日付で立った仕訳を、翌営業日の再締めが NAV に取り込む(重要-2)。"""
    first = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    assert first["nav"] == D(10_000_000)  # 0006+0011 の出資のみ

    _post_contribution(conn, run_id, DAY, D(5_000_000))  # ← 締めの後に立った仕訳
    # 締め直後の状態: スナップショットは古い NAV のまま(これが偽リターンの原因)。
    assert _snapshot(conn, DAY)[0] == D(10_000_000)

    changed = closing.reclose_recent(
        conn, book_id="DEMO_FUND", through=DAY + timedelta(days=1), days=3, run_id=run_id
    )
    assert [c["date"] for c in changed] == [DAY]
    assert changed[0]["nav_before"] == D(10_000_000)
    assert changed[0]["nav_after"] == D(15_000_000)

    nav, status, detail = _snapshot(conn, DAY)
    assert nav == D(15_000_000)
    assert status == "provisional"  # status は据え置き(照合の結論は変わらない)
    assert D(detail["reclose"]["nav_before"]) == D(10_000_000)


def test_reclose_is_noop_when_nothing_changed(conn, run_id):
    """値が変わらない日は書き込まない(確定値の producer を無駄に上書きしない)。"""
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    before = _snapshot(conn, DAY)[2]["producer"]
    changed = closing.reclose_recent(
        conn, book_id="DEMO_FUND", through=DAY + timedelta(days=1), days=3, run_id=run_id
    )
    assert changed == []
    after = _snapshot(conn, DAY)[2]
    assert after["producer"] == before
    assert "reclose" not in after


def test_reclose_window_bounds_which_days_are_rewritten(conn, run_id):
    """窓の外の日は書き換えない(過去を書き換えうる期間を N 営業日に閉じる)。"""
    days = [DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)]
    for d in days:
        closing.run_daily_close(
            conn, book_id="DEMO_FUND", date=d, price_source={}, run_id=run_id
        )
    _post_contribution(conn, run_id, DAY, D(5_000_000))

    # 窓 2 営業日: 直近 2 日(DAY+1, DAY+2)だけが再計算され、DAY は窓の外で据え置き。
    changed = closing.reclose_recent(
        conn, book_id="DEMO_FUND", through=DAY + timedelta(days=3), days=2, run_id=run_id
    )
    assert [c["date"] for c in changed] == [DAY + timedelta(days=1), DAY + timedelta(days=2)]
    assert _snapshot(conn, DAY)[0] == D(10_000_000)

    # 窓 3 営業日なら DAY も入り、残りの 1 日が解消する。
    changed = closing.reclose_recent(
        conn, book_id="DEMO_FUND", through=DAY + timedelta(days=3), days=3, run_id=run_id
    )
    assert [c["date"] for c in changed] == [DAY]
    assert _snapshot(conn, DAY)[0] == D(15_000_000)


def test_reclose_rejects_nonpositive_window(conn, run_id):
    """窓 0 は再締めなし(再締めを止めたい運用のための明示的な無効化)。"""
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    _post_contribution(conn, run_id, DAY, D(5_000_000))
    assert closing.reclose_recent(
        conn, book_id="DEMO_FUND", through=DAY + timedelta(days=1), days=0, run_id=run_id
    ) == []
    assert _snapshot(conn, DAY)[0] == D(10_000_000)


def test_nav_snapshot_records_producer_lineage(conn, run_id):
    """nav_snapshots.detail.producer に producer_job/code_version/as_of/input_refs(不変原則3)。"""
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    producer = _snapshot(conn, DAY)[2]["producer"]
    assert producer["job"] == "ledger.closing.run_daily_close"
    assert producer["run_id"] == run_id
    assert producer["as_of"] == DAY.isoformat()
    assert producer["input_refs"]["ledger.journal_entries.max_entry_id"] is not None
    with conn.cursor() as cur:
        cur.execute("SELECT code_version FROM meta.runs WHERE run_id = %s", (run_id,))
        assert producer["code_version"] == cur.fetchone()[0]

    # 再締めで書き換えた値は「誰が書いたか」も差し替わり、前の producer が残る。
    _post_contribution(conn, run_id, DAY, D(5_000_000))
    closing.reclose_recent(
        conn, book_id="DEMO_FUND", through=DAY + timedelta(days=1), days=3, run_id=run_id
    )
    detail = _snapshot(conn, DAY)[2]
    assert detail["producer"]["job"] == "ledger.closing.reclose_recent"
    assert detail["reclose"]["previous_producer"]["job"] == "ledger.closing.run_daily_close"
    # 水位(input_refs)が進んでいる = 後から立った仕訳を見たうえでの値だと辿れる。
    key = "ledger.journal_entries.max_entry_id"
    assert detail["producer"]["input_refs"][key] > producer["input_refs"][key]


def _mk_evidence(cur) -> int:
    cur.execute(
        """INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
           VALUES ('broker_fill', 'inline://trade', sha256('x'::bytea), 'test', now())
           RETURNING evidence_id"""
    )
    return cur.fetchone()[0]
