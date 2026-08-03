"""fm.theses のテスト(T-017): 証憑必須・反証条件必須・point-in-time・追記オンリー。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ryza.fm.theses import (
    EvidenceError,
    ThesisError,
    is_quarantined,
    open_theses_by_instrument,
    quarantine_stats,
    quarantine_thesis,
    quarantined_open_instruments,
    recent_theses,
    record_thesis,
    validate_evidence_refs,
)

BOOK = "DEMO_FUND"


def _record(conn, run, **overrides):
    kwargs = dict(
        fm="ben",
        book_id=BOOK,
        instrument_id=1,
        direction="buy",
        thesis_md="安全域がある。",
        evidence_refs=[],
        invalidation_md="営業利益率が2四半期連続で 8% を下回ったら降りる。",
        producer="test.fm",
        as_of=datetime.now(UTC),
        run_id=run.run_id,
        model="test-model",
    )
    kwargs.update(overrides)
    return record_thesis(conn, **kwargs)


# ── 証憑(evidence_refs)────────────────────────────────────────────────────────
def test_evidence_required(conn, run, insert_document):
    """証憑ゼロの提案は記録できない(不変原則3)。"""
    with pytest.raises(EvidenceError):
        _record(conn, run, evidence_refs=[])


def test_evidence_must_exist(conn, run):
    """存在しない証憑を参照する提案は拒否する。"""
    with pytest.raises(EvidenceError, match="存在しない"):
        _record(conn, run, evidence_refs=[{"kind": "document", "doc_id": 999_999_999}])


def test_evidence_point_in_time_rejected(conn, run, insert_document):
    """as_of より新しい証憑(未来情報)は拒否する — 不変原則4。"""
    as_of = datetime.now(UTC) - timedelta(days=7)
    doc_id = insert_document(as_of=datetime.now(UTC))  # 判断時点より新しい
    with pytest.raises(EvidenceError, match="未来情報"):
        _record(
            conn, run, as_of=as_of,
            evidence_refs=[{"kind": "document", "doc_id": doc_id}],
        )


def test_evidence_within_as_of_accepted(conn, run, insert_document):
    doc_id = insert_document(as_of=datetime.now(UTC) - timedelta(days=3))
    thesis_id = _record(
        conn, run, evidence_refs=[{"kind": "document", "doc_id": doc_id}]
    )
    assert thesis_id > 0


def test_evidence_bar_ref(conn, run, instrument, insert_bars):
    iid = instrument()
    stamps = insert_bars(iid, [100, 101, 102])
    refs = [
        {"kind": "bar", "instrument_id": iid, "timeframe": "1d", "ts": s.isoformat()}
        for s in stamps
    ]
    assert len(validate_evidence_refs(conn, refs, as_of=datetime.now(UTC))) == 3


# ── 対象時点(ts)の検証(独立役員審査 T-017 C-6)───────────────────────────────
def test_evidence_bar_with_future_ts_is_rejected(conn, run, instrument, insert_bars):
    """as_of は過去でも ts が判断時点より後のバーは拒否する(バックフィル・誤登録対策)。

    「知り得た時点(as_of)」だけを見る検査は、改定やバックフィルで作られる
    『as_of は過去だが ts は未来』の行を通してしまう。
    """
    iid = instrument()
    as_of = datetime.now(UTC)
    future_day = (as_of + timedelta(days=3)).astimezone(UTC).date()
    # as_of は判断時点より前(= 知り得た時点としては合法)だが、ts は未来。
    stamps = insert_bars(
        iid, [100], last_day=future_day, as_of=as_of - timedelta(days=1)
    )
    refs = [
        {"kind": "bar", "instrument_id": iid, "timeframe": "1d", "ts": stamps[0].isoformat()}
    ]
    with pytest.raises(EvidenceError, match="対象時点 ts") as exc:
        validate_evidence_refs(conn, refs, as_of=as_of)
    assert "不変原則4" in exc.value.problems[0]


def test_evidence_indicator_with_future_ts_is_rejected(conn, run):
    as_of = datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.indicators (series_code, ts, value, revision, as_of, run_id)
            VALUES ('TEST_TS_CHECK', %s, 1.0, 0, %s, %s)
            """,
            (as_of + timedelta(days=2), as_of - timedelta(days=1), run.run_id),
        )
    refs = [
        {
            "kind": "indicator",
            "series_code": "TEST_TS_CHECK",
            "ts": (as_of + timedelta(days=2)).isoformat(),
        }
    ]
    with pytest.raises(EvidenceError, match="対象時点 ts"):
        validate_evidence_refs(conn, refs, as_of=as_of)


