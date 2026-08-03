"""tests 全体の共有フィクスチャ。

**テスト専用 DB による恒久隔離**(Issue #23): DB 依存テストは以前、日次取込
ジョブ等と同じ共有 PostgreSQL(compose.yaml の ``ryza``)に対して実行していた。
rollback 隔離はテスト自身の書込にしか効かないため、別セッションが commit した
実データ(docs.documents / market.indicators など)が残っていると、テーブル
全体を数える count assert が壊れる(実例: 2026-08-03 の residual-seed Run)。

対策として ``pytest_configure`` で ``RYZA_DATABASE_URL`` を**テスト専用 DB**
(既定: 元の dbname + ``_test`` → ``ryza_test``)へ差し替える。テスト DB は
``migrated_db`` フィクスチャが初回に CREATE DATABASE し、全マイグレーションを
適用する。取込ジョブはテスト DB に書かないため、残留データは原理的に発生
しない。テスト DB を明示したい場合は ``RYZA_TEST_DATABASE_URL`` で上書きできる。

``clear_residual``(トランザクション内 DELETE、rollback で巻き戻し)は
防御の第二層として残す: テストのバグで commit してしまった場合や、複数
ワークツリーのテストセッションが同じテスト DB を並行使用した場合の保険。
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error

import psycopg
import pytest
from psycopg import conninfo, errors

from ryza import secrets
from ryza.db import migrate
from ryza.db.conn import database_url

# 差し替え前の(共有)DB URL。テスト DB の CREATE DATABASE 用の管理接続に使う。
_ADMIN_URL: str = ""


def _test_database_url(base_url: str) -> str:
    """テスト専用 DB の URL。RYZA_TEST_DATABASE_URL があれば最優先。

    既定は ``base_url`` の dbname に ``_test`` を付けたもの(``ryza`` → ``ryza_test``)。
    既に ``_test`` で終わる場合はそのまま使う。
    """
    override = os.environ.get("RYZA_TEST_DATABASE_URL")
    if override:
        return override
    params = conninfo.conninfo_to_dict(base_url)
    dbname = params.get("dbname") or "ryza"
    if not dbname.endswith("_test"):
        dbname = f"{dbname}_test"
    return conninfo.make_conninfo(base_url, dbname=dbname)


def pytest_configure(config: pytest.Config) -> None:
    """収集より前に RYZA_DATABASE_URL をテスト専用 DB へ差し替える。

    以後の ``ryza.db.conn.connect()`` / ``database_url()`` は全てテスト DB を
    指すため、テストコード側の変更は不要。
    """
    global _ADMIN_URL
    _ADMIN_URL = database_url()
    os.environ["RYZA_DATABASE_URL"] = _test_database_url(_ADMIN_URL)
    # VM(GCE)上でテストを実行しても Secret Manager フォールバック(Issue #30)が
    # 実メタデータ・実 Secret へ到達しないよう、環境の GCP_PROJECT は外す。
    # 必要なテストは monkeypatch.setenv("GCP_PROJECT", ...) で明示設定する。
    os.environ.pop("GCP_PROJECT", None)


@pytest.fixture(scope="session")
def migrated_db():
    """テスト専用 DB を(なければ)作成し、全マイグレーションを適用して yield。

    PostgreSQL 自体に接続できない場合は skip(Docker 未導入環境向け)。
    CREATE DATABASE はトランザクション外でしか実行できないため autocommit の
    管理接続(元 URL)を使う。並行するテストセッションと作成が競合しても
    DuplicateDatabase を握りつぶして続行する。
    """
    test_dbname = conninfo.conninfo_to_dict(database_url()).get("dbname")
    try:
        with psycopg.connect(_ADMIN_URL, autocommit=True, connect_timeout=3) as admin:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (test_dbname,)
                )
                if cur.fetchone() is None:
                    try:
                        cur.execute(f'CREATE DATABASE "{test_dbname}"')
                    except errors.DuplicateDatabase:
                        pass
    except Exception as exc:  # noqa: BLE001 - 接続不能は skip 理由として提示
        pytest.skip(f"PostgreSQL に接続できないため skip: {exc}")
    migrate.run()
    yield


# 取込・前処理テストが件数 assert の対象にするテーブル。FK の子 → 親の順。
RESIDUAL_TABLES = (
    "meta.lineage_edges",
    "docs.embeddings",           # docs.documents への FK 子
    "docs.documents_enriched",
    "docs.preprocess_pending",
    "docs.triage_queue",
    "docs.documents",
    "market.indicators",
    "market.bars",
    "market.calendar_events",
    "press.outbox",
)

# ledger.evidence は ledger.journal_entries / ledger.reconciliations から
# FK 参照され得るため、未参照行のみ削除する(取込由来の証憑は未参照)。
_CLEAR_EVIDENCE_SQL = """
    DELETE FROM ledger.evidence e
    WHERE NOT EXISTS (SELECT 1 FROM ledger.journal_entries j
                      WHERE j.evidence_id = e.evidence_id)
      AND NOT EXISTS (SELECT 1 FROM ledger.reconciliations r
                      WHERE r.evidence_id = e.evidence_id)
"""


@pytest.fixture
def clear_residual():
    """接続を受け取り、そのトランザクション内で残留データを不可視にする関数。

    呼び出し側の ``conn`` フィクスチャが接続直後に呼ぶ。**commit してはならない**
    (rollback で削除が巻き戻ることがテスト DB を並行セッションと共有する前提)。
    """

    def _clear(conn) -> None:
        with conn.cursor() as cur:
            for table in RESIDUAL_TABLES:
                cur.execute(f"DELETE FROM {table}")  # noqa: S608 - 固定リスト
            cur.execute(_CLEAR_EVIDENCE_SQL)

    return _clear


@pytest.fixture
def fake_secret_manager(monkeypatch):
    """``ryza.secrets`` の GCE メタデータ + Secret Manager REST をモックする(Issue #30)。

    ``fake_secret_manager({"jquants-api-key": "K"})`` のように登録済み Secret を渡すと
    ``ryza.secrets._urlopen`` を差し替え、アクセスされた URL のリストを返す(env 優先の
    検証は「リストが空のまま」であることを見る)。未登録 Secret へのアクセスは 404。
    実ネットワークは一切呼ばない。
    """

    def _install(values: dict[str, str]) -> list[str]:
        calls: list[str] = []

        def _fake(req, timeout):
            url = req.full_url
            calls.append(url)
            if "metadata.google.internal" in url:
                return io.BytesIO(json.dumps({"access_token": "TOKEN"}).encode())
            name = url.split("/secrets/")[1].split("/")[0]
            if name not in values:
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            data = base64.b64encode(values[name].encode()).decode()
            return io.BytesIO(json.dumps({"payload": {"data": data}}).encode())

        monkeypatch.setattr(secrets, "_urlopen", _fake)
        return calls

    return _install
