"""役員室(会議)のロジック層(src/ryza/governance/boardroom.py)のテスト。

LLM は ``FixtureProvider`` でモックし、実 API・実ネットワークは呼ばない。
DB 依存(議事録保存・決議マーク・stances 追記)はテスト専用 DB
(tests/conftest.py の ``migrated_db``)に対し、rollback 隔離で検証する。
Streamlit UI(dashboard/app.py)自体はテスト対象外。
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ryza.db.conn import connect
from ryza.governance.boardroom import (
    MEETING_ORDER,
    PASS_TEXT,
    ChatTurn,
    attendees_of,
    conduct_meeting,
    digest_stances,
    fetch_resolutions,
    is_pass,
    mark_resolution,
    record_chat_stances,
    save_office_chat_minute,
    speak,
    speaking_roles,
    transcript_markdown,
)
from ryza.governance.personas import recent_stances
from ryza.provenance import start_run
from ryza.research.llm import FixtureProvider, StructuredLLM
from ryza.research.schemas import SchemaError

HELD_AT = datetime(2026, 8, 3, 21, 0, tzinfo=UTC)

TURNS = [
    ChatTurn("representative", "実弾移行の時期を早めたい。"),
    ChatTurn("cio", "段階移行を提案する。"),
    ChatTurn("independent_officer", "反対する。予防統制が未稼働(定款第5条)。"),
    ChatTurn("audit", PASS_TEXT),
    ChatTurn("representative", "では前提条件は何か。"),
]


def _onboarding(role: str) -> str:
    return f"ONBOARDING[{role}]"


# ── 会話の Markdown 化・出席者(純関数)──────────────────────────────────────
def test_transcript_markdown_full_and_deterministic():
    """全発言が話者ラベル付きで残り(05 §4: 全文)、同一入力 → 同一出力。"""
    md = transcript_markdown(TURNS, held_at=HELD_AT)
    assert md.startswith("# 役員室会議")
    assert "- 出席: 代表、CIO、独立役員、監査" in md
    assert "**代表**: 実弾移行の時期を早めたい。" in md
    assert "**独立役員**: 反対する。予防統制が未稼働(定款第5条)。" in md
    # パスも全文に残す(発言機会があった証跡 — 05 §6-5 の検証可能性)。
    assert f"**監査**: {PASS_TEXT}" in md
    assert md == transcript_markdown(TURNS, held_at=HELD_AT)


def test_attendees_and_speaking_roles():
    """出席者はパスを含む固定順、stances 要約対象はパスを除いた役職。"""
    assert attendees_of(TURNS) == ["representative", "cio", "independent_officer", "audit"]
    assert speaking_roles(TURNS) == ["cio", "independent_officer"]
    assert is_pass(f"  {PASS_TEXT} \n") and not is_pass("懸念がある")


# ── 会議の逐次応答(FixtureProvider)──────────────────────────────────────────
def test_conduct_meeting_speaks_in_fixed_order_with_prior_statements():
    """固定順で全役職が発言し、後の発言者のプロンプトに先行発言が入る。"""
    provider = FixtureProvider(
        [
            {"reply": "段階移行を提案する。"},
            {"reply": "反対する。予防統制が未稼働。"},
            {"reply": PASS_TEXT},
        ]
    )
    llm = StructuredLLM(provider, dept_tag="governance")
    seen: list[ChatTurn] = []
    turns = [ChatTurn("representative", "実弾移行の時期を早めたい。")]
    new_turns = conduct_meeting(
        llm, onboarding_for_role=_onboarding, turns=turns,
        model="test-model", model_tier="fable", on_reply=seen.append,
    )
    # 発言順は BOARDROOM_ROLES の定義順(CIO → 独立役員 → 監査)。
    assert [t.speaker for t in new_turns] == list(MEETING_ORDER)
    assert new_turns == seen  # on_reply は1発言ごとに呼ばれる
    assert turns == [ChatTurn("representative", "実弾移行の時期を早めたい。")]  # 非破壊

    # 着任プロンプトは役職ごと(永続記憶の分離 — 05 §6-2)。
    assert [c["system"].split("\n")[0] for c in provider.calls] == [
        f"ONBOARDING[{r}]" for r in MEETING_ORDER
    ]
    assert all("会議" in c["system"] for c in provider.calls)
    assert all("追従の禁止" in c["system"] for c in provider.calls)
    assert all("何も自動執行されない" in c["system"] for c in provider.calls)
    # 独立役員だけ応答義務(最低1懸念)が上乗せされる(05 §3)。
    assert "応答義務" in provider.calls[1]["system"]
    assert "応答義務" not in provider.calls[0]["system"]

    # 1番目(CIO)は代表の発言のみ、2番目は CIO の発言も、3番目は両方を見ている。
    assert "段階移行を提案する。" not in provider.calls[0]["user"]
    assert "CIO: 段階移行を提案する。" in provider.calls[1]["user"]
    assert "CIO: 段階移行を提案する。" in provider.calls[2]["user"]
    assert "独立役員: 反対する。予防統制が未稼働。" in provider.calls[2]["user"]
    assert all("実弾移行の時期を早めたい。" in c["user"] for c in provider.calls)
    assert provider.calls[0]["model"] == "test-model"


def test_conduct_meeting_allows_pass_and_records_it():
    """『(発言なし)』はそのまま発言として残る(隠さない)。"""
    llm = StructuredLLM(FixtureProvider([{"reply": f"\n{PASS_TEXT}\n"}]), dept_tag="governance")
    new_turns = conduct_meeting(
        llm, onboarding_for_role=_onboarding,
        turns=[ChatTurn("representative", "雑談だが今日は良い天気だ。")],
        model="m", model_tier="fable",
    )
    assert [t.text for t in new_turns] == [PASS_TEXT] * len(MEETING_ORDER)
    assert all(is_pass(t.text) for t in new_turns)
    assert speaking_roles(new_turns) == []


def test_conduct_meeting_requires_trailing_representative_turn():
    llm = StructuredLLM(FixtureProvider([{"reply": "x"}]), dept_tag="governance")
    with pytest.raises(ValueError, match="代表"):
        conduct_meeting(
            llm, onboarding_for_role=_onboarding, turns=TURNS[:2],  # 末尾が役職の発言
            model="m", model_tier="fable",
        )


def test_speak_rejects_empty_transcript():
    llm = StructuredLLM(FixtureProvider([{"reply": "x"}]), dept_tag="governance")
    with pytest.raises(ValueError, match="空"):
        speak(
            llm, role="cio", onboarding_prompt="P", turns=[], model="m", model_tier="fable"
        )


def test_speak_schema_violation_raises():
    """reply フィールド欠落はスキーマ不適合 → リトライ後 SchemaError。"""
    llm = StructuredLLM(FixtureProvider([{"answer": "形式違反"}]), dept_tag="governance")
    with pytest.raises(SchemaError):
        speak(
            llm, role="cio", onboarding_prompt="P", turns=TURNS,
            model="m", model_tier="fable",
        )


# ── stances 要約(FixtureProvider)────────────────────────────────────────────
def test_digest_stances_returns_kind_and_summary():
    provider = FixtureProvider(
        [
            {
                "stances": [
                    {"kind": "dissent", "summary": "実弾移行の前倒しに反対(統制未稼働)"},
                    {"kind": "concern", "summary": "予防統制 CI の未整備を懸念"},
                ]
            }
        ]
    )
    llm = StructuredLLM(provider, dept_tag="governance")
    got = digest_stances(
        llm, role="independent_officer", transcript_md="(全文)",
        model="m", model_tier="mid",
    )
    assert got == [
        {"kind": "dissent", "summary": "実弾移行の前倒しに反対(統制未稼働)"},
        {"kind": "concern", "summary": "予防統制 CI の未整備を懸念"},
    ]
    # 対象役職と会話全文が user に入る。
    assert "independent_officer" in provider.calls[0]["user"]
    assert "(全文)" in provider.calls[0]["user"]


def test_digest_stances_rejects_invalid_kind():
    """retraction 等 enum 外の kind はスキーマで弾く(撤回は明示操作のみ)。"""
    provider = FixtureProvider([{"stances": [{"kind": "retraction", "summary": "x"}]}])
    llm = StructuredLLM(provider, dept_tag="governance")
    with pytest.raises(SchemaError):
        digest_stances(
            llm, role="cio", transcript_md="t", model="m", model_tier="mid"
        )


# ── DB 層(テスト専用 DB・rollback 隔離)──────────────────────────────────────
@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run_id(conn) -> int:
    """実 Run(meta.runs 行)。minutes/stances の run_id FK が要求する(不変原則3)。"""
    return start_run("test.boardroom", conn=conn).run_id


def test_save_office_chat_minute_roundtrip(conn, run_id):
    saved = save_office_chat_minute(
        conn, turns=TURNS, run_id=run_id, held_at=HELD_AT
    )
    assert saved.minute_id > 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT meeting, attendees, body_md, run_id FROM governance.minutes"
            " WHERE minute_id = %s",
            (saved.minute_id,),
        )
        meeting, attendees, body_md, rid = cur.fetchone()
    assert meeting == "office_chat"
    # 出席者は発言から導出(パスした監査も出席者に含む)。
    assert attendees == ["representative", "cio", "independent_officer", "audit"]
    assert body_md == saved.body_md
    assert "**代表**: 実弾移行の時期を早めたい。" in body_md
    assert rid == run_id
    conn.rollback()


def test_save_empty_conversation_rejected(conn, run_id):
    with pytest.raises(ValueError, match="空の会話"):
        save_office_chat_minute(conn, turns=[], run_id=run_id)
    conn.rollback()


def test_mark_resolution_sequences_and_representative(conn, run_id):
    """決議は代表名義で連番マークされ、fetch で seq 順に読める。"""
    saved = save_office_chat_minute(
        conn, turns=TURNS, run_id=run_id, held_at=HELD_AT
    )
    r1 = mark_resolution(
        conn, minute_id=saved.minute_id, title="IPS 改訂を付議",
        resolution_md="決議本文(反対意見含む)", proposal_ref="ips-rev-2026-09",
    )
    r2 = mark_resolution(
        conn, minute_id=saved.minute_id, title="追加検証の実施", resolution_md="本文",
    )
    assert r2 > r1
    got = fetch_resolutions(conn, saved.minute_id)
    assert [(g["seq"], g["title"]) for g in got] == [
        (1, "IPS 改訂を付議"), (2, "追加検証の実施"),
    ]
    assert got[0]["proposal_ref"] == "ips-rev-2026-09"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT resolved_by FROM governance.minute_resolutions"
            " WHERE minute_id = %s",
            (saved.minute_id,),
        )
        assert cur.fetchall() == [("representative",)]
    conn.rollback()


def test_record_chat_stances_links_minute(conn, run_id):
    """要約は stances へ追記され、出所議事録に紐づき、次回着任で読める。"""
    saved = save_office_chat_minute(
        conn, turns=TURNS, run_id=run_id, held_at=HELD_AT
    )
    ids = record_chat_stances(
        conn,
        role="independent_officer",
        stances=[
            {"kind": "dissent", "summary": "実弾移行の前倒しに反対"},
            {"kind": "concern", "summary": "統制未稼働の懸念"},
        ],
        minute_id=saved.minute_id,
        run_id=run_id,
    )
    assert len(ids) == 2
    got = recent_stances(conn, "independent_officer", limit=10)
    assert {s.stance_id for s in got} >= set(ids)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT minute_id FROM governance.stances WHERE stance_id = ANY(%s)",
            (ids,),
        )
        assert cur.fetchall() == [(saved.minute_id,)]
    conn.rollback()


def test_record_chat_stances_invalid_kind_rejected_by_db(conn, run_id):
    """LLM 出力の検証をすり抜けても DB の CHECK(0013)が最後の防衛線。"""
    saved = save_office_chat_minute(
        conn, turns=TURNS, run_id=run_id, held_at=HELD_AT
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        record_chat_stances(
            conn, role="cio", stances=[{"kind": "applause", "summary": "拍手"}],
            minute_id=saved.minute_id, run_id=run_id,
        )
    conn.rollback()
