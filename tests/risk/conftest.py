"""risk テストの共通フィクスチャ(T-015)。

engine テストは純ロジック — DB 不要。判定境界は **config/ips.yaml の実値**で検証する
(保護領域のリグレッション検知を兼ねる — 07-development §3-2)。DB テストはテスト専用
DB(tests/conftest.py の ``migrated_db``)に対して実行し、commit せず rollback で隔離。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ryza.db.conn import connect
from ryza.ips import IPSConfig
from ryza.ledger import create_run
from ryza.risk.engine import NavPoint


@pytest.fixture(scope="session")
def ips():
    """発効済み IPS の実値(config/ips.yaml)。"""
    return IPSConfig.load()


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
    return create_run(conn, "test.risk", params={"task": "T-015"})


def nav_series(navs, *, start=date(2030, 1, 1), flows=None):
    """営業日連続の NavPoint 列を組むヘルパ(数値は Decimal 化)。"""
    flows = flows or {}
    return [
        NavPoint(
            day=start + timedelta(days=i),
            nav=Decimal(str(nav)),
            net_flow=Decimal(str(flows.get(i, 0))),
        )
        for i, nav in enumerate(navs)
    ]


def constant_growth_series(n_points, *, rate="1.01", initial="1000000"):
    """日次リターン一定の NAV 系列(EWMA が |r|·√252 に一致する解析形)。"""
    navs = [Decimal(initial)]
    for _ in range(n_points - 1):
        navs.append(navs[-1] * Decimal(rate))
    return nav_series(navs)
