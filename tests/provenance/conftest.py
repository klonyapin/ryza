"""provenance テスト共通フィクスチャ。

ライブ PostgreSQL(compose.yaml の DB)に対して実行する。接続不可なら skip。
テストは commit せず rollback して隔離する。provenance の各 API は渡された ``conn`` を
commit しないため、rollback フィクスチャだけで完全に隔離できる(runs / lineage も含む)。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.db import migrate
from ryza.db.conn import connect, database_url


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
