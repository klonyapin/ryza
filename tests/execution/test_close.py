"""締め処理(close)の受け入れテスト。

E2E: gate_and_record(pass)→ runner → run_demo_close → risk.nav_daily 更新まで通し。
照合の一致・不一致検出(執行照合+ポジション照合)と provisional/confirmed の判定を
検証する。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from ryza.execution.close import reconcile_executions, run_demo_close
from ryza.execution.demo import DemoBroker
from ryza.execution.runner import run_pending
from ryza.gate.orders import advance_order_status, record_execution
from ryza.ledger import posting
from ryza.risk.engine import book_returns
from ryza.risk.navflow import load_nav_flow_data

from .conftest import JST, make_test_config


def _broker(conn, today) -> DemoBroker:
    return DemoBroker(conn, config=make_test_config(), trade_date=today)


def _fill_one(conn, run_id, passed_order, insert_bar, today):
    """買い 100 株を通し、約定まで済ませる(価格 1000.64・手数料 0)。"""
    insert_bar(1, today, close=Decimal(1000), volume=Decimal(1_000_000))
    order_id = passed_order()
    summary = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today), run_id=run_id
    )
    assert summary["filled"] == 1, summary
    return order_id


def _nav_daily_rows(conn, book_id="DEMO_FUND"):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT nav_date, nav, status, detail FROM risk.nav_daily
            WHERE book_id = %s ORDER BY nav_date
            """,
            (book_id,),
        )
        return cur.fetchall()


# ── E2E: ゲート → 執行 → 締め → NAV 確定 ─────────────────────────────────────
def test_close_end_to_end_confirmed(conn, run_id, passed_order, insert_bar, today_jst):
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    breaks: list[dict] = []
    result = run_demo_close(
        conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id, on_break=breaks.append
    )
    # NAV = 現金 (10,000,000 − 100,064) + 証券時価 (100×1000) = 9,999,936。
    assert result["nav"] == Decimal("9999936.00")
    assert result["status"] == "confirmed"
    assert result["exec_recon"]["matched"] is True
    assert breaks == []

    rows = _nav_daily_rows(conn)
    assert len(rows) == 1
    nav_date, nav, status, detail = rows[0]
    assert (nav_date, Decimal(nav), status) == (today_jst, Decimal("9999936.00"), "confirmed")
    assert detail["exec_recon"]["matched"] is True
    assert detail["positions"] == {"1": "100"}

    # ledger.nav_snapshots(既存 API 経由)も confirmed になっている。
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT nav, status FROM ledger.nav_snapshots
            WHERE book_id = 'DEMO_FUND' AND snap_date = %s
            """,
            (today_jst,),
        )
        snap = cur.fetchone()
    assert (Decimal(snap[0]), snap[1]) == (Decimal("9999936.00"), "confirmed")


# ── 再締め: 審査シナリオ G(締め後の同日出資)─────────────────────────────────
# 独立審査 重要-2(docs/reviews/risk-navflow-rollforward-independent-review.md):
# 締めが走った**後**に同じ日付で立った仕訳は当日のスナップショットに入らない。navflow は
# その仕訳を当日の flow_eop として NAV から引くため、当日に偽の下振れ・翌日に同額の偽の
# 上振れという ±X% の対が立つ。翌営業日の締めが同じ日付を再計算すれば対は消える。
_G_D0 = date(2026, 9, 1)   # シード出資(8/2・8/3)より後の日付を使う
_G_D1 = date(2026, 9, 2)   # この日の締めの後に出資が立つ
_G_D2 = date(2026, 9, 3)   # 翌営業日の締め = 再締めが走る日


def _post_contribution(conn, run_id, day, amount: Decimal) -> None:
    """指定日付で出資を記帳する(navflow が外部フローとして拾う拠出資本勘定)。"""
    posting.post_entry(
        conn,
        book_id="DEMO_FUND",
        entry_date=day,
        description="テスト用の出資(締め後)",
        lines=[
            {"account_id": "cash", "debit": amount, "currency": "JPY"},
            {"account_id": "capital", "credit": amount, "currency": "JPY"},
        ],
        evidence={"kind": "decision", "payload": {"test": "reclose"}, "source": "test"},
        run_id=run_id,
        posted_by="test.execution",
    )


def _returns(conn) -> list[float]:
    """navflow の NAV 点から帳簿リターン(外部フロー調整済み TWR)を測る。"""
    return book_returns(load_nav_flow_data(conn, "DEMO_FUND").points)


def _scenario_g(conn, run_id, *, reclose_business_days: int):
    cfg = make_test_config(reclose_business_days=reclose_business_days)
    for day in (_G_D0, _G_D1):
        run_demo_close(conn, book_id="DEMO_FUND", date=day, run_id=run_id, config=cfg)
    _post_contribution(conn, run_id, _G_D1, Decimal(5_000_000))  # ← 締めの後に立つ
    # この時点で D1 の NAV は出資を含まないのに flow_eop だけ引かれる = 偽の −50%。
    assert _returns(conn) == [-0.5]
    return run_demo_close(conn, book_id="DEMO_FUND", date=_G_D2, run_id=run_id, config=cfg)


def test_reclose_disabled_reproduces_false_return_pair(conn, run_id):
    """再締めなしでは ±50% の偽リターン対が残る(審査が再現した不具合そのもの)。"""
    result = _scenario_g(conn, run_id, reclose_business_days=0)
    assert result["reclose"] == []
    assert _returns(conn) == [-0.5, 0.5]


def test_reclose_resolves_false_return_pair(conn, run_id):
    """再締め(N=3)で D1 の NAV が出資を取り込み、偽リターン対が消える。"""
    result = _scenario_g(conn, run_id, reclose_business_days=3)

    # D1 だけが書き換わる(D0 は出資日より前なので値が変わらない)。
    assert [(r["date"], r["nav_before"], r["nav_after"]) for r in result["reclose"]] == [
        (_G_D1, Decimal(10_000_000), Decimal(15_000_000))
    ]
    assert _returns(conn) == [0.0, 0.0]

    # risk.nav_daily(0016 の risk 用ビュー)も追随し、nav_snapshots と一致する。
    assert result["reclose"][0]["nav_daily_missing"] is False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.nav, s.nav, d.detail -> 'reclose' ->> 'nav_before', d.run_id
            FROM risk.nav_daily d
            JOIN ledger.nav_snapshots s
              ON s.book_id = d.book_id AND s.snap_date = d.nav_date
            WHERE d.book_id = 'DEMO_FUND' AND d.nav_date = %s
            """,
            (_G_D1,),
        )
        nav_daily, nav_snapshot, nav_before, row_run_id = cur.fetchone()
    assert Decimal(nav_daily) == Decimal(nav_snapshot) == Decimal(15_000_000)
    assert Decimal(nav_before) == Decimal(10_000_000)
    assert row_run_id == run_id  # 書き手の run へ差し替わる(リネージ)


