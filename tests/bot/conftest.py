"""bot テストの共通フィクスチャ。

テスト専用 DB(tests/conftest.py の ``migrated_db`` が用意)に対して実行する。
接続できない場合は skip。各テストは関数スコープの接続を使い、commit せず rollback して隔離する。
discord API はモック(実際にはそもそも import しない — 純ロジックのみを検証する)。
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
    r = start_run("test.bot", conn=conn)
    return r.run_id
