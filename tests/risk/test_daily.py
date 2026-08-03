"""risk.daily の DB 統合テスト: 系列読出・limits_state 更新・レポート・ゲート結合(T-015)。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ryza.gate.orders import gate_and_record
from ryza.ips import IPSConfig
from ryza.risk.daily import (
    build_risk_embed,
    load_instrument_returns,
    load_nav_series,
    load_positions,
    run_risk_daily,
)
from ryza.risk.engine import book_returns

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


def _seed_nav_days(conn, navs: dict, *, book="DEMO_FUND"):
    """日付を明示して NAV を入れる(休日で穴の空いた系列を組むため)。"""
    with conn.cursor() as cur:
        for day, nav in navs.items():
            cur.execute(
                """
                INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
                VALUES (%s, %s, %s, 'provisional', '{}')
                ON CONFLICT (book_id, snap_date)
                DO UPDATE SET nav = EXCLUDED.nav
                """,
                (book, day, Decimal(str(nav))),
            )


def _seed_capital_flow(conn, run_id, *, amount, entry_date, book="DEMO_FUND"):
    """出資仕訳(cash / capital)を直接記帳する(0011 と同型)。

    ``amount`` が負なら払戻(cash 貸方 / capital 借方)として貸借を入れ替える。
    """
    debit_cash, credit_cash = (amount, 0) if amount >= 0 else (0, -amount)
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
            VALUES (%s, 1, %s, 'cash', %s, %s, 'JPY'),
                   (%s, 2, %s, 'capital', %s, %s, 'JPY')
            """,
            (
                entry_id, book, debit_cash, credit_cash,
                entry_id, book, credit_cash, debit_cash,
            ),
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
def _net_flows(conn, book="DEMO_FUND"):
    """``{snap_date: net_flow}``。系列先頭点には seed の開始仕訳(0006/0011 — 2026 年)が
    寄るため、テストは**増分**で判定する(先頭点のフローはリターンに使われない)。"""
    return {p.day: p.net_flow for p in load_nav_series(conn, book).points}


def test_load_nav_series_with_capital_flow(conn, run_id):
    _clear_nav(conn)
    _seed_nav(conn, [1_000_000, 2_000_000], start=date(2030, 1, 4))
    before = _net_flows(conn)
    _seed_capital_flow(conn, run_id, amount=1_000_000, entry_date=date(2030, 1, 5))
    series = load_nav_series(conn, "DEMO_FUND").points
    after = _net_flows(conn)
    assert [p.nav for p in series] == [Decimal(1_000_000), Decimal(2_000_000)]
    # 出資はフロー(損益ではない)。当日のスナップショットに載る。
    assert after[date(2030, 1, 5)] - before[date(2030, 1, 5)] == Decimal(1_000_000)
    assert after[date(2030, 1, 4)] == before[date(2030, 1, 4)]


def test_load_nav_series_rolls_forward_holiday_flow(conn, run_id):
    """休日(スナップショット無し)に付いた出資が次の測定日に載る(重要-5 の回帰)。

    修正前は entry_date 完全一致で結合していたため 1/3 の出資が net_flow に載らず、
    1/2 → 1/5 のリターンが +50%(実際の運用損益は 0%)になっていた。
    """
    _clear_nav(conn)
    _seed_nav_days(conn, {date(2030, 1, 2): 1_000_000, date(2030, 1, 5): 1_500_000})
    _seed_capital_flow(conn, run_id, amount=500_000, entry_date=date(2030, 1, 3))
    series = load_nav_series(conn, "DEMO_FUND").points
    assert [p.day for p in series] == [date(2030, 1, 2), date(2030, 1, 5)]
    assert series[1].net_flow == Decimal(500_000)  # 土曜の出資が 1/5 に寄る
    assert series[1].flow_bop == Decimal(500_000)  # 区間内の仕訳 → 分母に足す(BOP)
    assert series[1].flow_eop == Decimal(0)
    assert book_returns(series) == [0.0]  # 運用損益はゼロ(修正前は +0.5)


