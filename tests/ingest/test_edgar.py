"""EDGAR 取込テスト（HTTP 全モック）。設定読込・submissions・companyfacts・冪等・異常。"""

from __future__ import annotations

from datetime import UTC, datetime

from ryza.ingest import edgar
from ryza.ingest.edgar import Company

_SUBMISSIONS = {
    "cik": "320193",
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
            "form": ["10-K", "13F-HR"],
            "filingDate": ["2026-07-30", "2026-08-01"],
            "primaryDocument": ["aapl-10k.htm", ""],
            "primaryDocDescription": ["10-K", ""],
        }
    },
}

_COMPANYFACTS = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        # frame 付き（採用）。
                        {"end": "2026-06-27", "val": 94000000000, "filed": "2026-07-31",
                         "frame": "CY2026Q2"},
                        # frame 無し（重複期間の生ファクト → スキップ）。
                        {"end": "2026-06-27", "val": 94000000001, "filed": "2026-07-31"},
                    ]
                }
            },
            "IgnoredTag": {
                "units": {"USD": [{"end": "2026-06-27", "val": 1, "frame": "CY2026Q2"}]}
            },
        }
    },
}


def test_cik10_normalizes():
    assert edgar.cik10("320193") == "0000320193"
    assert edgar.cik10(320193) == "0000320193"
    assert edgar.cik10("CIK0000320193") == "0000320193"


def test_load_companies_active_only():
    companies = edgar.load_companies()
    ciks = {c.cik for c in companies}
    assert "0000320193" in ciks
    # 13F 提出者は facts: false。
    berkshire = next(c for c in companies if c.cik == "0001067983")
    assert berkshire.facts is False


def test_ingest_submissions_writes_documents(conn, run, store):
    res = edgar.ingest_submissions(conn, run, store, _SUBMISSIONS, cik="320193")
    assert res == {"written": 2, "total": 2}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, url, meta->>'form' FROM docs.documents "
            "WHERE source_name='EDGAR' ORDER BY doc_id"
        )
        rows = cur.fetchall()
    assert rows[0][0] == "Apple Inc. 10-K (2026-07-30)"
    assert rows[0][1] == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl-10k.htm"
    )
    # 13F-HR も同一経路で取り込まれる（primaryDocument 無し → url なし）。
    assert rows[1][2] == "13F-HR"
    assert rows[1][1] is None


def test_ingest_submissions_idempotent(conn, run, store):
    r1 = edgar.ingest_submissions(conn, run, store, _SUBMISSIONS, cik="320193")
    r2 = edgar.ingest_submissions(conn, run, store, _SUBMISSIONS, cik="320193")
    assert r1["written"] == 2
    assert r2["written"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.documents WHERE source_name='EDGAR'")
        assert cur.fetchone()[0] == 2


def test_ingest_submissions_form_filter(conn, run, store):
    res = edgar.ingest_submissions(
        conn, run, store, _SUBMISSIONS, cik="320193", forms={"13F-HR"}
    )
    assert res == {"written": 1, "total": 1}


def test_ingest_company_facts_frames_only_with_lineage(conn, run, store):
    res = edgar.ingest_company_facts(
        conn, run, store, _COMPANYFACTS, cik="320193"
    )
    # frame 無しはスキップ、対象外タグ（IgnoredTag）は数えない。
    assert res == {"written": 1, "total": 1}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value, as_of FROM market.indicators "
            "WHERE series_code = 'EDGAR:0000320193:us-gaap:Revenues:USD'"
        )
        row = cur.fetchone()
        # as_of は filed（提出日）＝point-in-time。
        assert float(row[0]) == 94000000000
        assert row[1] == datetime(2026, 7, 31, tzinfo=UTC)
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='indicators' AND to_kind='evidence'"
        )
        assert cur.fetchone()[0] == 1


def test_ingest_company_facts_idempotent(conn, run, store):
    r1 = edgar.ingest_company_facts(conn, run, store, _COMPANYFACTS, cik="320193")
    r2 = edgar.ingest_company_facts(conn, run, store, _COMPANYFACTS, cik="320193")
    assert r1["written"] == 1
    assert r2["written"] == 0


def test_ingest_all_uses_fetcher(conn, run, store, fetcher):
    fetcher.add_json("submissions/CIK0000320193.json", _SUBMISSIONS)
    fetcher.add_json("companyfacts/CIK0000320193.json", _COMPANYFACTS)
    res = edgar.ingest_all(
        conn, run, store, fetcher, [Company(cik="0000320193")]
    )
    assert res["documents"] == 2
    assert res["indicators"] == 1
    assert res["errors"] == 0
    assert any("submissions/CIK0000320193.json" in c for c in fetcher.calls)


def test_ingest_all_skips_facts_for_13f_filer(conn, run, store, fetcher):
    fetcher.add_json("submissions/CIK0001067983.json", _SUBMISSIONS)
    res = edgar.ingest_all(
        conn, run, store, fetcher, [Company(cik="0001067983", facts=False)]
    )
    assert res["errors"] == 0
    # companyfacts は呼ばれない。
    assert not any("companyfacts" in c for c in fetcher.calls)


def test_ingest_all_continues_on_error(conn, run, store, fetcher):
    # submissions 未登録 → 404 → RuntimeError を握って errors に計上。
    res = edgar.ingest_all(conn, run, store, fetcher, [Company(cik="0000000001")])
    assert res["errors"] == 1
    assert res["documents"] == 0
