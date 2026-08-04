"""F-12 事後監視: 約定ベース売買代金の G-7 上限跨ぎ検知。

``turnover_breach_after_execution`` はゲート判定時に固定した NAV(gate_log.state_ref)
を上限計算に使い、当該約定を含めた累計が上限を跨いだ瞬間だけを ``TurnoverBreach`` で
返す(エッジトリガ — 超過継続中の追加約定では鳴らさない)。NAV が取れない異常系は
fail-closed で urgent 側に倒す。テストは commit せず rollback で隔離する。

累計側のデータは ``trading.executions`` を SQL で直接複数投入して作る:
``record_execution`` は「累計約定 ≤ 注文数量」制約を持つため、G-3/G-8 の集中度・
レバ制約を通しつつ NAV×30% を跨ぐような大きな累計を単一注文経由で作れないため。
検知ヘルパは executions の合算式のみを見るので、直接 INSERT でも意味論は同一。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ryza.gate.orders import gate_and_record, turnover_breach_after_execution
from ryza.ips import load_and_validate

from .conftest import jp_stock_proposal

_JST = ZoneInfo("Asia/Tokyo")
_NAV = Decimal(10_000_000)
_CASH = Decimal(5_000_000)


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


@pytest.fixture(scope="session")
def turnover_limit_frac():
    """発効 IPS の daily_turnover_nav_max(NAV 比)を取得。"""
    ips, _ = load_and_validate()
    return Decimal(str(ips.hard_limits.daily_turnover_nav_max))


def _pass_order(conn, run_id, *, qty=Decimal(10)) -> int:
    """ゲート通過注文を作り order_id を返す(累計は SQL で別途積む)。"""
    order_id, _, result = gate_and_record(
        conn,
        jp_stock_proposal(qty=qty, ref_price=Decimal(1000)),
        nav=_NAV,
        cash=_CASH,
        run_id=run_id,
    )
    assert result.verdict in ("pass", "warn"), result.reasons
    return order_id


def _insert_execution(
    conn, *, order_id: int, qty: Decimal, price: Decimal, run_id: int, executed_at=None
) -> int:
    """``trading.executions`` に直接 INSERT して execution_id を返す。

    検知ヘルパは executions 側の合算のみを見る(注文の累積制約は見ない)ため、
    テスト用に累計を作るのは直接 INSERT で十分。JST 大引け相当を既定時刻とする。
    """
    if executed_at is None:
        today = datetime.now(UTC).astimezone(_JST).date()
        executed_at = datetime.combine(today, time(15, 30), tzinfo=_JST).astimezone(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trading.executions
                (order_id, qty, price, fee, executed_at, venue, broker_ref, run_id)
            VALUES (%s, %s, %s, 0, %s, 'demo', NULL, %s)
            RETURNING id
            """,
            (order_id, qty, price, executed_at, run_id),
        )
        return cur.fetchone()[0]


def test_edge_trigger_fires_once_on_crossing(conn, run_id, limits_row, turnover_limit_frac):
    """(a) before ≤ limit < after のエッジで検知1件、通知フィールドに数値が入る。"""
    limits_row()
    limit = turnover_limit_frac * _NAV  # NAV × 30% = ¥3,000,000
    order_id = _pass_order(conn, run_id)
    # 1件目: 上限手前まで積む(枠内)。
    first_notional = limit - Decimal(100_000)
    exec1 = _insert_execution(
        conn, order_id=order_id, qty=Decimal(1000),
        price=first_notional / Decimal(1000), run_id=run_id,
    )
    assert turnover_breach_after_execution(conn, exec1) is None  # まだ枠内
    # 2件目: 上限を跨ぐ(¥200,000 追加で¥3,100,000)。
    exec2 = _insert_execution(
        conn, order_id=order_id, qty=Decimal(200), price=Decimal(1000), run_id=run_id,
    )
    breach = turnover_breach_after_execution(conn, exec2)
    assert breach is not None
    assert breach.execution_id == exec2
    assert breach.before == first_notional
    assert breach.after == first_notional + Decimal(200_000)
    assert breach.limit == limit
    assert breach.nav == _NAV
    assert breach.nav_missing_reason is None
    assert breach.nav_source_gate_log_id is not None
    assert breach.book_id == "DEMO_FUND"


