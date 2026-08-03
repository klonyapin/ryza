"""research テスト共通フィクスチャ(T-011)。

ライブ PostgreSQL(compose.yaml の DB)に対して実行する。接続不可なら skip。
テストは commit せず rollback して隔離する(research の各 API は渡された ``conn`` を
commit しない)。**LLM は実プロバイダを呼ばない** — ``FixtureProvider``(構造化出力の
フィクスチャ)を注入する。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from ryza.db import migrate
from ryza.db.conn import connect, database_url
from ryza.provenance import start_run
from ryza.research.llm import FixtureProvider, StructuredLLM


@pytest.fixture(scope="session")
def migrated_db():
    try:
        with psycopg.connect(database_url(), connect_timeout=3):
            pass
    except Exception as exc:  # noqa: BLE001 - 接続不能は skip 理由として提示
        pytest.skip(f"PostgreSQL に接続できないため skip: {exc}")
    migrate.run()
    yield


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run(conn):
    """共有接続に参加する Run(commit しない・rollback で隔離)。"""
    return start_run("test.research", {"task": "T-011"}, conn=conn)


@pytest.fixture
def make_llm(run):
    """単一/複数の scores 応答を返す ``StructuredLLM`` を作るファクトリ。

    ``make_llm(scores_dict)`` または ``make_llm([r1, r2, ...])`` で FixtureProvider を注入する。
    provider も返して呼び出し記録(calls・コスト検証)を検査できるようにする。
    """

    def _make(responses):
        if isinstance(responses, dict):
            responses = [responses]
        provider = FixtureProvider(responses)
        llm = StructuredLLM(provider, run)
        return llm, provider

    return _make


@pytest.fixture
def insert_enriched_doc(conn, run):
    """``docs.documents`` に「前処理済み」文書を 1 件挿入して doc_id を返す。

    triage_queue ビューに載るよう meta を直接埋める(実 preprocess パイプラインは通さない)。
    """

    def _insert(
        *,
        source_type: str = "filing",
        source_name: str = "TDnet",
        title: str = "テスト開示",
        body: str = "本文",
        category: str = "filing_earnings",
        tier: str = "high",
        score: float = 0.8,
        instrument_ids: list[int] | None = None,
        is_duplicate: bool = False,
    ) -> int:
        digest = hashlib.sha256(f"{title}:{body}:{source_name}".encode()).digest()
        meta = {
            "preprocessed_at": datetime.now(UTC).isoformat(),
            "preprocess_version": "1",
            "classification": {"category": category, "label": category},
            "importance": {"tier": tier, "score": score},
            "dedup": {"is_duplicate": is_duplicate, "duplicate_of": None},
            "tags": {"instrument_ids": instrument_ids or []},
        }
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO docs.documents
                    (source_type, source_name, title, body, as_of, content_hash, meta, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING doc_id
                """,
                (source_type, source_name, title, body, datetime.now(UTC),
                 digest, Jsonb(meta), run.run_id),
            )
            return cur.fetchone()[0]

    return _insert
