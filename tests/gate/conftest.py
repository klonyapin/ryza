"""gate テストの共通フィクスチャ。

規則テスト(test_rules.py)は純ロジック — DB 不要。判定境界の値は **config/ips.yaml・
config/mandates/*.yaml の実値**を読み込んで検証する(保護領域のリグレッション検知を兼ねる)。
DB テスト(test_store.py)はテスト専用 DB(tests/conftest.py の ``migrated_db``)に対して
実行し、commit せず rollback で隔離する。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ryza.db.conn import connect
from ryza.gate.compliance import LimitsState, OrderProposal, PortfolioState, PositionState
from ryza.ips import load_and_validate
from ryza.ledger import create_run


@pytest.fixture(scope="session")
def ips_and_mandates():
    """発効済み設定の実値(config/ips.yaml + config/mandates/*.yaml)。"""
    return load_and_validate()


@pytest.fixture(scope="session")
def ips(ips_and_mandates):
    return ips_and_mandates[0]


@pytest.fixture(scope="session")
def mandates(ips_and_mandates):
    return ips_and_mandates[1]


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
    """テスト用の meta.runs 実行を作り run_id を返す。"""
    return create_run(conn, "test.gate", params={"task": "T-014"})


@pytest.fixture
def limits_row(conn):
    """risk.limits_state の行を(トランザクション内で)用意する関数。"""

    def _install(
        book_id: str = "DEMO_FUND",
        *,
        dd_soft: bool = False,
        dd_hard: bool = False,
        vol_exceeded: bool = False,
        es_exceeded: bool = False,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO risk.limits_state
                    (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (book_id) DO UPDATE
                SET dd_soft = EXCLUDED.dd_soft, dd_hard = EXCLUDED.dd_hard,
                    vol_exceeded = EXCLUDED.vol_exceeded, es_exceeded = EXCLUDED.es_exceeded,
                    as_of = now()
                """,
                (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded),
            )

    return _install


# ── 注文案・状態のビルダ(規則テスト用)──────────────────────────────────────
_NO_FLAGS = LimitsState()  # リスクフラグ全て false(通常時)
def jp_stock_proposal(**overrides) -> OrderProposal:
    """Ben の日本個別株の現物買い(全規則を通過する基準形)。"""
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


def make_state(
    *,
    nav: Decimal | None = Decimal(10_000_000),
    cash: Decimal | None = Decimal(5_000_000),
    positions: tuple[PositionState, ...] | None = (),
    daily_turnover: Decimal | None = Decimal(0),
    limits: LimitsState | None = _NO_FLAGS,
    trading_state: str | None = "normal",
    prices: dict[int, Decimal] | None = None,
    auto_prices: bool = True,
) -> PortfolioState:
    """状態ビルダ。auto_prices=True なら保有銘柄の時価を avg_cost で明示補完する
    (エンジン側は時価欠落を fail-closed で block するため、テストで明示的に与える)。
    """
    merged = dict(prices or {})
    if auto_prices:
        for pos in positions or ():
            merged.setdefault(pos.instrument_id, pos.avg_cost)
    return PortfolioState(
        trading_state=trading_state,
        nav=nav,
        cash=cash,
        positions=positions,
        daily_turnover=daily_turnover,
        limits=limits,
        prices=merged,
    )


def rules_of(result, severity: str | None = None) -> set[str]:
    """判定結果から(severity で絞った)規則 ID の集合を取り出す。"""
    return {r.rule for r in result.reasons if severity is None or r.severity == severity}
