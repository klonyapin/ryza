"""ingest.edinet — EDINET API v2（書類一覧 + type=5 CSV）。

有価証券報告書・大量保有報告書等の開示を日次取得する。まず書類一覧
（``/api/v2/documents.json?date=...&type=2``）でメタデータを得て各書類を
``docs.documents``（source_type='filing', source_name='EDINET'）へ冪等取込し、
必要に応じて type=5 CSV（``/api/v2/documents/{docID}?type=5``）を取得して証憑ストアへ
追加保存し、``documents → evidence`` のリネージ辺を張る。

認証: EDINET API v2 は Subscription-Key を要求する。Secret ``edinet-api-key`` / 環境変数
``RYZA_EDINET_API_KEY``。HTTP は ``Fetcher`` 越し（テストはモック）。

実行: ``python -m ryza.ingest.edinet [--date YYYY-MM-DD] [--no-csv]``
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.base import Fetcher
from ryza.provenance import EvidenceStore, Run, record
from ryza.provenance import run as run_ctx

_API_BASE = "https://api.edinet-fsa.go.jp"
SOURCE_NAME = "EDINET"


def api_key() -> str | None:
    """EDINET API キー（Secret 優先・環境変数フォールバック）。無ければ None。"""
    return os.environ.get("RYZA_EDINET_API_KEY") or os.environ.get("EDINET_API_KEY")


def _key_headers(key: str | None) -> dict[str, str]:
    return {"Ocp-Apim-Subscription-Key": key} if key else {}


def fetch_document_list(
    fetcher: Fetcher, target_date: str, *, key: str | None = None
) -> list[dict[str, Any]]:
    """指定日の提出書類一覧（type=2: メタデータ）を取得する。"""
    resp = fetcher.fetch(
        f"{_API_BASE}/api/v2/documents.json",
        params={"date": target_date, "type": "2"},
        headers=_key_headers(key),
    )
    if not resp.ok:
        raise RuntimeError(f"EDINET documents.json 失敗: status={resp.status}")
    return resp.json().get("results", [])


def fetch_document_csv(
    fetcher: Fetcher, doc_id: str, *, key: str | None = None
) -> bytes:
    """type=5 CSV（ZIP バイナリ）を取得する。"""
    resp = fetcher.fetch(
        f"{_API_BASE}/api/v2/documents/{doc_id}",
        params={"type": "5"},
        headers=_key_headers(key),
    )
    if not resp.ok:
        raise RuntimeError(f"EDINET type=5 失敗（{doc_id}）: status={resp.status}")
    return resp.body


def ingest_documents(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    documents: list[dict[str, Any]],
    *,
    fetch_csv: bool = True,
    key: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """書類一覧を ``docs.documents`` へ冪等取込し、type=5 CSV を証憑へ保存する。

    冪等キーは ``docID``（EDINET の書類 ID）。``{'written', 'total', 'csv_saved'}``。
    """
    as_of = as_of or datetime.now(UTC)
    written = 0
    csv_saved = 0
    for doc in documents:
        doc_id_ext = doc.get("docID", "")
        # 書類種別コードのみ持つ行（提出者不在・取消等）はスキップ。
        if not doc_id_ext:
            continue
        title = doc.get("docDescription") or doc.get("filerName") or doc_id_ext
        published_at = None
        submit_dt = doc.get("submitDateTime")
        if submit_dt:
            try:
                published_at = datetime.fromisoformat(submit_dt).replace(tzinfo=UTC)
            except ValueError:
                published_at = None
        res = base.upsert_document(
            conn, run, store,
            source_type="filing", source_name=SOURCE_NAME,
            title=title, body=doc.get("docDescription"),
            url=None, lang="ja",
            published_at=published_at, as_of=as_of,
            meta={
                "docID": doc_id_ext,
                "filerName": doc.get("filerName"),
                "secCode": doc.get("secCode"),
                "docTypeCode": doc.get("docTypeCode"),
            },
            raw_payload=doc, evidence_kind="edinet_meta",
            hash_source=f"{SOURCE_NAME}:{doc_id_ext}",
        )
        if not res.created:
            continue
        written += 1
        # type=5 CSV を取得して証憑へ追加保存し、文書 → 証憑のリネージ辺を張る。
        if fetch_csv:
            csv_bytes = fetch_document_csv(fetcher, doc_id_ext, key=key)
            evidence_id, _ = base.save_raw(
                conn, store, kind="edinet_csv",
                payload=csv_bytes, source=SOURCE_NAME,
            )
            record(conn, run, [("documents", res.doc_id)], [("evidence", evidence_id)])
            csv_saved += 1
    return {"written": written, "total": len(documents), "csv_saved": csv_saved}


def ingest_date(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    *,
    target_date: str,
    fetch_csv: bool = True,
    key: str | None = None,
) -> dict[str, int]:
    """指定日の EDINET 開示を取り込む。"""
    key = key if key is not None else api_key()
    docs = fetch_document_list(fetcher, target_date, key=key)
    return ingest_documents(
        conn, run, store, fetcher, docs, fetch_csv=fetch_csv, key=key
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EDINET v2 開示取込")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--no-csv", action="store_true", help="type=5 CSV 取得を省略")
    args = parser.parse_args(argv)

    store = base.default_store()
    fetcher = base.default_fetcher()
    conn = connect(autocommit=True)
    try:
        with run_ctx("ingest.edinet.daily", {"date": args.date}, conn=conn) as r:
            result = ingest_date(
                conn, r, store, fetcher,
                target_date=args.date, fetch_csv=not args.no_csv,
            )
        print(f"edinet {args.date}: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
