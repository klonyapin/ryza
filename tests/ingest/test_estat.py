"""e-Stat 取込テスト（HTTP 全モック）。設定・時刻コード・正常・冪等・ページング・異常。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ryza.ingest import estat
from ryza.ingest.estat import StatsTable


def _payload(values, *, next_key=None, status=0, error_msg=""):
    result_inf = {"TOTAL_NUMBER": len(values) if isinstance(values, list) else 1}
    if next_key is not None:
        result_inf["NEXT_KEY"] = next_key
    return {
        "GET_STATS_DATA": {
            "RESULT": {"STATUS": status, "ERROR_MSG": error_msg},
            "STATISTICAL_DATA": {
                "RESULT_INF": result_inf,
                "DATA_INF": {"VALUE": values},
            },
        }
    }


def test_load_tables_active_only():
    ids = {t.id for t in estat.load_tables()}
    assert "0003427113" in ids


def test_app_id_missing_raises(monkeypatch):
    monkeypatch.delenv("RYZA_ESTAT_APP_ID", raising=False)
    monkeypatch.delenv("ESTAT_APP_ID", raising=False)
    with pytest.raises(estat.EstatAuthError):
        estat.app_id()


def test_parse_time_code():
    # 年次 / 月次 / 四半期（期末月）/ 解釈不能。
    assert estat.parse_time_code("2026000000") == datetime(2026, 1, 1, tzinfo=UTC)
    assert estat.parse_time_code("2026000606") == datetime(2026, 6, 1, tzinfo=UTC)
    assert estat.parse_time_code("2026000103") == datetime(2026, 3, 1, tzinfo=UTC)
    assert estat.parse_time_code("bad") is None


def test_series_code_excludes_time_and_unit():
    row = {"@tab": "1", "@cat01": "0001", "@time": "2026000606", "@unit": "円", "$": "1"}
    assert estat.series_code("0003427113", row) == "ESTAT:0003427113:0001:1"


def test_ingest_payload_writes_indicators(conn, run, store):
    values = [
        {"@cat01": "0001", "@time": "2026000606", "$": "102.3"},
        {"@cat01": "0002", "@time": "2026000606", "$": "98.1"},
        {"@cat01": "0001", "@time": "2026000505", "$": "-"},   # 欠測記号 → skip
    ]
    res = estat.ingest_payload(
        conn, run, store, _payload(values), stats_data_id="0003427113"
    )
    assert res == {"written": 2, "total": 3}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT series_code, value FROM market.indicators "
            "WHERE series_code LIKE 'ESTAT:%' ORDER BY series_code"
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["ESTAT:0003427113:0001", "ESTAT:0003427113:0002"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='indicators' AND to_kind='evidence'"
        )
        assert cur.fetchone()[0] == 2


def test_ingest_payload_single_value_object(conn, run, store):
    # e-Stat は 1 件のとき VALUE が配列でなく単一オブジェクトになる。
    res = estat.ingest_payload(
        conn, run, store,
        _payload({"@time": "2026000000", "$": "1.5"}),
        stats_data_id="X",
    )
    assert res == {"written": 1, "total": 1}


def test_ingest_payload_idempotent(conn, run, store):
    values = [{"@cat01": "0001", "@time": "2026000606", "$": "102.3"}]
    r1 = estat.ingest_payload(conn, run, store, _payload(values), stats_data_id="X")
    r2 = estat.ingest_payload(conn, run, store, _payload(values), stats_data_id="X")
    assert r1["written"] == 1
    assert r2["written"] == 0


def test_fetch_stats_data_status_error(fetcher):
    fetcher.add_json("getStatsData", _payload([], status=100, error_msg="認証エラー"))
    with pytest.raises(RuntimeError, match="STATUS=100"):
        estat.fetch_stats_data(fetcher, "X", key="BADKEY")


def test_ingest_table_follows_next_key(conn, run, store, fetcher):
    page1 = _payload(
        [{"@time": "2026000505", "$": "1.0"}], next_key=2
    )
    page2 = _payload([{"@time": "2026000606", "$": "2.0"}])
    fetcher.add_json("startPosition=2", page2)   # 具体的な方を先に登録（部分一致のため）
    fetcher.add_json("getStatsData", page1)
    res = estat.ingest_table(
        conn, run, store, fetcher, StatsTable(id="X"), key="KEY"
    )
    assert res == {"written": 2, "total": 2}
    assert len(fetcher.calls) == 2


def test_ingest_all_continues_on_error(conn, run, store, fetcher):
    fetcher.add_json("statsDataId=OK", _payload([{"@time": "2026000000", "$": "3.0"}]))
    # BAD は未登録 → 404 → RuntimeError を握って errors に計上。
    res = estat.ingest_all(
        conn, run, store, fetcher,
        [StatsTable(id="BAD"), StatsTable(id="OK")], key="KEY",
    )
    assert res["errors"] == 1
    assert res["written"] == 1


def test_indicator_as_of_and_run_recorded(conn, run, store):
    as_of = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    estat.ingest_payload(
        conn, run, store,
        _payload([{"@time": "2026000606", "$": "1.0"}]),
        stats_data_id="X", as_of=as_of,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT as_of, run_id FROM market.indicators WHERE series_code='ESTAT:X'"
        )
        row = cur.fetchone()
    assert row[0] == as_of
    assert row[1] == run.run_id