def test_evidence_past_ts_still_accepted(conn, run, instrument, insert_bars):
    """ts 検査を足しても、過去の足の参照は従来どおり通る(回帰)。"""
    iid = instrument()
    stamps = insert_bars(iid, [100, 101])
    refs = [
        {"kind": "bar", "instrument_id": iid, "timeframe": "1d", "ts": s.isoformat()}
        for s in stamps
    ]
    assert len(validate_evidence_refs(conn, refs, as_of=datetime.now(UTC))) == 2


def test_evidence_unknown_kind(conn, run):
    with pytest.raises(EvidenceError, match="未知の証憑 kind"):
        _record(conn, run, evidence_refs=[{"kind": "gut_feeling", "id": 1}])


def test_evidence_problems_listed_together(conn, run):
    """違反は1件目で止めず全件返す(LLM 出力を1回で直せるように)。"""
    with pytest.raises(EvidenceError) as exc:
        _record(
            conn, run,
            evidence_refs=[
                {"kind": "document", "doc_id": 999_999_998},
                {"kind": "document", "doc_id": 999_999_999},
            ],
        )
    assert len(exc.value.problems) == 2


# ── 反証条件・direction・出所 ─────────────────────────────────────────────────
def test_invalidation_required(conn, run, insert_document):
    doc_id = insert_document()
    with pytest.raises(ThesisError, match="invalidation_md"):
        _record(
            conn, run, invalidation_md="   ",
            evidence_refs=[{"kind": "document", "doc_id": doc_id}],
        )


def test_thesis_body_required(conn, run, insert_document):
    doc_id = insert_document()
    with pytest.raises(ThesisError, match="thesis_md"):
        _record(
            conn, run, thesis_md="",
            evidence_refs=[{"kind": "document", "doc_id": doc_id}],
        )


def test_short_direction_rejected_for_first_wave(conn, run, insert_document):
    """第一陣は long-only — short は allow_short を明示しない限り記録できない。"""
    doc_id = insert_document()
    with pytest.raises(ThesisError, match="long-only"):
        _record(
            conn, run, direction="short",
            evidence_refs=[{"kind": "document", "doc_id": doc_id}],
        )


def test_origin_must_be_exclusive(conn, run, insert_document):
    """rule_id(決定論)と model(LLM)の両方指定・両方欠落は拒否する。"""
    doc_id = insert_document()
    refs = [{"kind": "document", "doc_id": doc_id}]
    with pytest.raises(ThesisError, match="どちらか一方"):
        _record(conn, run, evidence_refs=refs, rule_id="r1", model="m1")
    with pytest.raises(ThesisError, match="どちらか一方"):
        _record(conn, run, evidence_refs=refs, model=None)


# ── 追記オンリー ──────────────────────────────────────────────────────────────
def test_theses_append_only(conn, run, insert_document):
    doc_id = insert_document()
    thesis_id = _record(conn, run, evidence_refs=[{"kind": "document", "doc_id": doc_id}])
    with conn.cursor() as cur, pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE trading.fm_theses SET thesis_md = 'x' WHERE thesis_id = %s",
            (thesis_id,),
        )


# ── TRUNCATE の封鎖(独立役員審査 2026-08-03 C-2)────────────────────────────
@pytest.mark.parametrize(
    "table",
    [
        "trading.fm_theses",
        "trading.orders",
        "trading.executions",
        "trading.position_applies",
        "compliance.gate_log",
    ],
)
def test_truncate_is_blocked(conn, table):
    """TRUNCATE は行トリガを迂回するため文トリガで封鎖する(0015 と同基準)。"""
    with pytest.raises(psycopg.errors.RaiseException, match="TRUNCATE は禁止"):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f"TRUNCATE {table} CASCADE")  # noqa: S608 - 固定リスト


# ── 読出し(次回プロンプトへの注入)────────────────────────────────────────────
def test_recent_theses_orders_newest_first(conn, run, insert_document):
    doc_id = insert_document()
    refs = [{"kind": "document", "doc_id": doc_id}]
    first = _record(conn, run, evidence_refs=refs)
    second = _record(conn, run, evidence_refs=refs, instrument_id=2)
    rows = recent_theses(conn, "ben", limit=10)
    assert [r.thesis_id for r in rows[:2]] == [second, first]
    assert rows[0].order_status is None  # 注文になっていない提案は None