def test_reclose_reports_missing_nav_daily_row(conn, run_id):
    """nav_snapshots だけ動いて nav_daily の行が無い日は、合成せず痕跡を返す。"""
    cfg = make_test_config()
    run_demo_close(conn, book_id="DEMO_FUND", date=_G_D1, run_id=run_id, config=cfg)
    with conn.cursor() as cur:  # 執行段を経ていない日を模す
        cur.execute("DELETE FROM risk.nav_daily WHERE nav_date = %s", (_G_D1,))
    _post_contribution(conn, run_id, _G_D1, Decimal(5_000_000))

    result = run_demo_close(
        conn, book_id="DEMO_FUND", date=_G_D2, run_id=run_id, config=cfg
    )
    assert result["reclose"][0]["nav_daily_missing"] is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM risk.nav_daily WHERE nav_date = %s", (_G_D1,))
        assert cur.fetchone()[0] == 0  # 合成しない(執行照合を経ていない confirmed を作らない)


def test_close_no_positions_confirms_seed_nav(conn, run_id, today_jst):
    """注文が無い日も締めは走り、NAV(出資金のみ)を確定する(daily の no-op 経路)。"""
    result = run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    assert result["nav"] == Decimal(10_000_000)
    assert result["status"] == "confirmed"
    rows = _nav_daily_rows(conn)
    assert len(rows) == 1 and rows[0][2] == "confirmed"


