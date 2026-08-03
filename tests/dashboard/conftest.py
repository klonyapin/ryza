"""dashboard テスト共通フィクスチャ(Issue #10)。

テスト専用 DB(tests/conftest.py が用意)に対し実行し、接続不可なら skip、
commit せず rollback で隔離する(他モジュールと同流儀)。

``dashboard/`` はパッケージとしてインストールされない(streamlit run 前提)ため、
``queries`` モジュールは dashboard/ ディレクトリを sys.path に足して import する
(``app.py`` の実行時と同じ解決方法)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ryza.db.conn import connect
from ryza.provenance import start_run

_DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))


@pytest.fixture
def conn(migrated_db, clear_residual):
    c = connect()
    clear_residual(c)  # 最新行 assert があるため残留データを不可視にする
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run(conn):
    return start_run("test.dashboard", {"task": "issue-10"}, conn=conn)
