"""J-Quants 取込テスト（HTTP 全モック）。

正常系（認証→日足→bars 書込・SCD2・証憑・リネージ）・重複（冪等）・異常系（認証失敗）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ryza.ingest import jquants
from ryza.ingest.base import FetchResult


def _auth(fetcher):
    fetcher.add_json("token/auth_refresh", {"idToken": "ID_TOKEN"})


def test_authenticate_ok(fetcher):
    _auth(fetcher)
    assert jquants.authenticate(fetcher, "REFRESH") == "ID_TOKEN"


def test_authenticate_failure_raises(fetcher):
    fetcher.add_status("token/auth_refresh", 401)
    with pytest.raises(jquants.JQuantsAuthError):
        jquants.authenticate(fetcher, "REFRESH")


def test_refresh_token_missing_raises(monkeypatch):
    monkeypatch.delenv("RYZA_JQUANTS_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("JQUANTS_REFRESH_TOKEN", raising=False)
    with pytest.raises(jquants.JQuantsAuthError):
        jquants.refresh_token()


def _quotes():
    return [
        {"Code": "72030", "Open": 100, "High": 110, "Low": 95, "Close": 105,
         "Volume": 1000},
        {"Code": "67580", "Open": 50, "High": 55, "Low": 48, "Close": 52,
         "Volume": 500},
    ]


def test_ingest_daily_quotes_writes_bars_with_lineage(conn, run, store):
    as_of = datetime.now(UTC)
    raw = b'{"daily_quotes": []}'
    res = jquants.ingest_daily_quotes(
        conn, run, store, _quotes(),
        quote_date="2026-08-03", raw_response=raw, as_of=as_of,
    )
    assert res == {"written": 2, "total": 2}

    # SCD2 で 7203.T / 6758.T が自動登録されている。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.instruments "
            "WHERE symbol IN ('7203.T','6758.T') AND valid_to IS NULL"
        )
        assert cur.fetchone()[0] == 2

    # bars に run_id / as_of 付きで 2 本。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.bars "
            "WHERE source='jquants' AND run_id=%s AND as_of=%s",
            (run.run_id, as_of),
        )
        assert cur.fetchone()[0] == 2

    # 各バーに証憑リネージ辺。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='bars' AND to_kind='evidence'"
        )
        assert cur.fetchone()[0] == 2


def test_ingest_daily_quotes_idempotent(conn, run, store):
    as_of = datetime.now(UTC)
    kw = dict(quote_date="2026-08-03", raw_response=b"{}", as_of=as_of)
    r1 = jquants.ingest_daily_quotes(conn, run, store, _quotes(), **kw)
    r2 = jquants.ingest_daily_quotes(conn, run, store, _quotes(), **kw)
    assert r1["written"] == 2
    assert r2["written"] == 0  # 同一 PK は増えない
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM market.bars WHERE source='jquants'")
        assert cur.fetchone()[0] == 2


def test_ingest_statements_as_documents(conn, run, store):
    statements = [
        {"LocalCode": "72030", "DisclosedDate": "2026-08-03",
         "DisclosureNumber": "20260803001", "TypeOfDocument": "FYFinancialStatements"},
    ]
    r1 = jquants.ingest_statements(conn, run, store, statements)
    r2 = jquants.ingest_statements(conn, run, store, statements)
    assert r1["written"] == 1
    assert r2["written"] == 0  # 冪等
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type FROM docs.documents WHERE source_name='J-Quants'"
        )
        assert cur.fetchone()[0] == "filing"


def test_run_daily_full_flow(conn, run, store, fetcher, monkeypatch):
    monkeypatch.setenv("RYZA_JQUANTS_REFRESH_TOKEN", "REFRESH")
    _auth(fetcher)
    fetcher.add("listed/info", FetchResult(
        status=200,
        body=b'{"info": [{"Code": "72030"}]}',
    ))
    fetcher.add("daily_quotes", FetchResult(
        status=200,
        body=b'{"daily_quotes": [{"Code":"72030","Open":1,"High":2,"Low":1,'
             b'"Close":2,"Volume":10}]}',
    ))
    result = jquants.run_daily(conn, run, store, fetcher, quote_date="2026-08-03")
    assert result.bars["written"] == 1
    assert result.instruments["resolved"] == 1


def test_normalize_symbol():
    assert jquants._normalize_symbol("72030") == "7203.T"
    assert jquants._normalize_symbol("7203") == "7203.T"