def test_open_theses_by_instrument(conn, run, insert_document):
    doc_id = insert_document()
    refs = [{"kind": "document", "doc_id": doc_id}]
    _record(conn, run, evidence_refs=refs, instrument_id=7)
    latest = _record(conn, run, evidence_refs=refs, instrument_id=7)
    found = open_theses_by_instrument(conn, "ben", [7])
    assert found[7].thesis_id == latest


# ── 検疫(独立役員審査 T-017 C-3)──────────────────────────────────────────────
def test_quarantined_thesis_is_excluded_from_reinjection(conn, run, insert_document):
    """検疫した提案は再注入経路(recent_theses / open_theses_by_instrument)に出ない。"""
    doc_id = insert_document()
    refs = [{"kind": "document", "doc_id": doc_id}]
    clean = _record(conn, run, evidence_refs=refs, instrument_id=11)
    tainted = _record(conn, run, evidence_refs=refs, instrument_id=12)
    assert is_quarantined(conn, tainted) is False

    quarantine_thesis(
        conn, tainted, reason="外部文書経由の指示文が混入", quarantined_by="dev-lead",
        run_id=run.run_id,
    )
    assert is_quarantined(conn, tainted) is True

    ids = [r.thesis_id for r in recent_theses(conn, "ben", limit=50)]
    assert tainted not in ids and clean in ids
    assert open_theses_by_instrument(conn, "ben", [12]) == {}
    # 汚染されていない提案の建玉根拠は従来どおり引ける。
    assert open_theses_by_instrument(conn, "ben", [11])[11].thesis_id == clean


def test_quarantine_is_idempotent_and_validated(conn, run, insert_document):
    doc_id = insert_document()
    thesis_id = _record(conn, run, evidence_refs=[{"kind": "document", "doc_id": doc_id}])
    first = quarantine_thesis(conn, thesis_id, reason="汚染", quarantined_by="dev-lead")
    again = quarantine_thesis(conn, thesis_id, reason="汚染(再)", quarantined_by="audit")
    assert first == again  # 二重登録しない(証跡は1行)

    with pytest.raises(ThesisError, match="理由"):
        quarantine_thesis(conn, thesis_id, reason="  ", quarantined_by="dev-lead")
    with pytest.raises(ThesisError, match="実施主体"):
        quarantine_thesis(conn, thesis_id, reason="汚染", quarantined_by=" ")
    with pytest.raises(ThesisError, match="存在しない"):
        quarantine_thesis(
            conn, 999_999_999, reason="汚染", quarantined_by="dev-lead"
        )


def test_quarantine_table_is_append_only(conn, run, insert_document):
    """検疫表自身も追記オンリー(解除経路を作らない — 0023 の判断2)。"""
    doc_id = insert_document()
    thesis_id = _record(conn, run, evidence_refs=[{"kind": "document", "doc_id": doc_id}])
    quarantine_thesis(conn, thesis_id, reason="汚染", quarantined_by="dev-lead")
    with conn.cursor() as cur, pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "DELETE FROM trading.fm_theses_quarantine WHERE thesis_id = %s", (thesis_id,)
        )


def test_quarantine_stats_counts_today_and_total(conn, run, insert_document):
    """検疫の発生件数を当日増分・累計・全提案数で返す(mass-quarantine 検知の入力)。"""
    doc_id = insert_document()
    refs = [{"kind": "document", "doc_id": doc_id}]
    ids = [_record(conn, run, evidence_refs=refs, instrument_id=i) for i in (21, 22)]
    before = quarantine_stats(conn, as_of=datetime.now(UTC))
    assert before["theses_total"] >= 2

    for thesis_id in ids:
        quarantine_thesis(conn, thesis_id, reason="注入", quarantined_by="dev-lead")
    after = quarantine_stats(conn, as_of=datetime.now(UTC))
    assert after["today"] == before["today"] + 2
    assert after["total"] == before["total"] + 2


def test_quarantined_open_instruments_distinguishes_missing_from_quarantined(
    conn, run, insert_document
):
    """「thesis が無い」と「検疫済み」を区別する(C-11 の判定材料)。"""
    doc_id = insert_document()
    refs = [{"kind": "document", "doc_id": doc_id}]
    thesis_id = _record(conn, run, evidence_refs=refs, instrument_id=31)
    quarantine_thesis(conn, thesis_id, reason="注入", quarantined_by="dev-lead")
    # 32 番は thesis を作っていない(= 根拠が最初から無い保有)。
    assert quarantined_open_instruments(conn, "ben", [31, 32]) == {31}


def test_quarantine_truncate_is_blocked(conn):
    with pytest.raises(psycopg.errors.RaiseException, match="TRUNCATE は禁止"):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("TRUNCATE trading.fm_theses_quarantine CASCADE")
