"""risk.daily の DB 統合テスト: 系列読出・limits_state 更新・レポート・ゲート結合(T-015)。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ryza.gate.orders import gate_and_record
from ryza.risk.daily import (
    load_instrument_returns,
    load_nav_series,
    load_positions,
    run_risk_daily,
)

_AS_OF = datetime(2030, 2, 1, 0, 0, tzinfo=UTC)


def _clear_nav(conn, book="DEMO_FUND"):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ledger.nav_snapshots WHERE book_id = %s", (book,))


def _seed_nav(conn, navs, *, book="DEMO_FUND", start=date(2030, 1, 1)):
    from datetime import timedelta

    with conn.cursor() as cur:
        for i, nav in enumerate(navs):
            cur.execute(
                """
                INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
                VALUES (%s, %s, %s, 'provisional', '{}')
                ON CONFLICT (book_id, snap_date)
                DO UPDATE SET nav = EXCLUDED.nav
                """,
                (book, start + timedelta(days=i), Decimal(str(nav))),
            )


def _seed_capital_flow(conn, run_id, *, amount, entry_date, book="DEMO_FUND"):
    """出資仕訳(cash / capital)を直接記帳する(0011 と同型)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
            VALUES ('decision', '{}', sha256('test'::bytea), 'test', now())
            RETURNING evidence_id
            """
        )
        evidence_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO ledger.journal_entries
                (book_id, entry_date, description, evidence_id, posted_by, run_id)
            VALUES (%s, %s, 'テスト出資', %s, 'test', %s)
            RETURNING entry_id
            """,
            (book, entry_date, evidence_id, run_id),
        )
        entry_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO ledger.journal_lines
                (entry_id, line_no, book_id, account_id, debit, credit, currency)
            VALUES (%s, 1, %s, 'cash', %s, 0, 'JPY'),
                   (%s, 2, %s, 'capital', 0, %s, 'JPY')
            """,
            (entry_id, book, amount, entry_id, book, amount),
        )


def _limits_row(conn, book="DEMO_FUND"):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dd_soft, dd_hard, vol_exceeded, es_exceeded
            FROM risk.limits_state WHERE book_id = %s
            """,
            (book,),
        )
        return cur.fetchone()


def _reports(conn, channel="ops"):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT embed_json, urgent FROM press.outbox
            WHERE channel = %s AND embed_json->>'title' LIKE 'リスクレポート%%'
            ORDER BY id
            """,
            (channel,),
        )
        return cur.fetchall()


def _run(run_id):
    return SimpleNamespace(run_id=run_id)


# ── NAV 系列の読出し(フロー調整)──────────────────────────────────────────────
def test_load_nav_series_with_capital_flow(conn, run_id):
    _clear_nav(conn)
    _seed_nav(conn, [1_000_000, 2_000_000], start=date(2030, 1, 4))
    _seed_capital_flow(conn, run_id, amount=1_000_000, entry_date=date(2030, 1, 5))
    series = load_nav_series(conn, "DEMO_FUND")
    assert [p.nav for p in series] == [Decimal(1_000_000), Decimal(2_000_000)]
    assert series[1].net_flow == Decimal(1_000_000)  # 出資はフロー(損益ではない)
    assert series[0].net_flow == Decimal(0)


# ── 日次サイクル ──────────────────────────────────────────────────────────────
def test_run_risk_daily_measures_and_reports(conn, run_id, ips):
    _clear_nav(conn)
    _seed_nav(conn, [10_000_000, 9_000_000, 8_400_000])  # DD 16% → dd_soft のみ
    detail = run_risk_daily(conn, _run(run_id), as_of=_AS_OF)
    assert detail["DEMO_FUND"]["status"] == "measured"
    row = _limits_row(conn)
    assert row == (True, False, False, False)
    reports = _reports(conn)
    assert len(reports) == 1
    embed, urgent = reports[0]
    assert urgent is True  # フラグ(dd_soft)が立っている → urgent
    assert "DD" in embed["fields"][0]["name"]
    assert "classification" in detail


def test_run_risk_daily_idempotent(conn, run_id):
    _clear_nav(conn)
    _seed_nav(conn, [10_000_000, 9_900_000])
    run_risk_daily(conn, _run(run_id), as_of=_AS_OF)
    run_risk_daily(conn, _run(run_id), as_of=_AS_OF)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM risk.limits_state WHERE book_id = 'DEMO_FUND'")
        assert cur.fetchone()[0] == 1  # 状態は単一行を同値上書き(冪等)
    row = _limits_row(conn)
    assert row == (False, False, False, False)
    assert len(_reports(conn)) == 2  # レポートは実行ごとに 1 通(実行履歴)


def test_run_risk_daily_no_nav_fail_closed(conn, run_id):
    """NAV 系列なし → limits_state を作らない(未測定を「リスク OK」と主張しない)。"""
    _clear_nav(conn)
    detail = run_risk_daily(conn, _run(run_id), as_of=_AS_OF)
    assert detail["DEMO_FUND"]["status"] == "no_nav"
    assert _limits_row(conn) is None  # ゲートは行欠落を fail-closed で block(T-014)
    reports = _reports(conn)
    assert len(reports) == 1 and reports[0][1] is True  # urgent で通知


def test_insufficient_data_noted_in_report(conn, run_id):
    _clear_nav(conn)
    _seed_nav(conn, [10_000_000, 9_950_000, 9_960_000])  # リターン 2 件 < 20
    run_risk_daily(conn, _run(run_id), as_of=_AS_OF)
    embed, _ = _reports(conn)[0]
    notes = next(f for f in embed["fields"] if f["name"] == "注記")
    assert "データ不足 2/20営業日" in notes["value"]


# ── point-in-time(不変原則4): as_of 以降のバーを測定に混入させない ─────────────
def _seed_instrument_position_bars(conn, run_id, *, closes, book="DEMO_FUND"):
    """銘柄+ポジション+日次バー(closes: {ts(datetime): close})を仕込む。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instruments (symbol, asset_class, venue, currency, valid_from)
            VALUES ('PIT.T', 'equity', 'TSE', 'JPY', now())
            RETURNING instrument_id
            """
        )
        inst = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO trading.positions
                (book_id, fm, instrument_id, asset_class, qty, avg_cost, run_id)
            VALUES (%s, 'ben', %s, 'equity_jp', 100, 1000, %s)
            """,
            (book, inst, run_id),
        )
        for ts, close in closes.items():
            cur.execute(
                """
                INSERT INTO market.bars
                    (instrument_id, ts, timeframe, close, source, as_of, run_id)
                VALUES (%s, %s, '1d', %s, 'test', %s, %s)
                """,
                (inst, ts, Decimal(str(close)), ts, run_id),
            )
    return inst


