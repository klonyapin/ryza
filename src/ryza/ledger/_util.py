"""会計エンジン内部共通ヘルパー。

証憑(evidence)の作成、meta.runs の作成、Decimal 変換、勘定科目メタの取得、
移動平均法によるポジション再生(fills の再生)を提供する。

- 金額はすべて Decimal で扱う(numeric 列と対応)。
- 証憑作成(``create_evidence``)は T-003 の証憑ストア(``ryza.provenance.evidence``)経由に
  統合済み(T-005)。環境変数 ``RYZA_EVIDENCE_DIR`` があれば ``EvidenceStore(LocalStorage(そのパス))``
  で不変保存 + sha256 改竄検知 + 重複排除を行う。未設定時は設計書 §5 補足に従い、小さな内部記録は
  payload_ref に JSON をインライン格納する(kind='decision' 等の内部記録はインライン許容)。
- ``replay_position`` はどちらの経路の証憑でも payload を復元して読む。
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from datetime import UTC, datetime
from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

from ryza.provenance.evidence import EvidenceStore, LocalStorage

CODE_VERSION = "T-002"

# 証憑ストア経由で保存された payload_ref の URI スキーム(インライン格納との判別に使う)。
_STORE_URI_SCHEMES = ("file://", "gs://")


@functools.lru_cache(maxsize=8)
def _evidence_store_for(evidence_dir: str) -> EvidenceStore:
    """RYZA_EVIDENCE_DIR に対応する EvidenceStore を返す(パス単位でキャッシュ)。"""
    return EvidenceStore(LocalStorage(evidence_dir))


def _evidence_store() -> EvidenceStore | None:
    """環境変数 ``RYZA_EVIDENCE_DIR`` があれば証憑ストアを、無ければ None を返す。

    None のときは create_evidence が従来どおり payload_ref に JSON をインライン格納する。
    """
    evidence_dir = os.environ.get("RYZA_EVIDENCE_DIR")
    if not evidence_dir:
        return None
    return _evidence_store_for(evidence_dir)


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


def _store_payload(payload: Any) -> bytes | dict[str, Any] | list[Any]:
    """証憑ストア(``EvidenceStore.store``)が受ける型に正規化する。

    dict/list/bytes はそのまま(dict/list は store 側が決定論的 JSON 化)。str は utf-8 バイト列に。
    それ以外は決定論的 JSON バイト列にする。
    """
    if isinstance(payload, (bytes, dict, list)):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return _canonical_json(payload).encode("utf-8")


def create_evidence(
    conn: psycopg.Connection,
    *,
    kind: str,
    payload: Any,
    source: str,
    retrieved_at: datetime | None = None,
) -> int:
    """証憑行を作成し evidence_id を返す。

    ``RYZA_EVIDENCE_DIR`` が設定されていれば T-003 の証憑ストア経由で保存する
    (不変保存 + sha256 改竄検知 + 重複排除。retrieved_at はストアが設定するため無視)。
    未設定時は小さな内部記録として JSON を payload_ref にインライン格納し、
    sha256 は JSON バイト列に対して計算する(設計書 §5 補足)。
    """
    store = _evidence_store()
    if store is not None:
        return store.store(conn, kind, _store_payload(payload), source)

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


def load_evidence_payload(
    conn: psycopg.Connection, evidence_id: int, payload_ref: str
) -> Any | None:
    """証憑の payload を JSON として復元する(ストア経由・インラインの両対応)。

    payload_ref が証憑ストアの URI(file://|gs://)なら実体を取得して json.loads し、
    そうでなければインライン JSON としてそのまま json.loads する。復元不能時は None。
    """
    if payload_ref.startswith(_STORE_URI_SCHEMES):
        store = _evidence_store()
        if store is not None:
            try:
                return json.loads(store.get(conn, evidence_id).decode("utf-8"))
            except (ValueError, TypeError, KeyError, OSError):
                return None
        return None
    try:
        return json.loads(payload_ref)
    except (ValueError, TypeError):
        return None


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
    conn: psycopg.Connection,
    book_id: str,
    instrument_id: int,
    *,
    as_of: _date | None = None,
) -> tuple[Decimal, Decimal]:
    """記帳済みの約定(broker_fill 証憑)を再生し、移動平均法の (保有数量, 取得原価合計) を返す。

    - buy: qty と cost(=qty*price、手数料は別途費用計上のため原価に含めない)を加算
    - sell: 数量を減らし、取得原価を平均原価分だけ取り崩す
    逆仕訳(reversal_of)された約定と、逆仕訳エントリ自体は除外する。
    MTM(price_snapshot)は原価に影響しないため、ここでは対象外。

    ``as_of`` を渡すと ``entry_date <= as_of`` の約定だけを再生する(既定 None = 全期間 —
    従来挙動)。**なぜ必要か**(独立審査 新-3): 全期間再生の数量を使って過去日付の評価替えを
    打つと「その日に存在しなかった建玉」を過去日付で記帳してしまうため、再締めは MTM を
    打ち直せず、遅延約定のあった日の建玉が取得原価のまま残って恒久的な偽リターンを立てる。
    ``as_of`` はその日時点の建玉を point-in-time(不変原則4)で復元する手段である。

    逆仕訳の除外も ``as_of`` で切る(``r.entry_date <= as_of``)。``securities_book_value``
    の as_of は逆仕訳の**明細**を日付で落とすので、数量側だけ日付を無視して取り消すと
    「時価 − 帳簿価額」の差分が両者の非対称から生じる — 評価替えの差分計算が壊れる。
    """
    sql = """
        SELECT je.evidence_id, e.payload_ref
        FROM ledger.journal_entries je
        JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
        WHERE je.book_id = %s
          AND e.kind = 'broker_fill'
          AND je.reversal_of IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ledger.journal_entries r
              WHERE r.reversal_of = je.entry_id
                AND (%s::date IS NULL OR r.entry_date <= %s)
          )
          AND (%s::date IS NULL OR je.entry_date <= %s)
        ORDER BY je.entry_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (book_id, as_of, as_of, as_of, as_of))
        rows = cur.fetchall()

    qty = Decimal(0)
    cost = Decimal(0)
    for evidence_id, payload_ref in rows:
        fill = load_evidence_payload(conn, evidence_id, payload_ref)
        if not isinstance(fill, dict):
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


