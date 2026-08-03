"""J-Quants V2 取込テスト（HTTP 全モック）。

正常系（API キー→日足→bars 書込・SCD2・証憑・リネージ）・重複（冪等）・
認証（API キー未設定）・ページネーション。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ryza.ingest import jquants
from ryza.ingest.base import FetchResult


def test_api_key_env(monkeypatch):
    monkeypatch.setenv("RYZA_JQUANTS_API_KEY", "KEY123")
    assert jquants.api_key() == "KEY123"


def test_api_key_env_takes_priority_over_secret(monkeypatch, fake_secret_manager):
    """env があれば Secret Manager へアクセスしない(Issue #30)。"""
    calls = fake_secret_manager({"jquants-api-key": "SMKEY"})
    monkeypatch.setenv("RYZA_JQUANTS_API_KEY", "ENVKEY")
    monkeypatch.setenv("GCP_PROJECT", "proj")
    assert jquants.api_key() == "ENVKEY"
    assert calls == []


def test_api_key_secret_manager_fallback(monkeypatch, fake_secret_manager):
    """env 未設定でも VM(GCP_PROJECT あり)なら Secret 'jquants-api-key' から取得。"""
    fake_secret_manager({"jquants-api-key": "SMKEY"})
    monkeypatch.delenv("RYZA_JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "proj")
    assert jquants.api_key() == "SMKEY"


def test_api_key_missing_raises(monkeypatch, fake_secret_manager):
    """env も Secret も無ければ JQuantsAuthError(daily では skipped 扱い)。"""
    fake_secret_manager({})  # Secret 未登録(404)
    monkeypatch.delenv("RYZA_JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    with pytest.raises(jquants.JQuantsAuthError):
        jquants.api_key()


def test_auth_headers_uses_x_api_key():
    assert jquants._auth_headers("KEY123") == {"x-api-key": "KEY123"}


def test_fetch_daily_quotes_sends_api_key_header(fetcher):
    fetcher.add("equities/bars/daily", FetchResult(
        status=200, body=b'{"data": [{"Code": "72030"}]}',
    ))
    quotes = jquants.fetch_daily_quotes(fetcher, "KEY123", "2026-08-03")
    assert quotes == [{"Code": "72030"}]


def test_fetch_paginates_across_pages(fetcher):
    # 1 ページ目は pagination_key を返し、2 ページ目で終端。部分一致で同一 URL に
    # 2 回目以降は key 無しレスポンスを当てるため、先に登録したルートが優先される
    # のを避けて 2 番目のルートを登録順で後にする。
    class Paging:
        def __init__(self):
            self.n = 0

        def fetch(self, url, *, params=None, headers=None, method="GET", data=None):
            self.n += 1
            if self.n == 1:
                return FetchResult(
                    status=200,
                    body=b'{"data": [{"Code": "72030"}], "pagination_key": "K2"}',
                )
            return FetchResult(status=200, body=b'{"data": [{"Code": "67580"}]}')

    rows = jquants._fetch_all(Paging(), "/v2/equities/master", key="KEY")
    assert [r["Code"] for r in rows] == ["72030", "67580"]


def _quotes():
    return [
        {"Code": "72030", "O": 100, "H": 110, "L": 95, "C": 105, "Vo": 1000},
        {"Code": "67580", "O": 50, "H": 55, "L": 48, "C": 52, "Vo": 500},
    ]


def test_ingest_daily_quotes_writes_bars_with_lineage(conn, run, store):
    as_of = datetime.now(UTC)
    raw = b'{"data": []}'
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

    # 各バーに証憑リネージ辺(共有 DB の既存辺と区別するため自 run に絞る)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='bars' AND to_kind='evidence' AND run_id=%s",
            (run.run_id,),
        )
        assert cur.fetchone()[0] == 2


def test_ingest_daily_quotes_maps_v2_ohlcv(conn, run, store):
    """V2 の O/H/L/C/Vo カラムが bars の OHLCV に正しく対応する。"""
    as_of = datetime.now(UTC)
    jquants.ingest_daily_quotes(
        conn, run, store, _quotes()[:1],
        quote_date="2026-08-03", raw_response=b"{}", as_of=as_of,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open, high, low, close, volume FROM market.bars b "
            "JOIN market.instruments i USING (instrument_id) "
            "WHERE i.symbol='7203.T' AND b.source='jquants' AND b.run_id=%s",
            (run.run_id,),
        )
        assert cur.fetchone() == (100, 110, 95, 105, 1000)


def test_ingest_daily_quotes_idempotent(conn, run, store):
    as_of = datetime.now(UTC)
    kw = dict(quote_date="2026-08-03", raw_response=b"{}", as_of=as_of)
    r1 = jquants.ingest_daily_quotes(conn, run, store, _quotes(), **kw)
    r2 = jquants.ingest_daily_quotes(conn, run, store, _quotes(), **kw)
    assert r1["written"] == 2
    assert r2["written"] == 0  # 同一 PK は増えない
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.bars WHERE source='jquants' AND run_id=%s",
            (run.run_id,),
        )
        assert cur.fetchone()[0] == 2


def test_ingest_statements_as_documents(conn, run, store):
    statements = [
        {"Code": "72030", "DiscDate": "2026-08-03",
         "DiscNo": "20260803001", "DocType": "FYFinancialStatements"},
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


def test_run_daily_full_flow(conn, run, store, fetcher):
    fetcher.add("equities/master", FetchResult(
        status=200,
        body=b'{"data": [{"Code": "72030"}]}',
    ))
    fetcher.add("equities/bars/daily", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","O":1,"H":2,"L":1,"C":2,"Vo":10}]}',
    ))
    result = jquants.run_daily(
        conn, run, store, fetcher, quote_date="2026-08-03", key="KEY123"
    )
    assert result.bars["written"] == 1
    assert result.instruments["resolved"] == 1


def test_normalize_symbol():
    assert jquants._normalize_symbol("72030") == "7203.T"
    assert jquants._normalize_symbol("7203") == "7203.T"
