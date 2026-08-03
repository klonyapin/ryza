"""fm.theses のテスト(T-017): 証憑必須・反証条件必須・point-in-time・追記オンリー。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ryza.fm.theses import (
    EvidenceError,
    ThesisError,
    open_theses_by_instrument,
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
