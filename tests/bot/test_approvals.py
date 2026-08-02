"""承認記録・オーナー検証のテスト(受け入れ基準: 承認ボタン→decisions 記録 / 非オーナー拒否)。

discord API はモックせず(そもそも import しない)、純ロジック record_decision を
ライブ DB で検証する。各テストは rollback で隔離する。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.bot import approvals
from ryza.bot.approvals import NotOwnerError

OWNERS = ["1001", "1002"]


def _ref(conn) -> str:
    """テスト間で衝突しない proposal_ref を採番(UNIQUE 制約回避)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT nextval(pg_get_serial_sequence('governance.decisions', 'id'))")
        return f"prop-{cur.fetchone()[0]}"


def _fetch(conn, proposal_ref: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, decision, decided_by FROM governance.decisions WHERE proposal_ref = %s",
            (proposal_ref,),
        )
        return cur.fetchone()


def test_is_owner_str_and_int():
    # snowflake は文字列比較。int で渡しても str 化して一致する。
    assert approvals.is_owner("1001", OWNERS)
    assert approvals.is_owner(1001, OWNERS)  # type: ignore[arg-type]
    assert not approvals.is_owner("9999", OWNERS)


def test_record_decision_approve(conn):
    ref = _ref(conn)
    d = approvals.record_decision(conn, ref, "approve", "1001", OWNERS, kind="pr")
    assert d.decision == "approve"
    assert _fetch(conn, ref) == ("pr", "approve", "1001")


def test_record_decision_reject_and_question(conn):
    ref_r = _ref(conn)
    approvals.record_decision(conn, ref_r, "reject", "1002", OWNERS)
    assert _fetch(conn, ref_r)[1] == "reject"
    ref_q = _ref(conn)
    approvals.record_decision(conn, ref_q, "question", "1002", OWNERS)
    assert _fetch(conn, ref_q)[1] == "question"


def test_non_owner_rejected_and_not_recorded(conn):
    ref = _ref(conn)
    with pytest.raises(NotOwnerError):
        approvals.record_decision(conn, ref, "approve", "9999", OWNERS)
    # 非オーナーの操作は記録されない。
    assert _fetch(conn, ref) is None


def test_unknown_decision_or_kind_rejected(conn):
    ref = _ref(conn)
    with pytest.raises(ValueError):
        approvals.record_decision(conn, ref, "maybe", "1001", OWNERS)
    with pytest.raises(ValueError):
        approvals.record_decision(conn, ref, "approve", "1001", OWNERS, kind="bogus")


def test_double_press_blocked_by_unique(conn):
    """同一 proposal_ref の二度押しは UNIQUE 制約で弾かれる(1提案=1決定)。"""
    ref = _ref(conn)
    approvals.record_decision(conn, ref, "approve", "1001", OWNERS)
    with pytest.raises(psycopg.errors.UniqueViolation):
        approvals.record_decision(conn, ref, "reject", "1002", OWNERS)
    conn.rollback()


def test_build_approval_embed_shape():
    embed = approvals.build_approval_embed("prop-x", "PR #12", "本文", kind="pr")
    assert embed["color"] == approvals.COLOR_APPROVAL
    assert approvals.DISCLAIMER in embed["footer"]["text"]
    assert "prop-x" in embed["footer"]["text"]
    with pytest.raises(ValueError):
        approvals.build_approval_embed("prop-x", "t", "b", kind="bogus")
