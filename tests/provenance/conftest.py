"""provenance テスト共通フィクスチャ。

テスト専用 DB(tests/conftest.py の ``migrated_db`` が用意)に対して実行する。接続不可なら skip。
テストは commit せず rollback して隔離する。provenance の各 API は渡された ``conn`` を
commit しないため、rollback フィクスチャだけで完全に隔離できる(runs / lineage も含む)。
"""

from __future__ import annotations

import pytest

from ryza.db.conn import connect


@pytest.fixture
def conn(migrated_db):
    """関数スコープの接続。テストは commit せず rollback して隔離する。"""
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()