def mtm_book_value(
    conn: psycopg.Connection,
    book_id: str,
    instrument_id: int,
    *,
    as_of: _date | None = None,
) -> Decimal:
    """securities 帳簿価額のうち**評価替え(price_snapshot 証憑)が作ったぶん**を返す。

    全売却後の「残渣」を消す量はこれであって、``securities_book_value`` の総額ではない
    (独立審査 新-10 の是正を実装する過程で判明した危険): ``replay_position`` は
    ``broker_fill`` 証憑しか再生しないため、**約定を経ずに建った securities**(現物拠出:
    Dr securities / Cr capital、資産振替など)は数量ゼロに見える。総額を消しに行くと実在の
    資産を帳簿から消してしまう(実測: 現物拠出 1,000,000 の日の NAV が丸ごと戻り、
    ``restated`` が False になる = 訂正が消える)。

    評価替えが作った残高だけを見れば、約定でネットゼロになった銘柄の残渣(= 売りが
    取り崩さなかった未実現益ぶん)を過不足なく特定でき、約定外の建玉には触れない。

    逆仕訳の扱いは ``replay_position`` と同じ(逆仕訳された評価替えは両方落とす)。
    ``reverse_entry`` が作る逆仕訳の証憑は kind='decision' なので、原仕訳だけを残すと
    既に取り消された評価替えを二重に消してしまう。
    """
    sql = """
        SELECT COALESCE(sum(jl.debit - jl.credit), 0)
        FROM ledger.journal_lines jl
        JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
        JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
        WHERE jl.book_id = %s AND jl.account_id = 'securities'
          AND jl.instrument_id = %s
          AND e.kind = 'price_snapshot'
          AND je.reversal_of IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ledger.journal_entries r
              WHERE r.reversal_of = je.entry_id
                AND (%s::date IS NULL OR r.entry_date <= %s)
          )
          AND (%s::date IS NULL OR je.entry_date <= %s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (book_id, instrument_id, as_of, as_of, as_of, as_of))
        return to_decimal(cur.fetchone()[0])
