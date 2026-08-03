"""海外中銀取込テスト（HTTP 全モック）。設定・期間解析・ECB/BOE/IMF・冪等・異常。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ryza.ingest import intl_banks
from ryza.ingest.intl_banks import IntlSeries

_ECB_PAYLOAD = {
    "dataSets": [
        {
            "series": {
                "0:0:0:0:0": {
                    "observations": {"0": [1.0812], "1": [1.0854], "2": [None]}
                }
            }
        }
    ],
    "structure": {
        "dimensions": {
            "series": [
                {"id": "FREQ", "values": [{"id": "D"}]},
                {"id": "CURRENCY", "values": [{"id": "USD"}]},
                {"id": "CURRENCY_DENOM", "values": [{"id": "EUR"}]},
                {"id": "EXR_TYPE", "values": [{"id": "SP00"}]},
                {"id": "EXR_SUFFIX", "values": [{"id": "A"}]},
            ],
            "observation": [
                {
                    "id": "TIME_PERIOD",
                    "values": [
                        {"id": "2026-07-30"}, {"id": "2026-07-31"}, {"id": "2026-08-03"}
                    ],
                }
            ],
        }
    },
}

_BOE_CSV = b"""DATE,IUDBEDR
30 Jul 2026,4.00
31 Jul 2026,4.00
03 Aug 2026,
"""

_IMF_PAYLOAD = {
    "CompactData": {
        "DataSet": {
            "Series": {
                "@FREQ": "M",
                "@REF_AREA": "JP",
                "@INDICATOR": "PCPI_IX",
                # 要素 1 件 → 配列でなく単一オブジェクト（IMF の仕様どおり）。
                "Obs": {"@TIME_PERIOD": "2026-06", "@OBS_VALUE": "111.2"},
            }
        }
    }
}


def test_load_series_active_only():
    series = intl_banks.load_series()
    assert {"ecb", "boe", "imf"} <= {s.provider for s in series}
    ecb = next(s for s in series if s.path == "EXR/D.USD.EUR.SP00.A")
    assert ecb.code_base == "ECB:EXR.D.USD.EUR.SP00.A"


def test_load_series_rejects_unknown_provider(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("series:\n  - provider: fed\n    path: X\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fed"):
        intl_banks.load_series(p)


def test_parse_period():
    assert intl_banks.parse_period("2026") == datetime(2026, 1, 1, tzinfo=UTC)
    assert intl_banks.parse_period("2026-06") == datetime(2026, 6, 1, tzinfo=UTC)
    assert intl_banks.parse_period("2026-Q2") == datetime(2026, 6, 1, tzinfo=UTC)
    assert intl_banks.parse_period("2026-S2") == datetime(2026, 12, 1, tzinfo=UTC)
    assert intl_banks.parse_period("2026-W05") == datetime(2026, 1, 26, tzinfo=UTC)
    assert intl_banks.parse_period("2026-06-15") == datetime(2026, 6, 15, tzinfo=UTC)
    assert intl_banks.parse_period("garbage") is None


def test_parse_ecb_single_series_no_suffix():
    points = intl_banks.parse_ecb(_ECB_PAYLOAD)
    assert points == [
        ("", "2026-07-30", 1.0812),
        ("", "2026-07-31", 1.0854),
        ("", "2026-08-03", None),
    ]


def test_parse_ecb_multi_series_suffix():
    payload = {
        "dataSets": [
            {
                "series": {
                    "0:0": {"observations": {"0": [1.0]}},
                    "0:1": {"observations": {"0": [2.0]}},
                }
            }
        ],
        "structure": {
            "dimensions": {
                "series": [
                    {"id": "FREQ", "values": [{"id": "D"}]},
                    {"id": "CURRENCY", "values": [{"id": "USD"}, {"id": "JPY"}]},
                ],
                "observation": [
                    {"id": "TIME_PERIOD", "values": [{"id": "2026-08-01"}]}
                ],
            }
        },
    }
    points = intl_banks.parse_ecb(payload)
    assert ("D.USD", "2026-08-01", 1.0) in points
    assert ("D.JPY", "2026-08-01", 2.0) in points


def test_parse_boe_csv():
    points = intl_banks.parse_boe(_BOE_CSV)
    # 空欄（欠測）は値 None で返り ingest_points 側でスキップされる。
    assert points == [
        ("", "2026-07-30", "4.00"),
        ("", "2026-07-31", "4.00"),
        ("", "2026-08-03", None),
    ]


def test_parse_imf_single_obs_object():
    assert intl_banks.parse_imf(_IMF_PAYLOAD) == [("", "2026-06", "111.2")]


def test_ingest_series_ecb(conn, run, store, fetcher):
    fetcher.add_json("data-api.ecb.europa.eu", _ECB_PAYLOAD)
    s = IntlSeries(provider="ecb", path="EXR/D.USD.EUR.SP00.A")
    res = intl_banks.ingest_series(conn, run, store, fetcher, s)
    assert res == {"written": 2, "total": 3}  # None は非数値としてスキップ
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.indicators "
            "WHERE series_code = 'ECB:EXR.D.USD.EUR.SP00.A'"
        )
        assert cur.fetchone()[0] == 2
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='indicators' AND to_kind='evidence'"
        )
        assert cur.fetchone()[0] == 2


def test_ingest_series_boe(conn, run, store, fetcher):
    fetcher.add_bytes("bankofengland", _BOE_CSV)
    s = IntlSeries(provider="boe", path="IUDBEDR")
    res = intl_banks.ingest_series(conn, run, store, fetcher, s)
    assert res == {"written": 2, "total": 3}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM market.indicators "
            "WHERE series_code = 'BOE:IUDBEDR' ORDER BY ts"
        )
        assert [float(r[0]) for r in cur.fetchall()] == [4.0, 4.0]


def test_ingest_series_imf(conn, run, store, fetcher):
    fetcher.add_json("dataservices.imf.org", _IMF_PAYLOAD)
    s = IntlSeries(provider="imf", path="IFS/M.JP.PCPI_IX")
    res = intl_banks.ingest_series(conn, run, store, fetcher, s)
    assert res == {"written": 1, "total": 1}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts FROM market.indicators WHERE series_code = 'IMF:IFS.M.JP.PCPI_IX'"
        )
        assert cur.fetchone()[0] == datetime(2026, 6, 1, tzinfo=UTC)


def test_ingest_series_idempotent(conn, run, store, fetcher):
    fetcher.add_bytes("bankofengland", _BOE_CSV)
    s = IntlSeries(provider="boe", path="IUDBEDR")
    r1 = intl_banks.ingest_series(conn, run, store, fetcher, s)
    r2 = intl_banks.ingest_series(conn, run, store, fetcher, s)
    assert r1["written"] == 2
    assert r2["written"] == 0


def test_ingest_all_continues_on_error(conn, run, store, fetcher):
    fetcher.add_json("dataservices.imf.org", _IMF_PAYLOAD)
    # ECB は未登録 → 404 → RuntimeError を握って errors に計上。
    series = [
        IntlSeries(provider="ecb", path="EXR/D.USD.EUR.SP00.A"),
        IntlSeries(provider="imf", path="IFS/M.JP.PCPI_IX"),
    ]
    res = intl_banks.ingest_all(conn, run, store, fetcher, series)
    assert res["errors"] == 1
    assert res["written"] == 1


def test_indicator_as_of_and_run_recorded(conn, run, store, fetcher):
    as_of = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    fetcher.add_json("dataservices.imf.org", _IMF_PAYLOAD)
    s = IntlSeries(provider="imf", path="IFS/M.JP.PCPI_IX")
    intl_banks.ingest_series(conn, run, store, fetcher, s, as_of=as_of)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT as_of, run_id FROM market.indicators "
            "WHERE series_code = 'IMF:IFS.M.JP.PCPI_IX'"
        )
        row = cur.fetchone()
    assert row[0] == as_of
    assert row[1] == run.run_id
