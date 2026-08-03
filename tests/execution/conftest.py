"""execution テスト共通フィクスチャ(T-016)。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行し、commit せず
rollback で隔離する(gate テストと同じ流儀)。orders 行は ``gate_and_record`` が
唯一の入口のため、通過注文はゲート経由で作る — 取引状態・リスク状態のフィクスチャを
ここで用意する。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ryza.db.conn import connect
from ryza.execution.config import ExecutionConfig, FeeSpec, SlippageSpec
from ryza.gate.compliance import OrderProposal
from ryza.gate.orders import gate_and_record
from ryza.ledger import create_run

JST = ZoneInfo("Asia/Tokyo")

# ゲートに渡す判定用の状態(評価エンジン T-015 実装前は呼び出し側が渡す規約)。
NAV = Decimal(10_000_000)
CASH = Decimal(9_000_000)


@pytest.fixture
def conn(migrated_db):
    """関数スコープの接続。テストは commit せず rollback して隔離する。"""
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run_id(conn):
    return create_run(conn, "test.execution", params={"task": "T-016"})


@pytest.fixture(autouse=True)
def _normal_trading_state(conn):
    """取引状態を normal に初期化(G-0 は行欠落を fail-closed で block するため)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, updated_by) VALUES ('normal', 'test')
            ON CONFLICT (singleton) DO UPDATE SET state = 'normal', updated_by = 'test'
            """
        )


@pytest.fixture
def limits_row(conn):
    """risk.limits_state の行(全フラグ false)を用意する関数。"""

    def _install(book_id: str = "DEMO_FUND") -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO risk.limits_state
                    (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of)
                VALUES (%s, false, false, false, false, now())
                ON CONFLICT (book_id) DO UPDATE SET as_of = now()
                """,
                (book_id,),
            )

    return _install


def jp_stock_proposal(**overrides) -> OrderProposal:
    """全規則を通過する日本個別株の現物買い(gate テストと同じ基準形)。"""
    defaults = dict(
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
    defaults.update(overrides)
    return OrderProposal(**defaults)


@pytest.fixture
def passed_order(conn, run_id, limits_row):
    """ゲートを通過した注文行を作り order_id を返す関数。"""

    def _make(**overrides) -> int:
        limits_row()
        order_id, _, result = gate_and_record(
            conn, jp_stock_proposal(**overrides), nav=NAV, cash=CASH, run_id=run_id
        )
        assert result.verdict in ("pass", "warn"), result.reasons
        return order_id

    return _make


@pytest.fixture
def insert_bar(conn, run_id):
    """market.bars に日足を 1 本入れる関数(rollback で巻き戻る)。"""

    def _insert(
        instrument_id: int,
        d: date,
        *,
        close: Decimal,
        open_: Decimal | None = None,
        high: Decimal | None = None,
        low: Decimal | None = None,
        volume: Decimal | None = None,
        source: str = "test",
    ) -> None:
        ts = datetime.combine(d, time(0, 0), tzinfo=JST)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market.bars
                    (instrument_id, ts, timeframe, open, high, low, close, volume,
                     source, as_of, run_id)
                VALUES (%s, %s, '1d', %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (instrument_id, ts, open_, high, low, close, volume,
                 source, datetime.now(UTC), run_id),
            )

    return _insert


@pytest.fixture
def today_jst() -> date:
    return datetime.now(UTC).astimezone(JST).date()


def make_test_config(**fee_overrides) -> ExecutionConfig:
    """数値検証用の固定パラメータ(発効 config とは独立に手計算値と突合する)。"""
    fees = {
        "default": FeeSpec(commission_rate=Decimal(0)),
        "equity_jp": FeeSpec(commission_rate=Decimal(0)),
        "equity_us": FeeSpec(
            commission_rate=Decimal("0.001"), min_fee=Decimal(5), max_fee=Decimal(22)
        ),
    }
    fees.update(fee_overrides)
    return ExecutionConfig(
        version="test",
        slippage=SlippageSpec(
            half_spread_bps=Decimal(5),
            impact_coeff_bps=Decimal(140),
            max_bps=Decimal(100),
        ),
        fees=fees,
    )
