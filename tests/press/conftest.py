"""press テスト共通フィクスチャ(T-007/T-008)。

research テストと同流儀: ライブ PostgreSQL に対し実行し、接続不可なら skip、commit せず
rollback で隔離する。**LLM・画像 API・ネットワークは実呼び出ししない** — 構造化出力は
``PressEchoProvider``(素材の refs を反映した合法トピックを返す決定論プロバイダ)を注入し、
画像は ``FakeHttp``(フィクスチャ post を返す)を使う。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from ryza.db import migrate
from ryza.db.conn import connect, database_url
from ryza.provenance import start_run
from ryza.research.llm import ProviderResult, StructuredLLM


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
    return start_run("test.press", {"task": "T-007/T-008"}, conn=conn)


# ── 決定論の執筆プロバイダ ─────────────────────────────────────────────────────
def _pad(base: str, n: int) -> str:
    """base を繰り返して非空白 n 文字ちょうどにする(字数を決定論化するため)。"""
    return (base * (n // len(base) + 1))[:n]


class PressEchoProvider:
    """素材(user プロンプトの JSON)を読み、合法な構造化出力を返すフェイク LLM。

    - 一次判定(triage)スキーマ: 報道価値 = magnitude×100、種別は summary/task の「予兆」有無。
    - 朝刊トピック: U字 [4,3,2,1,3,5](refs があれば level1 が refs を引用、無ければ谷=2)、
      trade_implication つき、各文 45 字で計 270 字(200-400 の範囲内)。
    - 速報: argument→level1 根拠(refs 引用)→level5 含意。予兆なら prediction を付す。
    - ``bad_shape=True`` で U字を壊した出力を返す(落板テスト用)。
    """

    def __init__(self, *, bad_shape: bool = False) -> None:
        self.bad_shape = bad_shape
        self.calls: list[dict[str, str]] = []

    def generate(
        self, *, system: str, user: str, schema: dict[str, Any], model: str
    ) -> ProviderResult:
        self.calls.append({"system": system, "user": user, "model": model})
        props = schema.get("properties", {})
        data = json.loads(user)
        # 一次判定(triage)。
        if "newsworthiness" in props and "sentences" not in props:
            trg = data.get("trigger", {})
            mag = float(trg.get("magnitude", 0.5))
            summary = str(trg.get("summary", ""))
            kind = "prediction" if "予兆" in summary else "fact"
            return _result({"newsworthiness": min(100.0, mag * 100.0), "kind": kind,
                            "reason": "test"})
        # 執筆。
        required = schema.get("required", [])
        is_morning = "trade_implication" in required
        material = data.get("material", {})
        refs = [int(x) for x in (material.get("refs") or [])]
        is_prediction = "予兆" in str(data.get("task", ""))
        content = self._write(is_morning=is_morning, refs=refs, is_prediction=is_prediction,
                              title=str(material.get("title", "トピック")))
        return _result(content)

    def _write(
        self, *, is_morning: bool, refs: list[int], is_prediction: bool, title: str
    ) -> dict[str, Any]:
        if self.bad_shape:
            return {
                "argument": "アーギュメント一文。",
                "sentences": [{"text": _pad("単調な文", 45), "level": 5, "source_ids": []}
                              for _ in range(6)],
                "trade_implication": {"action": "watch", "target": "指数", "condition": "上抜け"},
            }
        if is_morning:
            valley_level = 1 if refs else 2
            valley_refs = refs[:1] if refs else []
            s = "source_ids"
            sentences = [
                {"text": _pad("概念をまとめ広い例を示す文", 45), "level": 4, s: []},
                {"text": _pad("二つ以上の証拠をまとめた文", 45), "level": 3, s: []},
                {"text": _pad("観察の解釈的要約の文", 45), "level": 2, s: []},
                {"text": _pad("純粋なファクトの文", 45), "level": valley_level, s: valley_refs},
                {"text": _pad("再び概念的にまとめ直す文", 45), "level": 3, s: []},
                {"text": _pad("含意で締める文", 45), "level": 5, s: []},
            ]
            return {
                "argument": "きょうの相場は半導体が主導したみたい。",
                "sentences": sentences,
                "trade_implication": {"action": "watch", "target": title, "condition": "上抜け"},
                "title": title,
            }
        # 速報(短縮形)。
        fact_refs = refs[:1] if refs else []
        sentences = [
            {"text": "価格が急変したよ。", "level": 1, "source_ids": fact_refs},
            {"text": "……これは、明日のあたしたちのことだから。", "level": 5, "source_ids": []},
        ]
        out: dict[str, Any] = {
            "argument": "相場が、動いたみたい。",
            "sentences": sentences,
            "title": title,
        }
        if is_prediction:
            out["prediction"] = {"claim": "円は来週さらに弱くなるかもしれない。",
                                 "confidence": 0.6, "verify_by": "2026-08-10T00:00:00+00:00"}
        return out


def _result(content: dict[str, Any]) -> ProviderResult:
    return ProviderResult(
        content=content, tokens_in=80, tokens_out=40, raw_text=json.dumps(content)
    )


@pytest.fixture
def make_press_llm(run):
    """``PressEchoProvider`` を注入した StructuredLLM(dept_tag=press)を作るファクトリ。"""

    def _make(*, bad_shape: bool = False):
        provider = PressEchoProvider(bad_shape=bad_shape)
        llm = StructuredLLM(provider, run, dept_tag="press")
        return llm, provider

    return _make


# ── 画像フェイク HTTP ──────────────────────────────────────────────────────────
class FakeHttp:
    """安全な post 群を返すフェイク。``fail=True`` で例外(取得失敗→画像なしを検証)。"""

    def __init__(self, posts: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.posts = posts
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        if self.fail:
            raise OSError("network down")
        return self.posts if self.posts is not None else [
            {"file_url": "https://safebooru.org/img/lain.png", "owner": "artist_a",
             "tags": "iwakura_lain 1girl"},
        ]


# ── DB 素材の投入ヘルパ ────────────────────────────────────────────────────────
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
        digest = hashlib.sha256(f"{title}:{body}:{source_name}:{score}".encode()).digest()
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


@pytest.fixture
def insert_flash_trigger(conn, run):
    """docs.flash_triggers に 1 件挿入して trigger_id を返す。"""

    def _insert(*, view_id: int = 1, magnitude: float = 0.8, refs: list[int] | None = None,
               as_of: datetime | None = None) -> int:
        as_of = as_of or datetime.now(UTC)
        reason = {"magnitude": magnitude,
                  "applied": [{"kind": "regime_flip",
                               "detail": {"dimension": "jp_equity", "refs": refs or []}}]}
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO docs.flash_triggers (view_id, magnitude, reason, as_of, run_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING trigger_id
                """,
                (view_id, magnitude, Jsonb(reason), as_of, run.run_id),
            )
            return cur.fetchone()[0]

    return _insert