def test_load_positions_ignores_future_bars(conn, run_id):
    inst = _seed_instrument_position_bars(
        conn,
        run_id,
        closes={
            datetime(2030, 1, 30, 6, tzinfo=UTC): 1000,
            datetime(2030, 2, 5, 6, tzinfo=UTC): 9999,  # as_of より未来
        },
    )
    positions, notes = load_positions(conn, "DEMO_FUND", as_of=_AS_OF)
    pos = next(p for p in positions if p.instrument_id == inst)
    assert pos.value == Decimal(100) * Decimal(1000)  # 未来バー(9999)を使わない
    assert notes == []


def test_load_instrument_returns_ignores_future_bars(conn, run_id):
    inst = _seed_instrument_position_bars(
        conn,
        run_id,
        closes={
            datetime(2030, 1, 29, 6, tzinfo=UTC): 100,
            datetime(2030, 1, 30, 6, tzinfo=UTC): 110,
            datetime(2030, 2, 5, 6, tzinfo=UTC): 220,  # as_of より未来
        },
    )
    returns = load_instrument_returns(conn, [inst], as_of=_AS_OF)
    assert list(returns[inst].values()) == [pytest.approx(0.10)]  # 未来リターンなし


# ── ゲート(T-014)との結合: エンジンが立てたフラグで block ─────────────────────
def _normal_trading_state(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, updated_by) VALUES ('normal', 'test')
            ON CONFLICT (singleton) DO UPDATE SET state = 'normal', updated_by = 'test'
            """
        )


def _jp_proposal():
    from ryza.gate.compliance import OrderProposal

    return OrderProposal(
        book_id="DEMO_FUND",
        fm="ben",
        instrument_id=1,
        side="buy",
        qty=Decimal(100),
        order_type="market",
        ref_price=Decimal(1000),
        product="listed_equity_cash",
        asset_class="equity_jp",
        universe_tags=("jp_equity_cash",),
        is_single_name=True,
        unit_size=Decimal(100),
    )


def test_gate_blocks_after_engine_sets_dd_hard(conn, run_id):
    """エンジンが dd_hard を立てた状態でゲートが block する(受け入れ基準の結合試験)。"""
    _clear_nav(conn)
    _seed_nav(conn, [10_000_000, 7_000_000])  # DD 30% → dd_hard
    run_risk_daily(conn, _run(run_id), as_of=_AS_OF)
    assert _limits_row(conn) == (True, True, False, False)

    _normal_trading_state(conn)
    _, _, result = gate_and_record(
        conn,
        _jp_proposal(),
        nav=Decimal(7_000_000),
        cash=Decimal(3_000_000),
        run_id=run_id,
    )
    assert result.blocked
    assert any(r.rule == "G-10" and "ハードリミット" in r.message for r in result.reasons)


def test_gate_passes_after_committee_release(conn, run_id):
    """委員会解除(release_dd_hard)後は新規建てが通る(dd_soft warn は残る)。"""
    from ryza.risk.state import release_dd_hard

    _clear_nav(conn)
    _seed_nav(conn, [10_000_000, 7_000_000])
    run_risk_daily(conn, _run(run_id), as_of=_AS_OF)
    release_dd_hard(
        conn, "DEMO_FUND", actor="investment_committee", reason="復帰決議", run_id=run_id
    )
    _normal_trading_state(conn)
    _, _, result = gate_and_record(
        conn,
        _jp_proposal(),
        nav=Decimal(7_000_000),
        cash=Decimal(3_000_000),
        run_id=run_id,
    )
    assert not result.blocked  # dd_soft の warn は残ってよい(枠半減は G-7)
