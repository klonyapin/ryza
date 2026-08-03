"""ingest テストの共通フィクスチャ。

テスト専用 DB(tests/conftest.py の ``migrated_db`` が用意)に対して実行する。
接続できない場合は skip。各テストは関数スコープの接続を使い、commit せず rollback して
隔離する。保険として、トランザクション内で対象テーブルを空にしてから yield する
(tests/conftest.py の ``clear_residual`` — rollback で削除ごと巻き戻る)。

**HTTP は全てモック**（受け入れ基準）。``FakeFetcher`` を注入し、取込コードは実 API へ
一切アクセスしない。証憑ストアは ``tmp_path`` の ``LocalStorage``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from ryza.db.conn import connect
from ryza.ingest.base import FetchResult
from ryza.provenance import EvidenceStore, LocalStorage, start_run


@pytest.fixture
def conn(migrated_db, clear_residual):
    """関数スコープの接続。テストは commit せず rollback して隔離する。

    接続直後にトランザクション内で残留データを不可視化する(Issue #23)。
    """
    c = connect()
    clear_residual(c)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run(conn):
    """共有接続に参加するテスト用 Run。"""
    return start_run("test.ingest", conn=conn)


@pytest.fixture
def store(tmp_path):
    """``tmp_path`` 上の証憑ストア（LocalStorage）。"""
    return EvidenceStore(LocalStorage(tmp_path / "evidence"))


# ────────────────────────────────────────────────────────────────────────────
# HTTP モック
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class FakeFetcher:
    """``Fetcher`` プロトコルのフェイク。URL 部分一致でレスポンスを返す。

    ``routes`` は「URL に含まれる部分文字列 → FetchResult」。``add_json`` /
    ``add_bytes`` / ``add_status`` で登録する。``calls`` に呼び出し履歴を残す。
    """

    routes: list[tuple[str, FetchResult]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def add(self, needle: str, result: FetchResult) -> FakeFetcher:
        self.routes.append((needle, result))
        return self

    def add_json(self, needle: str, obj, status: int = 200) -> FakeFetcher:
        body = json.dumps(obj).encode("utf-8")
        return self.add(needle, FetchResult(status=status, body=body))

    def add_bytes(self, needle: str, body: bytes, status: int = 200) -> FakeFetcher:
        return self.add(needle, FetchResult(status=status, body=body))

    def add_status(self, needle: str, status: int) -> FakeFetcher:
        return self.add(needle, FetchResult(status=status, body=b""))

    def fetch(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        data: bytes | None = None,
    ) -> FetchResult:
        full = url
        if params:
            full = f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
        self.calls.append(full)
        for needle, result in self.routes:
            if needle in full:
                return result
        return FetchResult(status=404, body=b"")


@pytest.fixture
def fetcher():
    return FakeFetcher()
