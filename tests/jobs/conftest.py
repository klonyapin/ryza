"""jobs テスト共通フィクスチャ(T-013)。

research/press と同流儀: テスト専用 DB(tests/conftest.py が用意)に対し実行し、
接続不可なら skip、commit せず rollback で隔離する。**LLM は実プロバイダを呼ばない** —
``DryRunProvider`` を注入する。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from ryza.db.conn import connect
from ryza.jobs import daily
from ryza.provenance import start_run
from ryza.research.llm import StructuredLLM
from ryza.research.providers import DryRunProvider, LLMConfig


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
    return start_run("test.jobs", {"task": "T-013"}, conn=conn)


@pytest.fixture
def curated_dir(tmp_path):
    """curated ユニバース定義の探索先(テスト用の空ディレクトリ)。"""
    d = tmp_path / "universe"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _isolate_curated_dir(monkeypatch, curated_dir):
    """既定の探索先を空ディレクトリへ差し替える(同梱リストへの依存を切る)。

    daily の curated 段は既定で ``config/universe/*.yaml`` を読む。テスト DB には
    同梱リストの銘柄が存在しないため、差し替えないと全テストが ``unresolved`` 35 件で
    警告を出し、テストの意図と無関係な差分に振り回される。**同梱リストが daily の
    自動経路から実際に読めること**は tests/jobs/test_curated.py が既定パスのまま検証する。
    """
    monkeypatch.setattr(daily, "CURATED_UNIVERSE_DIR", curated_dir)


@pytest.fixture
def llm_config():
    return LLMConfig.load()


@pytest.fixture
def make_daily_llms(run):
    """``DryRunProvider`` を注入した research/press の ``StructuredLLM`` ペアを作る。"""

    def _make():
        provider = DryRunProvider()
        price = LLMConfig.load().price_map()
        research = StructuredLLM(provider, run, dept_tag="research", price_per_1k=price)
        press = StructuredLLM(provider, run, dept_tag="press", price_per_1k=price)
        return research, press, provider

    return _make


@pytest.fixture
def insert_enriched_doc(conn, run):
    """triage_queue に載る「前処理済み」文書を 1 件挿入して doc_id を返す。"""

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
    ) -> int:
        digest = hashlib.sha256(
            f"{title}:{body}:{source_name}:{score}".encode()
        ).digest()
        meta = {
            "preprocessed_at": datetime.now(UTC).isoformat(),
            "preprocess_version": "1",
            "classification": {"category": category, "label": category},
            "importance": {"tier": tier, "score": score},
            "dedup": {"is_duplicate": False, "duplicate_of": None},
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
