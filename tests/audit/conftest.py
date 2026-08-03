"""audit テストの共通フィクスチャ(tests/bot と同じ流儀)。

DB 依存テストはテスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行し、
commit せず rollback で隔離する。git 突合テストは一時リポジトリのみで DB を使わない。
"""

from __future__ import annotations

import pytest

from ryza.db.conn import connect
from ryza.provenance import start_run


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
    """テスト用の meta.runs 実行を作り run_id を返す(共有接続に参加)。"""
    r = start_run("test.audit", conn=conn)
    return r.run_id