def test_close_upsert_same_day(conn, run_id, passed_order, insert_bar, today_jst):
    """同日再締めは上書き(1 行のまま)。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    assert len(_nav_daily_rows(conn)) == 1


# ── 照合: 一致・不一致の検出 ─────────────────────────────────────────────────
def test_reconcile_executions_matched(conn, run_id, passed_order, insert_bar, today_jst):
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    recon = reconcile_executions(conn, book_id="DEMO_FUND", date=today_jst)
    assert recon["matched"] is True
    assert recon["executions"]["count"] == 1 and recon["ledger"]["count"] == 1
    assert recon["executions"]["gross"] == Decimal("100064.00")
    assert recon["ledger"]["gross"] == Decimal("100064.00")


def test_reconcile_detects_unposted_execution(
    conn, run_id, passed_order, insert_bar, today_jst
):
    """ledger 仕訳の無い約定(記帳漏れ)を件数・金額ブレイクとして検出する。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    # 2 件目: record_execution だけ行い post_fill を意図的に飛ばす(記帳漏れの再現)。
    order2 = passed_order(ref_price=Decimal(1000))
    advance_order_status(conn, order2, "submitted")
    record_execution(
        conn, order_id=order2, qty=Decimal(100), price=Decimal(1000),
        executed_at=datetime.combine(today_jst, time(15, 30), tzinfo=JST),
        run_id=run_id,
    )

    breaks: list[dict] = []
    recon = reconcile_executions(
        conn, book_id="DEMO_FUND", date=today_jst, on_break=breaks.append
    )
    assert recon["matched"] is False
    items = {b["item"] for b in recon["breaks"]}
    assert items == {"exec_count", "exec_gross"}  # fee は両側 0 で一致
    assert len(breaks) == 2 and breaks[0]["book_id"] == "DEMO_FUND"


def test_close_break_leaves_nav_provisional(
    conn, run_id, passed_order, insert_bar, today_jst
):
    """照合ブレイク時は risk.nav_daily を provisional に留める(NAV 確定しない)。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    order2 = passed_order(ref_price=Decimal(1000))
    advance_order_status(conn, order2, "submitted")
    record_execution(  # 記帳漏れ + ポジション乖離(executions 側だけ 200 株になる)
        conn, order_id=order2, qty=Decimal(100), price=Decimal(1000),
        executed_at=datetime.combine(today_jst, time(15, 30), tzinfo=JST),
        run_id=run_id,
    )

    breaks: list[dict] = []
    result = run_demo_close(
        conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id, on_break=breaks.append
    )
    assert result["status"] == "provisional"
    assert result["exec_recon"]["matched"] is False
    assert breaks  # 執行照合+ポジション照合の両方から通知が出る
    rows = _nav_daily_rows(conn)
    assert rows[0][2] == "provisional"


def test_close_fails_loudly_without_price(conn, run_id, passed_order, insert_bar, today_jst):
    """保有銘柄の終値が無ければ締めは明確な例外で失敗する(黙ってスキップしない)。"""
    import pytest

    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    with conn.cursor() as cur:  # 当日バーを消して評価不能にする(rollback で巻き戻る)
        cur.execute("DELETE FROM market.bars WHERE instrument_id = 1")
    with pytest.raises(ValueError, match="終値が無い"):
        run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)


def test_reconcile_scopes_by_date_and_book(conn, run_id, passed_order, insert_bar, today_jst):
    """照合は JST 日付と帳簿でスコープされる(他日・他帳簿の約定を混ぜない)。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    from datetime import timedelta

    other_day = today_jst - timedelta(days=1)
    recon = reconcile_executions(conn, book_id="DEMO_FUND", date=other_day)
    assert recon["executions"]["count"] == 0 and recon["ledger"]["count"] == 0
    assert recon["matched"] is True


def test_nav_daily_requires_known_book(conn, run_id, today_jst):
    """risk.nav_daily は ledger.books への FK — 帳簿語彙の外には書けない。"""
    import psycopg
    import pytest

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO risk.nav_daily (book_id, nav_date, nav, status, run_id)
                    VALUES ('NO_SUCH_BOOK', %s, 0, 'provisional', %s)
                    """,
                    (today_jst, run_id),
                )


def test_close_records_mtm_via_ledger_api(conn, run_id, passed_order, insert_bar, today_jst):
    """評価差損益は ledger の期末評価替え(post_mark_to_market)流儀で記帳される。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    with conn.cursor() as cur:
        # 取得 100,064 → 時価 100,000: unrealized_pnl 借方 64(評価損)。
        cur.execute(
            """
            SELECT COALESCE(sum(debit - credit), 0) FROM ledger.journal_lines
            WHERE book_id = 'DEMO_FUND' AND account_id = 'unrealized_pnl'
            """
        )
        assert Decimal(cur.fetchone()[0]) == Decimal("64.00")
        # 証券勘定は時価に一致。
        cur.execute(
            """
            SELECT COALESCE(sum(debit - credit), 0) FROM ledger.journal_lines
            WHERE book_id = 'DEMO_FUND' AND account_id = 'securities'
            """
        )
        assert Decimal(cur.fetchone()[0]) == Decimal("100000.00")


def test_close_uses_utc_now_not_needed(conn, run_id, today_jst):
    """(回帰)締めは date 引数のみに依存し、実行時刻に依存しない。"""
    r1 = run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    r2 = run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    assert r1["nav"] == r2["nav"] and r1["status"] == r2["status"]
