"""EDINET v2 取込テスト（HTTP 全モック）。正常・type=5 CSV・重複・異常。"""

from __future__ import annotations

import pytest

from ryza.ingest import edinet


def _docs_response():
    return {
        "results": [
            {"docID": "S100AAAA", "filerName": "トヨタ自動車",
             "docDescription": "有価証券報告書", "secCode": "72030",
             "docTypeCode": "120", "submitDateTime": "2026-08-03 10:00"},
            {"docID": "S100BBBB", "filerName": "ソニーグループ",
             "docDescription": "大量保有報告書", "secCode": "67580",
             "docTypeCode": "350", "submitDateTime": "2026-08-03 11:00"},
        ]
    }


def _wire(fetcher):
    fetcher.add_json("documents.json", _docs_response())
    fetcher.add_bytes("documents/S100AAAA", b"PK\x03\x04csvzip-A")
    fetcher.add_bytes("documents/S100BBBB", b"PK\x03\x04csvzip-B")


def test_ingest_date_ok_with_csv(conn, run, store, fetcher):
    _wire(fetcher)
    res = edinet.ingest_date(
        conn, run, store, fetcher, target_date="2026-08-03", key="KEY"
    )
    assert res["written"] == 2
    assert res["csv_saved"] == 2

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.documents WHERE source_name='EDINET'")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM ledger.evidence WHERE kind='edinet_csv'")
        assert cur.fetchone()[0] == 2
        # 文書 → CSV 証憑のリネージ辺。
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='documents' AND to_kind='evidence'"
        )
        assert cur.fetchone()[0] >= 2


def test_ingest_no_csv(conn, run, store, fetcher):
    _wire(fetcher)
    res = edinet.ingest_date(
        conn, run, store, fetcher,
        target_date="2026-08-03", key="KEY", fetch_csv=False,
    )
    assert res["written"] == 2
    assert res["csv_saved"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ledger.evidence WHERE kind='edinet_csv'")
        assert cur.fetchone()[0] == 0


def test_ingest_idempotent(conn, run, store, fetcher):
    _wire(fetcher)
    r1 = edinet.ingest_date(conn, run, store, fetcher, target_date="2026-08-03", key="K")
    r2 = edinet.ingest_date(conn, run, store, fetcher, target_date="2026-08-03", key="K")
    assert r1["written"] == 2
    assert r2["written"] == 0  # docID 冪等
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.documents WHERE source_name='EDINET'")
        assert cur.fetchone()[0] == 2


def test_document_list_http_error(conn, run, store, fetcher):
    fetcher.add_status("documents.json", 500)
    with pytest.raises(RuntimeError):
        edinet.ingest_date(conn, run, store, fetcher, target_date="2026-08-03", key="K")