def test_load_nav_series_splits_bop_and_eop(conn, run_id):
    """当日仕訳は EOP、区間内の仕訳は BOP に振り分ける(重要-1)。

    1/2 ¥100万 → 1/3(休日)+¥50万 → 1/5 当日 +¥20万・市場 +5% の想定。
    真値は +5.0%(BOP 50 万は区間の運用元本、EOP 20 万は当日の入金)。
    """
    _clear_nav(conn)
    nav_15 = 1_000_000 + 500_000  # 区間元本
    _seed_nav_days(
        conn,
        {date(2030, 1, 2): 1_000_000, date(2030, 1, 5): int(nav_15 * 1.05) + 200_000},
    )
    _seed_capital_flow(conn, run_id, amount=500_000, entry_date=date(2030, 1, 3))
    _seed_capital_flow(conn, run_id, amount=200_000, entry_date=date(2030, 1, 5))
    series = load_nav_series(conn, "DEMO_FUND").points
    assert series[1].flow_bop == Decimal(500_000)
    assert series[1].flow_eop == Decimal(200_000)
    assert book_returns(series) == [pytest.approx(0.05)]


def test_load_nav_series_rollforward_sums_multiple_flows(conn, run_id):
    """同じスナップショットに寄る複数フロー(出資+払戻)は純額で合算する。"""
    _clear_nav(conn)
    _seed_nav_days(conn, {date(2030, 1, 2): 1_000_000, date(2030, 1, 5): 1_300_000})
    _seed_capital_flow(conn, run_id, amount=500_000, entry_date=date(2030, 1, 3))
    _seed_capital_flow(conn, run_id, amount=-200_000, entry_date=date(2030, 1, 4))
    series = load_nav_series(conn, "DEMO_FUND").points
    assert series[1].net_flow == Decimal(300_000)
    assert book_returns(series) == [0.0]


def test_load_nav_series_flow_before_series_start(conn, run_id):
    """系列先頭より前のフローは先頭点に寄る(先頭 NAV が既に含む — リターンに影響しない)。"""
    _clear_nav(conn)
    _seed_nav_days(conn, {date(2030, 1, 5): 1_000_000, date(2030, 1, 6): 1_100_000})
    before = _net_flows(conn)
    _seed_capital_flow(conn, run_id, amount=400_000, entry_date=date(2030, 1, 1))
    loaded = load_nav_series(conn, "DEMO_FUND")
    assert loaded.points[0].net_flow - before[date(2030, 1, 5)] == Decimal(400_000)
    assert loaded.pending_flows == ()  # 先頭より前でもスナップショットはある → 未反映ではない
    assert book_returns(loaded.points) == [pytest.approx(0.1)]


def test_load_nav_series_pending_flow_after_last_snapshot(conn, run_id):
    """系列最終日より後のフローは捨てず pending として返す(黙って落とさない)。"""
    _clear_nav(conn)
    _seed_nav_days(conn, {date(2030, 1, 2): 1_000_000, date(2030, 1, 5): 1_000_000})
    before = _net_flows(conn)
    _seed_capital_flow(conn, run_id, amount=500_000, entry_date=date(2030, 1, 6))
    loaded = load_nav_series(conn, "DEMO_FUND")
    assert {p.day: p.net_flow for p in loaded.points} == before  # 点は変わらない
    assert [(p.entry_date, p.amount) for p in loaded.pending_flows] == [
        (date(2030, 1, 6), Decimal(500_000))
    ]


