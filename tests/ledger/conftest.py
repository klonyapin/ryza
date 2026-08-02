"""ledger テストの共通フィクスチャ。

ライブ PostgreSQL(compose.yaml の DB)に対して実行する。接続できない場合は skip。
各テストは関数スコープの接続を使い、commit せず rollback して隔離する(シードや他テストを汚さない)。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.db import migrate
from ryza.db.conn import connect, database_url
from ryza.ledger import create_run


@pytest.fixture(scope="session")
def migrated_db():
    """DB に接続できれば全マイグレーションを適用して yield。不可なら skip。"""
    try:
        with psycopg.connect(database_url(), connect_timeout=3):
            pass
    except Exception as exc:  # noqa: BLE001 - 接続不能は skip 理由として提示
        pytest.skip(f"PostgreSQL に接続できないため skip: {exc}")
    migrate.run()
    yield


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
    return create_run(conn, "test.ledger", params={"task": "T-002"})
