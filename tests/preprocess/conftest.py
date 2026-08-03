"""preprocess テスト共通フィクスチャ。

ライブ PostgreSQL（compose.yaml の DB）に対して実行する。接続不可なら skip。
テストは commit せず rollback して隔離する（preprocess の各 API は渡された ``conn`` を
commit しない）。**実埋め込みモデルはロードしない** — ``HashingEmbedder``（依存ゼロの
決定論ダミー）を注入する（要件: フィクスチャはダミーベクトル）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from ryza.db import migrate
from ryza.db.conn import connect, database_url
from ryza.preprocess.embed import HashingEmbedder
from ryza.preprocess.importance import ImportanceConfig
from ryza.provenance import start_run


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
def conn(migrated_db, clear_residual):
    """関数スコープの接続。テストは commit せず rollback して隔離する。

    接続直後にトランザクション内で残留データを不可視化する(Issue #23)。
    共有 DB に commit 済みの docs.documents 等が残っていると、パイプラインが
    残留分を処理して件数 assert が壊れるため。rollback で削除ごと巻き戻る。
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
    """共有接続に参加する Run（commit しない・rollback で隔離）。"""
    return start_run("test.preprocess", {"task": "T-010"}, conn=conn)


@pytest.fixture
def embedder():
    """決定論ダミー埋め込み器（実モデルをロードしない）。"""
    return HashingEmbedder(dim=64, model_name="test-hashing")


@pytest.fixture
def config():
    """本番の config/importance.yaml を読む（ルールの実データで検証する）。"""
    return ImportanceConfig.load()


@pytest.fixture
def insert_doc(conn, run):
    """テスト用に ``docs.documents`` へ 1 件挿入して doc_id を返すヘルパー。

    ``content_hash`` は本文から計算する（未指定時）。meta は未処理（NULL）で始める。
    """
    import hashlib

    def _insert(
        *,
        source_type: str = "filing",
        source_name: str = "TDnet",
        title: str | None = None,
        body: str | None = None,
        content_hash: bytes | None = None,
        meta: dict | None = None,
    ) -> int:
        digest = content_hash or hashlib.sha256(
            (body or title or "x").encode("utf-8")
        ).digest()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO docs.documents
                    (source_type, source_name, title, body, as_of, content_hash, meta, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING doc_id
                """,
                (
                    source_type, source_name, title, body,
                    datetime.now(UTC), digest,
                    Jsonb(meta) if meta is not None else None, run.run_id,
                ),
            )
            return cur.fetchone()[0]

    return _insert


@pytest.fixture
def make_instrument(conn):
    """テスト用に ``market.instruments`` へ現行行を挿入し instrument_id を返す。"""

    def _make(symbol: str, *, asset_class: str = "equity", venue: str = "TSE",
              currency: str = "JPY") -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market.instruments
                    (symbol, asset_class, venue, currency, valid_from, valid_to)
                VALUES (%s, %s, %s, %s, now(), NULL)
                RETURNING instrument_id
                """,
                (symbol, asset_class, venue, currency),
            )
            return cur.fetchone()[0]

    return _make
