"""FRED 取込テスト（HTTP 全モック）。設定読込・正常・欠測スキップ・冪等・改定・異常。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ryza.ingest import fred
from ryza.ingest.fred import Series


def _payload(values):
    return {
        "observations": [
            {"date": d, "value": v} for d, v in values
        ]
    }


def test_load_series_active_only():
    ids = {s.id for s in fred.load_series()}
    assert {"DGS10", "CPIAUCSL", "UNRATE"} <= ids


def test_api_key_missing_raises(monkeypatch):
    monkeypatch.delenv("RYZA_FRED_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(fred.FredAuthError):
        fred.api_key()


def test_ingest_series_writes_indicators_with_prefix(conn, run, store):
    payload = _payload([("2026-08-01", "4.25"), ("2026-08-02", "."),
                        ("2026-08-03", "4.30")])
    res = fred.ingest_series(conn, run, store, payload, series_id="DGS10")
    assert res["written"] == 2  # 欠測 '.' はスキップ
    assert res["total"] == 3
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.indicators WHERE series_code='FRED:DGS10'"
        )
        assert cur.fetchone()[0] == 2
        # 証憑リネージ辺。
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='indicators' AND to_kind='evidence'"
        )
        assert cur.fetchone()[0] == 2


def test_ingest_series_idempotent(conn, run, store):
    payload = _payload([("2026-08-01", "4.25")])
    r1 = fred.ingest_series(conn, run, store, payload, series_id="DGS10")
    r2 = fred.ingest_series(conn, run, store, payload, series_id="DGS10")
    assert r1["written"] == 1
    assert r2["written"] == 0  # 同値の再取込は増えない
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM market.indicators WHERE series_code='FRED:DGS10'")
        assert cur.fetchone()[0] == 1


def test_ingest_series_revision_on_value_change(conn, run, store):
    fred.ingest_series(conn, run, store, _payload([("2026-08-01", "4.25")]),
                       series_id="DGS10")
    # 同一 ts で値が改定 → revision が進み追記される。
    fred.ingest_series(conn, run, store, _payload([("2026-08-01", "4.28")]),
                       series_id="DGS10")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT revision, value FROM market.indicators "
            "WHERE series_code='FRED:DGS10' ORDER BY revision"
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == 0
    assert rows[1][0] == 1


def test_ingest_all_uses_fetcher_and_key(conn, run, store, fetcher):
    fetcher.add_json("series/observations", _payload([("2026-08-01", "3.5")]))
    res = fred.ingest_all(
        conn, run, store, fetcher, [Series(id="UNRATE")], key="KEY"
    )
    assert res["written"] == 1
    assert res["errors"] == 0


def test_ingest_all_continues_on_error(conn, run, store, fetcher):
    # observations 未登録 → 404 → RuntimeError を握って errors に計上。
    res = fred.ingest_all(
        conn, run, store, fetcher, [Series(id="BADSERIES")], key="KEY"
    )
    assert res["errors"] == 1
    assert res["written"] == 0


def test_indicator_as_of_recorded(conn, run, store):
    as_of = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    fred.ingest_series(conn, run, store, _payload([("2026-08-01", "4.25")]),
                       series_id="DGS10", as_of=as_of)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT as_of, run_id FROM market.indicators WHERE series_code='FRED:DGS10'"
        )
        row = cur.fetchone()
    assert row[0] == as_of
    assert row[1] == run.run_id