def test_run_risk_daily_reports_pending_flow(conn, run_id):
    """未反映フローは独立フィールドで注記し、締めを跨いでいれば urgent(重要-4・中-5)。"""
    _clear_nav(conn)
    _seed_nav_days(conn, {date(2030, 1, 2): 1_000_000, date(2030, 1, 5): 1_000_000})
    _seed_capital_flow(conn, run_id, amount=500_000, entry_date=date(2030, 1, 6))
    detail = run_risk_daily(conn, _run(run_id), as_of=_AS_OF)  # 測定日 2030-02-01
    assert detail["DEMO_FUND"]["pending_flows"] == 1
    assert detail["DEMO_FUND"]["pending_urgent"] is True
    embed, urgent = _reports(conn)[0]
    assert urgent is True
    pend = [f for f in embed["fields"] if f["name"] == "未反映フロー"]
    assert pend and "スナップショット未生成の外部フロー" in pend[0]["value"]
    assert "締めを跨いだ" in pend[0]["value"]
    notes = [f for f in embed["fields"] if f["name"] == "注記"]
    assert notes and notes[0]["value"].startswith("【要確認】")  # 注記の先頭(切られない)


def test_run_risk_daily_pending_same_day_immaterial_is_not_urgent(conn, run_id):
    """当日仕訳・NAV 比 0.5% 未満の未反映フローは注記のみ(毎日 urgent にしない)。"""
    _clear_nav(conn)
    _seed_nav_days(conn, {date(2030, 1, 2): 1_000_000, date(2030, 1, 5): 1_000_000})
    _seed_capital_flow(conn, run_id, amount=1_000, entry_date=date(2030, 1, 6))  # 0.1%
    as_of = datetime(2030, 1, 6, 12, 0, tzinfo=UTC)  # JST でも 1/6(締め前)
    detail = run_risk_daily(conn, _run(run_id), as_of=as_of)
    assert detail["DEMO_FUND"]["pending_flows"] == 1
    assert detail["DEMO_FUND"]["pending_urgent"] is False
    embed, urgent = _reports(conn)[0]
    assert urgent is False
    assert [f for f in embed["fields"] if f["name"] == "未反映フロー"]  # 注記は必ず出す


def test_run_risk_daily_pending_same_day_material_is_urgent(conn, run_id):
    """当日でも NAV 比 0.5% 以上なら urgent(材料性しきい値)。"""
    _clear_nav(conn)
    _seed_nav_days(conn, {date(2030, 1, 2): 1_000_000, date(2030, 1, 5): 1_000_000})
    _seed_capital_flow(conn, run_id, amount=50_000, entry_date=date(2030, 1, 6))  # 5%
    as_of = datetime(2030, 1, 6, 12, 0, tzinfo=UTC)
    detail = run_risk_daily(conn, _run(run_id), as_of=as_of)
    assert detail["DEMO_FUND"]["pending_urgent"] is True
    _, urgent = _reports(conn)[0]
    assert urgent is True


def test_build_risk_embed_pending_note_survives_note_truncation():
    """注記欄が 1024 字で切られても未反映フローの理由は消えない(重要-4)。"""
    state = SimpleNamespace(
        drawdown=Decimal("0.01"),
        nav=Decimal(1_000_000),
        peak_nav=Decimal(1_010_000),
        ewma_vol_annual=0.1,
        es95=SimpleNamespace(adopted=0.01, historical=0.01, parametric=0.005),
        n_returns=30,
        as_of_day=date(2030, 1, 5),
        notes=tuple(f"注記{i} " + "あ" * 200 for i in range(20)),  # 1024 字を大きく超える
    )
    embed = build_risk_embed(
        "DEMO_FUND",
        state,
        {"dd_soft": False},
        {},
        IPSConfig.load(),
        as_of=_AS_OF,
        pending_note="NAV スナップショット未生成の外部フロー 1 件: 2030-01-06 出資 ¥500,000",
    )
    notes = [f for f in embed["fields"] if f["name"] == "注記"][0]
    assert len(notes["value"]) == 1024  # 切り詰めは起きている
    assert "スナップショット未生成" not in notes["value"]  # そこには残っていない
    pend = [f for f in embed["fields"] if f["name"] == "未反映フロー"][0]
    assert "2030-01-06 出資 ¥500,000" in pend["value"]  # 独立フィールドには残る


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