def test_no_alarm_while_already_over_limit(conn, run_id, limits_row, turnover_limit_frac):
    """(b) 既に超過中の追加約定では鳴らさない(after > limit だが before > limit)。

    ゲートは注文時価格で判定するため、事後累計が既に上限を超えている状態でも新規注文
    自体は「注文時基準では枠内」の局面がありうる(スリッページ次第)。エッジトリガの
    継続鳴動抑止(通知が毎回赤だと意味を失う)を固定する。
    """
    limits_row()
    limit = turnover_limit_frac * _NAV
    order_id = _pass_order(conn, run_id)
    # 1件目で一気に上限を跨がせる(before=0, after > limit)。
    exec1 = _insert_execution(
        conn, order_id=order_id, qty=Decimal(4000), price=Decimal(1000), run_id=run_id,
    )
    first_breach = turnover_breach_after_execution(conn, exec1)
    assert first_breach is not None  # 最初の跨ぎは鳴る
    assert first_breach.after > limit
    # 追加約定: before は既に上限超え → after も上限超えだが「跨ぎ」ではない。
    exec2 = _insert_execution(
        conn, order_id=order_id, qty=Decimal(100), price=Decimal(1000), run_id=run_id,
    )
    assert turnover_breach_after_execution(conn, exec2) is None


def test_within_limit_returns_none(conn, run_id, limits_row):
    """(c) after ≤ limit の枠内なら None(通知しない)。"""
    limits_row()
    order_id = _pass_order(conn, run_id)
    execution_id = _insert_execution(
        conn, order_id=order_id, qty=Decimal(100), price=Decimal(1000), run_id=run_id,
    )
    assert turnover_breach_after_execution(conn, execution_id) is None


def test_fail_closed_when_gate_log_state_ref_lacks_nav(conn, run_id, limits_row):
    """(d) gate_log.state_ref から NAV を取れない異常系は fail-closed で TurnoverBreach を返す。

    ヘルパが呼び出し側にイベントを渡さない=通知が消える経路を作らないための不変性。
    実運用では state_ref に nav は必ず入るが、ここでは検知の fail-closed 姿勢を固定する。
    """
    limits_row()
    order_id = _pass_order(conn, run_id)
    execution_id = _insert_execution(
        conn, order_id=order_id, qty=Decimal(100), price=Decimal(1000), run_id=run_id,
    )
    # gate_log は追記オンリー(監査証跡)だが、fail-closed 経路の検証にはスナップ
    # ショットの nav 欠落を作る必要がある。**この1テストに限り**追記オンリーの
    # BEFORE UPDATE トリガを一時的に外して state_ref から nav キーを取り除く
    # (DDL はトランザクション内で巻き戻る)。実装が実運用でこの経路に到達する
    # のは apply_execution が「gate_log を持たない約定」を弾く前段で state_ref が
    # 壊れている異常系のみで、そのとき fail-closed で TurnoverBreach を返すことを
    # 固定する。
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE compliance.gate_log DISABLE TRIGGER gate_log_no_mutation"
        )
        cur.execute(
            """
            UPDATE compliance.gate_log g
            SET state_ref = g.state_ref - 'nav'
            FROM trading.orders o
            WHERE o.id = %s AND g.id = o.gate_log_id
            """,
            (order_id,),
        )
        cur.execute(
            "ALTER TABLE compliance.gate_log ENABLE TRIGGER gate_log_no_mutation"
        )
    breach = turnover_breach_after_execution(conn, execution_id)
    assert breach is not None
    assert breach.nav is None
    assert breach.nav_missing_reason and "nav" in breach.nav_missing_reason
    assert breach.after == Decimal(100_000)  # 累計は取れている(判定不能なのは上限)


def test_trade_date_scoped_by_jst(conn, run_id, limits_row, turnover_limit_frac):
    """JST 日付境界: 前日の約定は当日累計に入らない(``_daily_turnover`` と同じ規約)。"""
    limits_row()
    order_id = _pass_order(conn, run_id)
    yesterday = datetime.now(UTC).astimezone(_JST).date() - timedelta(days=1)
    yesterday_close = datetime.combine(yesterday, time(15, 30), tzinfo=_JST).astimezone(UTC)

    # 前日約定で上限相当を積む(当日の累計には入らない)。
    limit = turnover_limit_frac * _NAV
    _insert_execution(
        conn, order_id=order_id, qty=Decimal(3000),
        price=limit / Decimal(3000), run_id=run_id, executed_at=yesterday_close,
    )
    # 当日約定: 単独で枠内(¥100,000)。前日分を含めれば超過だが、日付で切られる。
    exec_today = _insert_execution(
        conn, order_id=order_id, qty=Decimal(100), price=Decimal(1000), run_id=run_id,
    )
    breach = turnover_breach_after_execution(conn, exec_today)
    assert breach is None  # 当日累計は¥100,000 のみ → 枠内
