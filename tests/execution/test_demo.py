"""DemoBroker の数値検証(スリッページ・limit 判定・手数料)。

パラメータはテスト固定値(``make_test_config``)を使い、手計算値と突合する:
slippage_rate(bps) = 5 + 140×√(注文代金/当日売買代金)、上限 100bps。
"""

from __future__ import annotations

from decimal import Decimal

from ryza.execution.broker import EXPIRED, FILLED, REJECTED, BrokerOrder
from ryza.execution.demo import DemoBroker, latest_close

from .conftest import make_test_config


def _order(**overrides) -> BrokerOrder:
    defaults = dict(
        order_id=1,
        book_id="DEMO_FUND",
        fm="ben",
        instrument_id=901,
        side="buy",
        qty=Decimal(100),
        order_type="market",
        asset_class="equity_jp",
    )
    defaults.update(overrides)
    return BrokerOrder(**defaults)


def _broker(conn, today) -> DemoBroker:
    return DemoBroker(conn, config=make_test_config(), trade_date=today)


# ── 成行: スリッページの数値検証 ──────────────────────────────────────────────
def test_market_buy_slippage(conn, insert_bar, today_jst):
    # 終値 1000・出来高 10,000 株 → 売買代金 10,000,000。注文 100 株 → 参加率 0.01。
    # rate = 5 + 140×√0.01 = 19bps → 価格 = 1000×1.0019 = 1001.90(切り上げ方向)。
    insert_bar(901, today_jst, close=Decimal(1000), volume=Decimal(10_000))
    result = _broker(conn, today_jst).submit(_order())
    assert result.status == FILLED
    assert result.qty == Decimal(100)
    assert result.price == Decimal("1001.90")
    assert result.fee == Decimal(0)  # equity_jp はゼロ
    assert result.executed_at is not None and result.venue == "demo"
    assert result.broker_ref == f"demo:1:{today_jst.isoformat()}"


def test_market_sell_slippage_rounds_down(conn, insert_bar, today_jst):
    # 売りは 1000×(1−0.0019) = 998.10(切り捨て方向 = 不利側)。
    insert_bar(901, today_jst, close=Decimal(1000), volume=Decimal(10_000))
    result = _broker(conn, today_jst).submit(_order(side="sell"))
    assert result.status == FILLED
    assert result.price == Decimal("998.10")


def test_market_no_volume_uses_cap(conn, insert_bar, today_jst):
    # 出来高欠損 → 上限 100bps を適用(保守側)。買い = 1000×1.01 = 1010.00。
    insert_bar(901, today_jst, close=Decimal(1000), volume=None)
    result = _broker(conn, today_jst).submit(_order())
    assert result.status == FILLED
    assert result.price == Decimal("1010.00")


def test_market_slippage_capped(conn, insert_bar, today_jst):
    # 参加率 4(注文が当日売買代金の 4 倍)→ 5+140×2 = 285bps → 100bps でキャップ。
    insert_bar(901, today_jst, close=Decimal(1000), volume=Decimal(1_000))
    result = _broker(conn, today_jst).submit(_order(qty=Decimal(4_000)))
    assert result.status == FILLED
    assert result.price == Decimal("1010.00")


def test_cover_slips_up_short_slips_down(conn, insert_bar, today_jst):
    # cover は買い方向(+)、short は売り方向(−)に滑る。
    insert_bar(901, today_jst, close=Decimal(1000), volume=Decimal(10_000))
    broker = _broker(conn, today_jst)
    assert broker.submit(_order(side="cover")).price == Decimal("1001.90")
    assert broker.submit(_order(side="short")).price == Decimal("998.10")


# ── limit: 約定判定 ──────────────────────────────────────────────────────────
def test_limit_buy_touch_fills_at_limit(conn, insert_bar, today_jst):
    insert_bar(901, today_jst, close=Decimal(1000), open_=Decimal(1005),
               high=Decimal(1010), low=Decimal(990), volume=Decimal(10_000))
    result = _broker(conn, today_jst).submit(
        _order(order_type="limit", limit_price=Decimal(995))
    )
    assert result.status == FILLED
    assert result.price == Decimal(995)  # ザラ場タッチ → 指値


