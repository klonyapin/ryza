"""preprocess.fundamentals テスト(T-029)。

モック payload(実フィールド名 = J-Quants /v2/fins/summary V2 命名)で
`docs.documents` + 証憑を用意し、`market.indicators` へ昇格されることを検証する:

1. 正常系: series_code / ts / as_of / value が仕様どおり書かれる
2. 欠測 skip: 一部フィールド欠測 → 該当項目のみ skip、他は書かれる、集計される
3. 訂正開示: 同一期の別 value → revision が進む(base.write_indicator の既存規約)
4. 冪等: 同一入力の再実行で書込 0
5. バックフィル: 複数文書の一括処理+冪等マーカ更新
6. リネージ: indicators→documents の辺が張られる

DB は tests/conftest.py の ``migrated_db`` フィクスチャ(テスト専用 DB)。
接続は commit せず rollback で隔離する(preprocess/conftest.py の ``conn``)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from ryza.ingest import base
from ryza.ingest.jquants import SOURCE_NAME as JQUANTS_SOURCE_NAME
from ryza.preprocess import fundamentals
from ryza.provenance import EvidenceStore, LocalStorage


# 実 J-Quants /v2/fins/summary の V2 命名に合わせた 1 開示 payload。
# 実績: FY 決算・連結・IFRS(FYFinancialStatements_Consolidated_IFRS)。
# 会社予想は当期分(FSales/FOP/FOdP/FNP/FEPS)まで持たせる(NxF* は欠測 → skip)。
def _payload_full() -> dict:
    return {
        "Code": "72030",
        "DiscDate": "2026-05-14",
        "DiscTime": "15:00:00",
        "DiscNo": "20260514001",
        "DocType": "FYFinancialStatements_Consolidated_IFRS",
        "CurPerType": "FY",
        "CurPerSt": "2025-04-01",
        "CurPerEn": "2026-03-31",
        "CurFYSt": "2025-04-01",
        "CurFYEn": "2026-03-31",
        "NxtFYSt": "2026-04-01",
        "NxtFYEn": "2027-03-31",
        "Sales": "45000000000",
        "OP": "3200000000",
        "OdP": "3300000000",
        "NP": "2400000000",
        "EPS": "180.5",
        "DEPS": "179.0",
        "FSales": "48000000000",
        "FOP": "3500000000",
        "FOdP": "3600000000",
        "FNP": "2600000000",
        "FEPS": "195.0",
        # NxF* は空(会社予想を出さない開示の再現)。
        "NxFSales": "",
        "NxFOP": "",
        "NxFOdP": "",
        "NxFNp": "",
        "NxFEPS": "",
    }


@pytest.fixture
def store(tmp_path):
    """``tmp_path`` 上の証憑ストア(LocalStorage)。ingest テストと同じ流儀。"""
    return EvidenceStore(LocalStorage(tmp_path / "evidence"))


def _upsert_jquants_doc(conn, run, store, payload: dict) -> int:
    """本番と同じ流儀で docs.documents + 証憑 + リネージを 1 件作る。

    ``ingest.jquants.ingest_statements`` と同一パスで呼ぶことで、リネージ辺
    (documents→evidence)も本番同様に張られ、fundamentals 側の証憑解決を
    フル経路で検証できる。
    """
    symbol = "7203.T"
    disclosed = payload.get("DiscDate", "")
    disc_no = payload.get("DiscNo", "")
    published_at = None
    if disclosed:
        try:
            published_at = datetime.fromisoformat(disclosed).replace(tzinfo=UTC)
        except ValueError:
            published_at = None
    res = base.upsert_document(
        conn, run, store,
        source_type="filing", source_name=JQUANTS_SOURCE_NAME,
        title=f"{symbol} 財務諸表 ({disclosed})", body=None, lang="ja",
        published_at=published_at, as_of=datetime.now(UTC),
        meta={"symbol": symbol, "kind": "financial_statement"},
        raw_payload=payload, evidence_kind="jquants_statement",
        hash_source=f"{JQUANTS_SOURCE_NAME}:{disclosed}:{disc_no}:{symbol}",
    )
    return res.doc_id


def _fetch_indicator(conn, series_code, ts):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value, as_of, revision FROM market.indicators "
            "WHERE series_code = %s AND ts = %s ORDER BY revision DESC LIMIT 1",
            (series_code, ts),
        )
        return cur.fetchone()


# ── 1. 正常系: series_code / ts / as_of / value を値で固定 ──────────────────
def test_promotes_actuals_and_forecasts(conn, run, store):
    payload = _payload_full()
    doc_id = _upsert_jquants_doc(conn, run, store, payload)
    result = fundamentals.run_promotion(conn, run, store, limit=10)
    assert result.processed == 1
    # 実績 6 項目(Sales/OP/OdP/NP/EPS/DEPS)+ 現行予想 5 項目(F*) = 11 点。
    # NxF* は "" のため書き込まれない(no_value)。
    assert result.written == 11
    assert result.total_extractions == 11
    assert result.skip["no_value"] == 5  # NxF* 5 項目

    # 実績・売上(FY・Consolidated・ts=2026-03-31)
    row = _fetch_indicator(
        conn,
        "JQUANTS:7203.T:NetSales:FY:Consolidated",
        datetime(2026, 3, 31, tzinfo=UTC),
    )
    assert row is not None
    # as_of は開示日時(2026-05-14 15:00 JST = 06:00 UTC)。**開示時点で固定**
    # されていることを値で確認する(point-in-time — T-029 §1-2)。
    assert float(row[0]) == 45000000000
    assert row[1] == datetime(2026, 5, 14, 6, 0, tzinfo=UTC)
    assert row[2] == 0

    # 会社予想・EPS(FY・Consolidated・予想対象期末 CurFYEn=2026-03-31)。
    # 実績 EPS と別 field 名(FcstEarningsPerShare)で分離されている。
    row = _fetch_indicator(
        conn,
        "JQUANTS:7203.T:FcstEarningsPerShare:FY:Consolidated",
        datetime(2026, 3, 31, tzinfo=UTC),
    )
    assert row is not None
    assert float(row[0]) == 195.0
    # 昇格した文書に processed マーカーが刻まれている。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT meta->>'fundamentals_version' FROM docs.documents WHERE doc_id=%s",
            (doc_id,),
        )
        assert cur.fetchone()[0] == fundamentals.FUNDAMENTALS_VERSION


# ── 2. 欠測 skip: 実績のうち一部フィールドが欠測 → 他は書かれる、集計に載る ───
def test_missing_fields_skipped_and_counted(conn, run, store):
    payload = _payload_full()
    # 実績 OdP を欠測(""),NP を非数値。他はそのまま。
    payload["OdP"] = ""
    payload["NP"] = "not a number"
    _upsert_jquants_doc(conn, run, store, payload)
    result = fundamentals.run_promotion(conn, run, store, limit=10)
    # 実績 4(Sales/OP/EPS/DEPS) + 予想 5(F*) = 9 点。
    assert result.written == 9
    # skip: 実績 2(OdP・NP) + NxF* 5 = 7 件が no_value に計上される。
    assert result.skip["no_value"] == 7
    # 欠測の項目は市場系列に**書かれていない**(fail-closed)。
    row = _fetch_indicator(
        conn,
        "JQUANTS:7203.T:OrdinaryProfit:FY:Consolidated",
        datetime(2026, 3, 31, tzinfo=UTC),
    )
    assert row is None


# ── 3. 訂正開示: 同一 (series_code, ts) の別 value → revision++ ─────────────
def test_amended_statement_bumps_revision(conn, run, store):
    payload = _payload_full()
    _upsert_jquants_doc(conn, run, store, payload)
    fundamentals.run_promotion(conn, run, store, limit=10)

    # 訂正開示: 同じ期(CurPerEn=2026-03-31)で Sales を修正。DiscNo/DiscDate を変えて
    # docs.documents の content_hash 衝突を避ける(訂正は別文書として取り込まれる)。
    amended = _payload_full()
    amended["DiscDate"] = "2026-05-20"
    amended["DiscNo"] = "20260520002"
    amended["Sales"] = "45500000000"
    _upsert_jquants_doc(conn, run, store, amended)
    fundamentals.run_promotion(conn, run, store, limit=10)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT revision, value FROM market.indicators "
            "WHERE series_code=%s AND ts=%s ORDER BY revision ASC",
            (
                "JQUANTS:7203.T:NetSales:FY:Consolidated",
                datetime(2026, 3, 31, tzinfo=UTC),
            ),
        )
        rows = cur.fetchall()
    # 2 revision が並ぶ: 初版(45000000000)+ 改定(45500000000)。
    assert [(r[0], float(r[1])) for r in rows] == [
        (0, 45000000000.0),
        (1, 45500000000.0),
    ]


# ── 4. 冪等: 同一入力の再実行で書込 0 ──────────────────────────────────────
def test_idempotent_rerun(conn, run, store):
    _upsert_jquants_doc(conn, run, store, _payload_full())
    r1 = fundamentals.run_promotion(conn, run, store, limit=10)
    r2 = fundamentals.run_promotion(conn, run, store, limit=10)
    assert r1.processed == 1
    # 2 回目は fundamentals_version が一致するため find_unprocessed が拾わない。
    assert r2.processed == 0
    assert r2.written == 0


# ── 5. バックフィル: 複数文書の一括処理+冪等マーカ更新 ─────────────────────
def test_backfill_processes_multiple_documents(conn, run, store):
    # 別銘柄 2 件を用意(payload 差分は Code と DiscNo)。
    p1 = _payload_full()
    p2 = _payload_full()
    p2["Code"] = "67580"
    p2["DiscNo"] = "20260514002"
    _upsert_jquants_doc(conn, run, store, p1)
    doc2 = _upsert_jquants_doc(conn, run, store, p2)
    # payload 2 の meta.symbol は _upsert_jquants_doc が "7203.T" 固定なので上書きする
    # (テストの都合。実 ingest では jquants._normalize_symbol("67580") = "6758.T")。
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE docs.documents SET meta = meta || %s::jsonb WHERE doc_id = %s",
            (Jsonb({"symbol": "6758.T"}), doc2),
        )
    result = fundamentals.run_promotion(conn, run, store, limit=10)
    assert result.processed == 2
    # 2 銘柄 × 11 系列 = 22 点。
    assert result.written == 22
    # 双方に処理済みマーカーが刻まれている。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM docs.documents "
            "WHERE source_name=%s AND meta->>'fundamentals_version'=%s",
            (JQUANTS_SOURCE_NAME, fundamentals.FUNDAMENTALS_VERSION),
        )
        assert cur.fetchone()[0] == 2


# ── 6. リネージ: indicators→documents の辺が張られる ────────────────────────
def test_lineage_indicators_to_documents(conn, run, store):
    doc_id = _upsert_jquants_doc(conn, run, store, _payload_full())
    fundamentals.run_promotion(conn, run, store, limit=10)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='indicators' AND to_kind='documents' "
            "AND to_id = %s AND run_id = %s",
            (str(doc_id), run.run_id),
        )
        # 実績 6 + 現行予想 5 = 11 点(=indicator ごと 1 辺)。
        assert cur.fetchone()[0] == 11


# ── 追加: DocType が財務諸表本体でない開示は全項目 skip(no_basis)────────────
def test_non_statement_doctype_skipped(conn, run, store):
    """DividendForecastRevision 等の DocType は財務諸表ではないので全 skip。"""
    payload = _payload_full()
    payload["DocType"] = "DividendForecastRevision"
    _upsert_jquants_doc(conn, run, store, payload)
    result = fundamentals.run_promotion(conn, run, store, limit=10)
    assert result.processed == 1
    assert result.written == 0
    assert result.skip["no_basis"] == 1


# ── 追加: load_field_maps は config を bucket ごとに正しく読む ──────────────
def test_load_field_maps_reads_config():
    field_maps = fundamentals.load_field_maps()
    buckets = {fm.bucket for fm in field_maps}
    assert buckets == {"actuals", "forecasts_current", "forecasts_next"}
    # 実績 6 + 現行予想 5 + 翌期予想 5 = 16 行を想定(config を変えたら要見直し)。
    assert len(field_maps) == 16
    # 「実績と予想は別 normalized 名」を確認(look-ahead 混入対策 — T-029 §1-2)。
    actuals = {fm.normalized for fm in field_maps if fm.bucket == "actuals"}
    forecasts = {fm.normalized for fm in field_maps if fm.bucket != "actuals"}
    assert actuals.isdisjoint(forecasts)
