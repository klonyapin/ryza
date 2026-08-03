"""fm テスト共通フィクスチャ(T-017)。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行し、commit せず
rollback で隔離する(gate/execution テストと同じ流儀)。**LLM は実プロバイダを
呼ばない** — Ben のテストは ``FixtureProvider`` を注入する。

シグナル・サイジングの純ロジックは DB 不要だが、提案の記録(point-in-time 検証)と
ゲート連携は DB を要するため、同じ conftest に両方の道具を置く。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ryza.db.conn import connect
from ryza.provenance import start_run
from ryza.risk.classify import Classification, upsert_classification

JST = ZoneInfo("Asia/Tokyo")

BOOK = "DEMO_FUND"


@pytest.fixture
def conn(migrated_db):
    """関数スコープの接続。テストは commit せず rollback して隔離する。"""
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture(autouse=True)
def _isolate_market(conn):
    """並行ワークツリーの残留を不可視にする(トランザクション内 DELETE・rollback で復元)。

    ユニバース走査は分類テーブル全体を読むため、残留行があると件数 assert が壊れる。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM market.instrument_classification")
        cur.execute("DELETE FROM market.bars")


@pytest.fixture
def run(conn):
    return start_run("test.fm", {"task": "T-017"}, conn=conn)


@pytest.fixture
def as_of() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(autouse=True)
def _normal_trading_state(conn):
    """取引状態 normal + リスク状態(全フラグ false)。ゲートは行欠落を block するため。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, updated_by) VALUES ('normal', 'test')
            ON CONFLICT (singleton) DO UPDATE SET state = 'normal', updated_by = 'test'
            """
        )
        cur.execute(
            """
            INSERT INTO risk.limits_state
                (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of)
            VALUES (%s, false, false, false, false, now())
            ON CONFLICT (book_id) DO UPDATE SET as_of = now()
            """,
            (BOOK,),
        )


@pytest.fixture
def nav_snapshot(conn):
    """``ledger.nav_snapshots`` に NAV を1行入れる(FM はここから NAV を読む)。"""

    def _install(nav: Decimal = Decimal(10_000_000), *, day: date | None = None) -> None:
        day = day or datetime.now(UTC).astimezone(JST).date()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
                VALUES (%s, %s, %s, 'confirmed', '{}'::jsonb)
                ON CONFLICT (book_id, snap_date) DO UPDATE SET nav = EXCLUDED.nav
                """,
                (BOOK, day, nav),
            )

    return _install


@pytest.fixture
def instrument(conn):
    """``market.instruments`` に現行銘柄を1件作り instrument_id を返す。"""

    def _make(
        symbol: str = "7203.T",
        *,
        asset_class: str = "equity",
        venue: str = "TSE",
        currency: str = "JPY",
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market.instruments
                    (symbol, asset_class, venue, currency, valid_from)
                VALUES (%s, %s, %s, %s, now() - interval '30 days')
                RETURNING instrument_id
                """,
                (symbol, asset_class, venue, currency),
            )
            return cur.fetchone()[0]

    return _make


@pytest.fixture
def classify(conn, run):
    """``market.instrument_classification`` に決定論分類を入れる(curated 供給の模擬)。"""

    def _install(
        instrument_id: int,
        *,
        universe_tags: tuple[str, ...] = ("jp_equity_cash",),
        instrument_flags: tuple[str, ...] = (),
        is_single_name: bool | None = True,
        product: str = "listed_equity_cash",
        unit_size: Decimal | None = Decimal(100),
        as_of: datetime | None = None,
    ) -> None:
        upsert_classification(
            conn,
            instrument_id,
            Classification(
                universe_tags=universe_tags,
                instrument_flags=instrument_flags,
                is_single_name=is_single_name,
                product=product,
                unit_size=unit_size,
            ),
            run_id=run.run_id,
            source="curated",
            as_of=as_of or (datetime.now(UTC) - timedelta(days=1)),
        )

    return _install


@pytest.fixture
def insert_bars(conn, run):
    """日足を古い順に流し込む(``closes`` の末尾が最新日)。"""

    def _insert(
        instrument_id: int,
        closes: list[float | Decimal],
        *,
        volumes: list[float | Decimal] | None = None,
        last_day: date | None = None,
        source: str = "test",
        as_of: datetime | None = None,
    ) -> list[datetime]:
        last_day = last_day or (datetime.now(UTC).astimezone(JST).date() - timedelta(days=1))
        stamps: list[datetime] = []
        n = len(closes)
        with conn.cursor() as cur:
            for i, close in enumerate(closes):
                day = last_day - timedelta(days=(n - 1 - i))
                ts = datetime.combine(day, time(0, 0), tzinfo=JST)
                volume = None if volumes is None else Decimal(str(volumes[i]))
                cur.execute(
                    """
                    INSERT INTO market.bars
                        (instrument_id, ts, timeframe, open, high, low, close, volume,
                         source, as_of, run_id)
                    VALUES (%s, %s, '1d', %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        instrument_id, ts, Decimal(str(close)), Decimal(str(close)),
                        Decimal(str(close)), Decimal(str(close)), volume, source,
                        as_of or ts, run.run_id,
                    ),
                )
                stamps.append(ts)
        return stamps

    return _insert


@pytest.fixture
def insert_document(conn, run):
    """``docs.documents`` に文書を1件入れて doc_id を返す(証憑テスト用)。"""

    def _insert(*, title: str = "テスト文書", as_of: datetime | None = None) -> int:
        stamp = as_of or (datetime.now(UTC) - timedelta(days=1))
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO docs.documents
                    (source_type, source_name, title, body, as_of, content_hash, run_id)
                VALUES ('filing', 'TDnet', %s, '本文', %s, sha256(%s::bytea), %s)
                RETURNING doc_id
                """,
                (title, stamp, title.encode(), run.run_id),
            )
            return cur.fetchone()[0]

    return _insert
