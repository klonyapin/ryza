"""ingest.jquants — J-Quants API **V2**（日足・財務・銘柄マスタ）。

Free プランで開始する（必要時に予算承認の上 Standard 化）。J-Quants は 2026-06-01 に
V1 を廃止し V2 へ移行済み（V1 の ``/v1/token/auth_refresh`` は HTTP 410 Gone）。V2 は
**API キー認証**（``x-api-key`` ヘッダ）で、リフレッシュトークン→idToken の 2 段認証は
廃止された。API キーは Secret Manager の ``jquants-api-key``、無ければ環境変数
``RYZA_JQUANTS_API_KEY`` フォールバック。

取込対象（V2 エンドポイント）:
- 銘柄マスタ（``/v2/equities/master``）→ ``market.instruments``（SCD2 自動登録）
- 日足（``/v2/equities/bars/daily``）→ ``market.bars``（timeframe='1d', source='jquants'）
- 財務（``/v2/fins/summary``）→ ``docs.documents``（source_type='filing'）

V2 ではレスポンスのカラム名が短縮された（例 ``Open``→``O``、``Volume``→``Vo``、
``DisclosedDate``→``DiscDate``）。トップレベルは ``{"data": [...]}`` に統一され、
``pagination_key`` でページ送りする（全ページを連結して返す）。

HTTP は ``Fetcher`` 越し（テストはモック）。日足の各バーは証憑（API 生レスポンス）への
リネージ辺を張る。

実行: ``python -m ryza.ingest.jquants [--date YYYY-MM-DD]``
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.base import Fetcher
from ryza.provenance import EvidenceStore, Run, record
from ryza.provenance import run as run_ctx

_API_BASE = "https://api.jquants.com"
SOURCE = "jquants"
SOURCE_NAME = "J-Quants"


class JQuantsAuthError(RuntimeError):
    """API キー未設定。"""


def api_key() -> str:
    """API キーを取得する（Secret 優先・環境変数フォールバック）。

    Secret Manager 連携は運用基盤側で環境変数に注入される想定。ここでは env を見る。
    """
    key = os.environ.get("RYZA_JQUANTS_API_KEY") or os.environ.get("JQUANTS_API_KEY")
    if not key:
        raise JQuantsAuthError(
            "J-Quants API キー未設定（Secret 'jquants-api-key' / "
            "env RYZA_JQUANTS_API_KEY）"
        )
    return key


def _auth_headers(key: str) -> dict[str, str]:
    return {"x-api-key": key}


def _fetch_all(
    fetcher: Fetcher,
    path: str,
    *,
    key: str,
    params: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """V2 エンドポイントを ``pagination_key`` で全ページ取得し ``data`` を連結して返す。

    V2 は全エンドポイントで ``{"data": [...], "pagination_key": ...}`` 形。次ページが
    無くなる（``pagination_key`` が返らない）まで繰り返す。
    """
    url = f"{_API_BASE}{path}"
    query = dict(params or {})
    out: list[dict[str, Any]] = []
    while True:
        resp = fetcher.fetch(url, params=query, headers=_auth_headers(key))
        if not resp.ok:
            raise RuntimeError(f"{path} 失敗: status={resp.status}")
        payload = resp.json()
        out.extend(payload.get("data", []))
        next_key = payload.get("pagination_key")
        if not next_key:
            break
        query["pagination_key"] = next_key
    return out


def _normalize_symbol(code: str) -> str:
    """J-Quants の Code（5 桁, 例 '72030'）を TSE ティッカー（'7203.T'）に正規化。"""
    code = str(code).strip()
    if len(code) == 5 and code.endswith("0"):
        code = code[:4]
    return f"{code}.T"


# ────────────────────────────────────────────────────────────────────────────
# 銘柄マスタ（/v2/equities/master）
# ────────────────────────────────────────────────────────────────────────────
def fetch_listed_info(fetcher: Fetcher, key: str) -> list[dict[str, Any]]:
    """上場銘柄一覧を取得する。"""
    return _fetch_all(fetcher, "/v2/equities/master", key=key)


def ingest_instruments(
    conn: psycopg.Connection,
    records: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """銘柄マスタを ``market.instruments`` に反映（SCD2 自動登録）。

    既存銘柄はそのまま、未登録銘柄のみ新規行を作る。``{'resolved': n, 'created': m}``。
    """
    as_of = as_of or datetime.now(UTC)
    created = 0
    for rec in records:
        symbol = _normalize_symbol(rec.get("Code", ""))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM market.instruments "
                "WHERE symbol = %s AND valid_to IS NULL LIMIT 1",
                (symbol,),
            )
            existed = cur.fetchone() is not None
        base.resolve_instrument(
            conn, symbol, asset_class="equity", venue="TSE",
            currency="JPY", as_of=as_of,
        )
        if not existed:
            created += 1
    return {"resolved": len(records), "created": created}


# ────────────────────────────────────────────────────────────────────────────
# 日足（/v2/equities/bars/daily）
# ────────────────────────────────────────────────────────────────────────────
def fetch_daily_quotes(
    fetcher: Fetcher, key: str, quote_date: str
) -> list[dict[str, Any]]:
    """指定日の全銘柄日足を取得する（Free プランは 12 週遅延だが構造は同じ）。"""
    return _fetch_all(
        fetcher, "/v2/equities/bars/daily", key=key, params={"date": quote_date}
    )


def _num(rec: dict[str, Any], key: str) -> float | None:
    v = rec.get(key)
    return float(v) if v is not None else None


def ingest_daily_quotes(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    quotes: list[dict[str, Any]],
    *,
    quote_date: str,
    raw_response: dict[str, Any] | list[Any] | bytes | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """日足を ``market.bars`` に書き込む（冪等・証憑・リネージ込み）。

    V2 のカラム名（``O/H/L/C/Vo``）を読む。バーの ``ts`` は当日 00:00 UTC。各バー行は
    生 API レスポンス（証憑）へのリネージ辺を張る。``{'written': 新規本数, 'total': 入力件数}``。
    """
    as_of = as_of or datetime.now(UTC)
    ts = datetime.fromisoformat(quote_date).replace(tzinfo=UTC)

    evidence_id: int | None = None
    if raw_response is not None:
        evidence_id, _ = base.save_raw(
            conn, store, kind="jquants_daily_bars",
            payload=raw_response, source=SOURCE_NAME,
        )

    written = 0
    for q in quotes:
        symbol = _normalize_symbol(q.get("Code", ""))
        instrument_id = base.resolve_instrument(
            conn, symbol, asset_class="equity", venue="TSE",
            currency="JPY", as_of=as_of,
        )
        created = base.write_bar(
            conn, run,
            instrument_id=instrument_id, ts=ts, timeframe="1d",
            open=_num(q, "O"), high=_num(q, "H"),
            low=_num(q, "L"), close=_num(q, "C"),
            volume=_num(q, "Vo"), source=SOURCE, as_of=as_of,
        )
        if created:
            written += 1
            if evidence_id is not None:
                record(
                    conn, run,
                    [("bars", base.bar_ref(instrument_id, "1d", ts))],
                    [("evidence", evidence_id)],
                )
    return {"written": written, "total": len(quotes)}


# ────────────────────────────────────────────────────────────────────────────
# 財務（/v2/fins/summary）
# ────────────────────────────────────────────────────────────────────────────
def fetch_statements(
    fetcher: Fetcher, key: str, *, code: str | None = None, stmt_date: str | None = None
) -> list[dict[str, Any]]:
    """財務諸表サマリを取得する（code か date のいずれかで）。"""
    params: dict[str, str] = {}
    if code:
        params["code"] = code
    if stmt_date:
        params["date"] = stmt_date
    return _fetch_all(fetcher, "/v2/fins/summary", key=key, params=params)


def ingest_statements(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    statements: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """財務を ``docs.documents``（source_type='filing'）に取り込む（冪等・証憑・リネージ）。

    V2 のカラム名（``Code/DiscDate/DiscNo/DocType``）を読む。1 開示 = 1 文書。冪等キーは
    ``DiscDate + DiscNo``。``{'written', 'total'}``。
    """
    as_of = as_of or datetime.now(UTC)
    written = 0
    for st in statements:
        symbol = _normalize_symbol(st.get("Code", ""))
        disclosure_no = st.get("DiscNo", "")
        disclosed = st.get("DiscDate", "")
        title = f"{symbol} 財務諸表 {st.get('DocType', '')} ({disclosed})"
        published_at = None
        if disclosed:
            try:
                published_at = datetime.fromisoformat(disclosed).replace(tzinfo=UTC)
            except ValueError:
                published_at = None
        res = base.upsert_document(
            conn, run, store,
            source_type="filing", source_name=SOURCE_NAME,
            title=title, body=None, lang="ja",
            published_at=published_at, as_of=as_of,
            meta={"symbol": symbol, "kind": "financial_statement"},
            raw_payload=st, evidence_kind="jquants_statement",
            hash_source=f"{SOURCE_NAME}:{disclosed}:{disclosure_no}:{symbol}",
        )
        if res.created:
            written += 1
    return {"written": written, "total": len(statements)}


# ────────────────────────────────────────────────────────────────────────────
# オーケストレーション + エントリポイント
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class DailyResult:
    instruments: dict[str, int]
    bars: dict[str, int]


def run_daily(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    *,
    quote_date: str,
    with_instruments: bool = True,
    key: str | None = None,
) -> DailyResult:
    """日次取込（銘柄マスタ更新 + 当日日足）を実行する。"""
    key = key if key is not None else api_key()
    inst_result = {"resolved": 0, "created": 0}
    if with_instruments:
        inst_result = ingest_instruments(conn, fetch_listed_info(fetcher, key))
    quotes = fetch_daily_quotes(fetcher, key, quote_date)
    bars_result = ingest_daily_quotes(
        conn, run, store, quotes,
        quote_date=quote_date, raw_response=quotes,
    )
    return DailyResult(instruments=inst_result, bars=bars_result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J-Quants 日次取込")
    parser.add_argument(
        "--date", default=date.today().isoformat(), help="対象日 YYYY-MM-DD"
    )
    parser.add_argument(
        "--no-instruments", action="store_true", help="銘柄マスタ更新を省略"
    )
    args = parser.parse_args(argv)

    store = base.default_store()
    fetcher = base.default_fetcher()
    # autocommit 共有接続: 冪等な追記書込は逐次確定でよい（再実行が続きを埋める）。
    conn = connect(autocommit=True)
    try:
        with run_ctx("ingest.jquants.daily", {"date": args.date}, conn=conn) as r:
            result = run_daily(
                conn, r, store, fetcher,
                quote_date=args.date, with_instruments=not args.no_instruments,
            )
        print(f"jquants daily {args.date}: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
