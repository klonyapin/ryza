"""DemoBroker — market.bars の日足で約定をシミュレートする決定論ブローカー(T-016)。

乱数を使わない完全決定論(同じ入力なら同じ約定)。執行層に確率的攪乱を持ち込むと
E5(複数シード)の評価対象が執行層まで広がってしまうため。価格・手数料モデルの
パラメータと根拠は ``config/execution.yaml`` のコメントが正(ハードコード禁止)。

- market 注文: 直近終値 ×(1 ± スリッページ率)。率(bps)= 半スプレッド +
  インパクト係数×√参加率(注文代金/当日売買代金)。丸めは不利方向
  (買いは切り上げ・売りは切り捨て = コスト保守側)
- limit 注文: 当日バーで約定判定 — 寄りで指値より有利なら寄り値、ザラ場で指値到達
  (買い: low ≤ 指値 / 売り: high ≥ 指値)なら指値。達しなければ ``expired``
  (guaranteed fill はしない — 指示書)
- 手数料: 資産クラス別の率(min/max クランプ・切り上げ)
- バーが無い銘柄は ``rejected``(理由付き)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

import psycopg

from ryza.execution.broker import EXPIRED, FILLED, REJECTED, BrokerOrder, BrokerResult
from ryza.execution.config import ExecutionConfig, FeeSpec

_JST = ZoneInfo("Asia/Tokyo")

# 約定時刻は東証の大引け 15:30 JST に固定する(決定論)。2024-11-05 の取引時間拡大以降、
# 東証の立会終了は 15:30(JPX「売買立会時間の拡大」)。米国銘柄も JST 日付の帳簿日に
# 寄せる(基準通貨 JPY・JST 日次締めの簡略化 — MVP の割り切り。実ブローカー統合時に再訪)。
_CLOSE_TIME = time(15, 30)

# 価格・手数料の丸め粒度。日本株の呼値は銘柄・価格帯で異なるが、デモは一律 0.01 とする
# (呼値テーブルの再現は実ブローカー統合時)。
_QUANTUM = Decimal("0.01")

# 買い方向(約定価格が上に滑る side)。
_BUY_SIDES = frozenset({"buy", "cover"})


@dataclass(frozen=True)
class DailyBar:
    """直近日足のスナップショット。"""

    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: Decimal | None
    ts: datetime


def latest_daily_bar(
    conn: psycopg.Connection, instrument_id: int, on_or_before: date
) -> DailyBar | None:
    """JST 日付 ``on_or_before`` 以前で最新の日足(close 必須)。無ければ None。

    同一バーの改定は as_of が最新の行を採る。執行は「今知っている最新値」で行う
    ライブ経路であり、バックテスト(point-in-time 固定)とは規約が異なる。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open, high, low, close, volume, ts
            FROM market.bars
            WHERE instrument_id = %s AND timeframe = '1d' AND close IS NOT NULL
              AND (ts AT TIME ZONE 'Asia/Tokyo')::date <= %s
            ORDER BY ts DESC, as_of DESC
            LIMIT 1
            """,
            (instrument_id, on_or_before),
        )
        row = cur.fetchone()
    if row is None:
        return None
    o, h, lo, c, v, ts = row
    return DailyBar(
        open=None if o is None else Decimal(o),
        high=None if h is None else Decimal(h),
        low=None if lo is None else Decimal(lo),
        close=Decimal(c),
        volume=None if v is None else Decimal(v),
        ts=ts,
    )


def latest_close(
    conn: psycopg.Connection, instrument_id: int, on_or_before: date
) -> Decimal | None:
    """直近日足の終値(締め処理の評価に使う)。無ければ None。"""
    bar = latest_daily_bar(conn, instrument_id, on_or_before)
    return None if bar is None else bar.close


def close_on(
    conn: psycopg.Connection, instrument_id: int, day: date
) -> Decimal | None:
    """**その日ちょうど**の日足終値。その日のバーが無ければ None(遡らない)。

    ``latest_close``(on_or_before)との違いは意図的である(独立審査 新-6): 過去日の
    評価替えを打ち直す再締めが遡り取得を使うと、別日の終値でその日を評価しながら
    ``priced_at`` にはその日を書く**虚偽の証憑**ができ、しかも当該日は以後 stale では
    ないため誤価格が恒久固定される。過去日の再評価は「その日のバーがある」ときだけ
    行い、無い日は再適用しない(全か無かの門を『価格の有無』でなく『その日のバーの
    有無』で切る)。ライブの執行・当日の締めは従来どおり遡り取得でよい — あちらは
    「今知っている最新値」で建てる規約だからである。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close FROM market.bars
            WHERE instrument_id = %s AND timeframe = '1d' AND close IS NOT NULL
              AND (ts AT TIME ZONE 'Asia/Tokyo')::date = %s
            ORDER BY as_of DESC
            LIMIT 1
            """,
            (instrument_id, day),
        )
        row = cur.fetchone()
    return None if row is None else Decimal(row[0])


class DemoBroker:
    """デモ市場(market.bars の日足)に対する ``Broker`` 実装。DB は読み取りのみ。"""

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        config: ExecutionConfig,
        trade_date: date | None = None,
    ) -> None:
        self._conn = conn
        self._config = config
        self._trade_date = trade_date or datetime.now(UTC).astimezone(_JST).date()

    @property
    def trade_date(self) -> date:
        return self._trade_date

    # ── Broker Protocol ──────────────────────────────────────────────────────
    def submit(self, order: BrokerOrder) -> BrokerResult:
        bar = latest_daily_bar(self._conn, order.instrument_id, self._trade_date)
        if bar is None:
            return BrokerResult(
                status=REJECTED,
                reason=(
                    f"market.bars に日足が無い(instrument_id={order.instrument_id}, "
                    f"~{self._trade_date.isoformat()})"
                ),
            )

        if order.order_type == "market":
            price = self._market_price(order, bar)
        elif order.order_type == "limit":
            if order.limit_price is None:  # スキーマ CHECK 上あり得ないが防御
                return BrokerResult(status=REJECTED, reason="limit 注文に limit_price が無い")
            limit_fill = self._limit_price(order, bar)
            if limit_fill is None:
                return BrokerResult(
                    status=EXPIRED,
                    reason=(
                        f"指値 {order.limit_price} に当日バー"
                        f"(O={bar.open} H={bar.high} L={bar.low})が達しなかった"
                    ),
                )
            price = limit_fill
        else:
            return BrokerResult(
                status=REJECTED, reason=f"未知の order_type: {order.order_type!r}"
            )

        fee = self._fee(self._config.fee_for(order.asset_class), order.qty * price)
        return BrokerResult(
            status=FILLED,
            qty=order.qty,
            price=price,
            fee=fee,
            executed_at=datetime.combine(self._trade_date, _CLOSE_TIME, tzinfo=_JST),
            venue="demo",
            broker_ref=f"demo:{order.order_id}:{self._trade_date.isoformat()}",
        )

    # ── 価格モデル ───────────────────────────────────────────────────────────
    def _slippage_rate(self, order_value: Decimal, daily_value: Decimal | None) -> Decimal:
        """スリッページ率(比率)。当日売買代金が不明なら上限を適用(保守側)。"""
        s = self._config.slippage
        if daily_value is None or daily_value <= 0:
            bps = s.max_bps
        else:
            participation = order_value / daily_value
            bps = s.half_spread_bps + s.impact_coeff_bps * participation.sqrt()
            bps = min(bps, s.max_bps)
        return bps / Decimal(10_000)

    def _market_price(self, order: BrokerOrder, bar: DailyBar) -> Decimal:
        """成行: 終値 ×(1 ± スリッページ率)。丸めは不利方向。"""
        buy_side = order.side in _BUY_SIDES
        daily_value = None if bar.volume is None else bar.close * bar.volume
        rate = self._slippage_rate(order.qty * bar.close, daily_value)
        raw = bar.close * ((1 + rate) if buy_side else (1 - rate))
        return raw.quantize(_QUANTUM, rounding=ROUND_CEILING if buy_side else ROUND_FLOOR)

    def _limit_price(self, order: BrokerOrder, bar: DailyBar) -> Decimal | None:
        """指値: 寄りで有利なら寄り値、ザラ場タッチなら指値。達しなければ None。"""
        limit = order.limit_price
        assert limit is not None  # 呼び出し側で検証済み
        if order.side in _BUY_SIDES:
            if bar.open is not None and bar.open <= limit:
                return bar.open
            if bar.low is not None and bar.low <= limit:
                return limit
            return None
        if bar.open is not None and bar.open >= limit:
            return bar.open
        if bar.high is not None and bar.high >= limit:
            return limit
        return None

    # ── 手数料 ───────────────────────────────────────────────────────────────
    @staticmethod
    def _fee(spec: FeeSpec, gross: Decimal) -> Decimal:
        """fee = clamp(rate×約定代金, min_fee, max_fee)。切り上げ(コスト保守側)。"""
        fee = gross * spec.commission_rate
        if spec.max_fee is not None:
            fee = min(fee, spec.max_fee)
        fee = max(fee, spec.min_fee)
        return fee.quantize(_QUANTUM, rounding=ROUND_CEILING)


__all__ = ["DailyBar", "DemoBroker", "close_on", "latest_close", "latest_daily_bar"]