def test_limit_buy_gap_down_fills_at_open(conn, insert_bar, today_jst):
    insert_bar(901, today_jst, close=Decimal(1000), open_=Decimal(980),
               high=Decimal(1010), low=Decimal(975), volume=Decimal(10_000))
    result = _broker(conn, today_jst).submit(
        _order(order_type="limit", limit_price=Decimal(995))
    )
    assert result.status == FILLED
    assert result.price == Decimal(980)  # 寄りが指値より有利 → 寄り値


def test_limit_buy_not_touched_expires(conn, insert_bar, today_jst):
    insert_bar(901, today_jst, close=Decimal(1000), open_=Decimal(1005),
               high=Decimal(1010), low=Decimal(990), volume=Decimal(10_000))
    result = _broker(conn, today_jst).submit(
        _order(order_type="limit", limit_price=Decimal(985))
    )
    assert result.status == EXPIRED
    assert result.reason and "985" in result.reason


def test_limit_sell_touch_fills_at_limit(conn, insert_bar, today_jst):
    insert_bar(901, today_jst, close=Decimal(1000), open_=Decimal(995),
               high=Decimal(1008), low=Decimal(990), volume=Decimal(10_000))
    result = _broker(conn, today_jst).submit(
        _order(side="sell", order_type="limit", limit_price=Decimal(1005))
    )
    assert result.status == FILLED
    assert result.price == Decimal(1005)


# ── 手数料 ───────────────────────────────────────────────────────────────────
def test_fee_rate_and_max_clamp(conn, insert_bar, today_jst):
    # equity_us: rate 0.1%・min 5・max 22(テスト固定値)。出来高欠損 → 価格 1010。
    # gross = 100×1010 = 101,000 → fee 101 → max 22 にクランプ。
    insert_bar(902, today_jst, close=Decimal(1000), volume=None)
    result = _broker(conn, today_jst).submit(
        _order(instrument_id=902, asset_class="equity_us")
    )
    assert result.fee == Decimal(22)


def test_fee_min_clamp(conn, insert_bar, today_jst):
    # qty 1 → gross 1010 → fee 1.01 → min 5 に引き上げ。
    insert_bar(902, today_jst, close=Decimal(1000), volume=None)
    result = _broker(conn, today_jst).submit(
        _order(instrument_id=902, asset_class="equity_us", qty=Decimal(1))
    )
    assert result.fee == Decimal(5)


def test_unknown_asset_class_uses_default_fee(conn, insert_bar, today_jst):
    insert_bar(903, today_jst, close=Decimal(1000), volume=Decimal(10_000))
    result = _broker(conn, today_jst).submit(
        _order(instrument_id=903, asset_class="mystery")
    )
    assert result.status == FILLED
    assert result.fee == Decimal(0)  # テスト固定値の default はゼロ


# ── バー欠落・データ取得 ─────────────────────────────────────────────────────
def test_no_bar_rejected(conn, today_jst):
    result = _broker(conn, today_jst).submit(_order(instrument_id=999_999))
    assert result.status == REJECTED
    assert result.reason and "999999" in result.reason


def test_latest_bar_ignores_future_dates(conn, insert_bar, today_jst):
    """trade_date より未来のバーは見ない(前日付の締めで当日バーを混入させない)。"""
    from datetime import timedelta

    yesterday = today_jst - timedelta(days=1)
    insert_bar(904, yesterday, close=Decimal(500), volume=Decimal(10_000))
    insert_bar(904, today_jst, close=Decimal(999), volume=Decimal(10_000))
    assert latest_close(conn, 904, yesterday) == Decimal(500)
    assert latest_close(conn, 904, today_jst) == Decimal(999)
