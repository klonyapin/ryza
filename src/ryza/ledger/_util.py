"""会計エンジン内部共通ヘルパー。

証憑(evidence)の作成、meta.runs の作成、Decimal 変換、勘定科目メタの取得、
移動平均法によるポジション再生(fills の再生)を提供する。

- 金額はすべて Decimal で扱う(numeric 列と対応)。
- 証憑は設計書 §5 の補足に従い、小さな内部記録は payload_ref に JSON をインライン格納し、
  sha256 は格納内容(UTF-8 バイト列)に対して計算する(T-003 の GCS 証憑ストア完成まで
  の DB 内フォールバック)。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

CODE_VERSION = "T-002"


def to_decimal(x: Any) -> Decimal:
    """int/float/str/Decimal を Decimal に変換する。float は文字列経由で精度劣化を避ける。"""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(str(x))
    return Decimal(x)


def _now() -> datetime:
    return datetime.now(UTC)


def create_run(
    conn: psycopg.Connection,
    job_name: str,
    *,
    code_version: str = CODE_VERSION,
    params: dict | None = None,
    status: str = "success",
) -> int:
    """meta.runs に実行記録を作り run_id を返す。全書き込みのリネージの鍵。"""
    with conn.cursor() as cur:
        now = _now()
        cur.execute(
            """
            INSERT INTO meta.runs (job_name, code_version, started_at, finished_at,
                                   status, params)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING run_id
            """,
            (job_name, code_version, now, now, status, json.dumps(params or {})),
        )
        return cur.fetchone()[0]


def _canonical_json(payload: Any) -> str:
    """dict/list は決定論的 JSON 文字列に、str はそのまま返す。"""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def create_evidence(
    conn: psycopg.Connection,
    *,
    kind: str,
    payload: Any,
    source: str,
    retrieved_at: datetime | None = None,
) -> int:
    """証憑行を作成し evidence_id を返す。

    payload は dict/list/str。小さな内部記録は JSON を payload_ref にインライン格納し、
    sha256 は JSON バイト列に対して計算する(設計書 §5 補足)。
    """
    text = _canonical_json(payload)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING evidence_id
            """,
            (kind, text, digest, source, retrieved_at or _now()),
        )
        return cur.fetchone()[0]


def resolve_evidence(
    conn: psycopg.Connection,
    evidence: int | dict | None,
    *,
    default_kind: str = "decision",
    default_source: str = "ledger",
) -> int:
    """evidence を evidence_id に解決する。

    - int: 既存 evidence_id としてそのまま返す
    - dict: {kind, payload, source} から新規作成
    - None: ValueError(証憑必須)
    """
    if evidence is None:
        raise ValueError("証憑(evidence)が必要です: 仕訳は evidence_id 必須")
    if isinstance(evidence, int):
        return evidence
    if isinstance(evidence, dict):
        return create_evidence(
            conn,
            kind=evidence.get("kind", default_kind),
            payload=evidence.get("payload", evidence),
            source=evidence.get("source", default_source),
        )
    raise TypeError(f"evidence は int/dict/None のいずれか: {type(evidence)!r}")


def book_type(conn: psycopg.Connection, book_id: str) -> str:
    """帳簿の book_type('fund'|'ops')を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT book_type FROM ledger.books WHERE book_id = %s", (book_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"未知の帳簿: {book_id}")
    return row[0]


def account_meta(conn: psycopg.Connection, book_id: str) -> dict[str, dict[str, str]]:
    """勘定科目 account_id -> {name, category} のマップを返す。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, name, category FROM ledger.accounts WHERE book_id = %s",
            (book_id,),
        )
        return {r[0]: {"name": r[1], "category": r[2]} for r in cur.fetchall()}


def cash_account(bt: str) -> str:
    """帳簿種別に対応する現金勘定 ID。"""
    return "cash" if bt == "fund" else "cash_bank"


def replay_position(
    conn: psycopg.Connection, book_id: str, instrument_id: int
) -> tuple[Decimal, Decimal]:
    """記帳済みの約定(broker_fill 証憑)を再生し、移動平均法の (保有数量, 取得原価合計) を返す。

    - buy: qty と cost(=qty*price、手数料は別途費用計上のため原価に含めない)を加算
    - sell: 数量を減らし、取得原価を平均原価分だけ取り崩す
    逆仕訳(reversal_of)された約定と、逆仕訳エントリ自体は除外する。
    MTM(price_snapshot)は原価に影響しないため、ここでは対象外。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.payload_ref
            FROM ledger.journal_entries je
            JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
            WHERE je.book_id = %s
              AND e.kind = 'broker_fill'
              AND je.reversal_of IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ledger.journal_entries r
                  WHERE r.reversal_of = je.entry_id
              )
            ORDER BY je.entry_id
            """,
            (book_id,),
        )
        payloads = [r[0] for r in cur.fetchall()]

    qty = Decimal(0)
    cost = Decimal(0)
    for text in payloads:
        try:
            fill = json.loads(text)
        except (ValueError, TypeError):
            continue
        if int(fill.get("instrument_id", -1)) != int(instrument_id):
            continue
        f_qty = to_decimal(fill["qty"])
        f_price = to_decimal(fill["price"])
        side = fill["side"]
        if side == "buy":
            qty += f_qty
            cost += f_qty * f_price
        elif side == "sell":
            if qty <= 0:
                continue
            released = cost * f_qty / qty
            cost -= released
            qty -= f_qty
    return qty, cost


def held_instruments(conn: psycopg.Connection, book_id: str) -> list[int]:
    """securities 勘定に instrument_id 付きの明細を持つ全銘柄 ID を返す。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT instrument_id
            FROM ledger.journal_lines
            WHERE book_id = %s AND account_id = 'securities' AND instrument_id IS NOT NULL
            ORDER BY instrument_id
            """,
            (book_id,),
        )
        return [r[0] for r in cur.fetchall()]


def securities_book_value(
    conn: psycopg.Connection,
    book_id: str,
    instrument_id: int,
    *,
    as_of: _date | None = None,
) -> Decimal:
    """securities 勘定の当該銘柄の帳簿価額(borrow=debit-credit)を返す。MTM 反映後は時価。"""
    sql = """
        SELECT COALESCE(sum(jl.debit - jl.credit), 0)
        FROM ledger.journal_lines jl
        JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
        WHERE jl.book_id = %s AND jl.account_id = 'securities'
          AND jl.instrument_id = %s
    """
    params: list[Any] = [book_id, instrument_id]
    if as_of is not None:
        sql += " AND je.entry_date <= %s"
        params.append(as_of)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return to_decimal(cur.fetchone()[0])
