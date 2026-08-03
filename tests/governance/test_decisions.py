"""みなし承認・事後否認の writer(src/ryza/governance/decisions.py)のテスト。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行し、
commit せず rollback で隔離する。接続不可なら skip。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.bot.approvals import record_decision
from ryza.db.conn import connect
from ryza.governance.decisions import (
    RESERVED_KINDS,
    DuplicateDecisionError,
    ReservedMatterError,
    current_decision,
    record_deemed_approval,
    record_veto,
)
from ryza.provenance import start_run

OWNER = "424242"
NOTICE = "discord://承認/1234567890"


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


# ── みなし承認の記録(定款第3条3号・0019 C-3)──────────────────────────────
def test_record_deemed_approval_writes_deemed_row(conn):
    """decision='deemed'・decided_by='system:deemed'・通知参照つきで記録される。"""
    got = record_deemed_approval(
        conn, "https://github.com/x/pull/101", "pr", NOTICE
    )
    assert got.decided_by == "system:deemed"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision, decided_by, channel_msg_id, kind "
            "FROM governance.decisions WHERE id = %s",
            (got.id,),
        )
        assert cur.fetchone() == ("deemed", "system:deemed", NOTICE, "pr")
    conn.rollback()


def test_deemed_source_is_reflected_in_actor(conn):
    """発効源は decided_by='system:<source>' に載る(0019 の system:% CHECK に適合)。"""
    got = record_deemed_approval(
        conn, "ips-rev-2026-09", "other", NOTICE, source="ips_monthly_review"
    )
    assert got.decided_by == "system:ips_monthly_review"
    conn.rollback()


def test_deemed_row_appears_in_current_decisions(conn):
    """現決定 view から読める(A-13 の突合・deemed_ratio 集計の読み口)。"""
    record_deemed_approval(conn, "mandate-rev-2026-09", "other", NOTICE)
    row = current_decision(conn, "mandate-rev-2026-09")
    assert row["effective_decision"] == "deemed"
    assert row["is_vetoed"] is False
    conn.rollback()


def test_current_decision_returns_none_for_unknown_ref(conn):
    assert current_decision(conn, "no-such-proposal-ref") is None
    conn.rollback()


# ── 3専決事項はみなし承認できない(定款第3条・0019 C-2)────────────────────
@pytest.mark.parametrize("kind", sorted(RESERVED_KINDS))
def test_reserved_kinds_rejected_before_insert(conn, kind):
    """スキーマに届く前に明確なエラーで弾く。

    CheckViolation はどの制約かが呼び出し側に伝わりにくく、かつトランザクションを
    中断させるため、通知と同一トランザクションで記録する設計では通知の書込まで
    巻き添えになる。スキーマ側の CHECK が一次統制であることは変わらない
    (test_reserved_matter_cannot_be_deemed が DB 側を直接検証している)。
    """
    with pytest.raises(ReservedMatterError, match="専決事項"):
        record_deemed_approval(conn, f"reserved-{kind}", kind, NOTICE)
    # トランザクションが中断していない = 続けて別の記録ができる。
    assert record_deemed_approval(conn, f"ok-after-{kind}", "pr", NOTICE).id > 0
    conn.rollback()


def test_unknown_kind_rejected(conn):
    with pytest.raises(ValueError, match="未知の提案種別"):
        record_deemed_approval(conn, "unknown-kind", "wishlist", NOTICE)
    conn.rollback()


@pytest.mark.parametrize("missing", ["proposal_ref", "notice_ref"])
def test_blank_required_fields_rejected(conn, missing):
    """通知参照は必須 — 定款第3条は通知を発効要件とする(通知なき発効は A-13 違反)。"""
    args = {"proposal_ref": "blank-test", "notice_ref": NOTICE}
    args[missing] = "  "
    with pytest.raises(ValueError, match=missing):
        record_deemed_approval(conn, args["proposal_ref"], "pr", args["notice_ref"])
    conn.rollback()


# ── 1提案=1決定(0007 の UNIQUE)──────────────────────────────────────────
def test_duplicate_proposal_ref_raises_clear_error(conn):
    """二重通知・リトライでも承認記録は増えない。UniqueViolation を包んで返す。"""
    record_deemed_approval(conn, "dup-ref", "pr", NOTICE)
    with pytest.raises(DuplicateDecisionError, match="1提案=1決定"):
        record_deemed_approval(conn, "dup-ref", "pr", NOTICE)
    # 事前検査で弾くためトランザクションは生きている(通知の書込を巻き添えにしない)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decisions WHERE proposal_ref = 'dup-ref'"
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_duplicate_against_explicit_decision_raises(conn):
    """明示承認済みの提案をみなし承認で上書きできない(承認経路の格上げ防止)。"""
    record_decision(conn, "explicit-then-deemed", "approve", OWNER, [OWNER], kind="pr")
    with pytest.raises(DuplicateDecisionError, match="approve"):
        record_deemed_approval(conn, "explicit-then-deemed", "pr", NOTICE)
    conn.rollback()


# ── 事後否認(定款第3条2号・0021)──────────────────────────────────────────
def test_record_veto_marks_decision_vetoed(conn):
    deemed = record_deemed_approval(conn, "veto-target", "other", NOTICE)
    veto = record_veto(
        conn, deemed.id, "リスク上限を緩める方向のため否認", vetoed_by=OWNER
    )
    assert veto.veto_id > 0
    row = current_decision(conn, "veto-target")
    assert row["effective_decision"] == "vetoed"
    assert row["recorded_decision"] == "deemed"  # 何が発効していたかは残る
    assert row["vetoed_by"] == OWNER
    conn.rollback()


def test_record_veto_accepts_revert_and_derived_effects(conn):
    """取消コミットと取消不能な派生効果の参照を記録できる(第3条の報告義務)。"""
    deemed = record_deemed_approval(conn, "veto-with-revert", "other", NOTICE)
    run_id = start_run("test.governance", conn=conn).run_id
    record_veto(
        conn, deemed.id, "否認", vetoed_by=OWNER,
        revert_commit="0123abc", derived_effects_ref="discord://運営/999", run_id=run_id,
    )
    row = current_decision(conn, "veto-with-revert")
    assert row["revert_commit"] == "0123abc"
    assert row["derived_effects_ref"] == "discord://運営/999"
    conn.rollback()


def test_veto_then_revert_completion_is_appended(conn):
    """取消完了は追記で表現し、現決定には最新行が映る(追記オンリーのため UPDATE 不可)。"""
    deemed = record_deemed_approval(conn, "veto-two-step", "other", NOTICE)
    record_veto(conn, deemed.id, "否認(取消未完了)", vetoed_by=OWNER)
    assert current_decision(conn, "veto-two-step")["revert_commit"] is None
    record_veto(
        conn, deemed.id, "否認に伴う取消完了", vetoed_by=OWNER, revert_commit="feedface"
    )
    assert current_decision(conn, "veto-two-step")["revert_commit"] == "feedface"
    conn.rollback()


def test_explicit_approval_can_be_vetoed(conn):
    """明示承認も否認できる(0021 の一般化。定款は明示承認の撤回を禁じていない)。"""
    got = record_decision(
        conn, "explicit-veto", "approve", OWNER, [OWNER], kind="strategy_promotion"
    )
    record_veto(conn, got.id, "前提データの誤りが判明したため", vetoed_by=OWNER)
    assert current_decision(conn, "explicit-veto")["effective_decision"] == "vetoed"
    conn.rollback()


def test_veto_of_unknown_decision_raises_clear_error(conn):
    """FK 違反を待たず明確なエラーにする(FK 違反はトランザクションを中断させる)。"""
    with pytest.raises(ValueError, match="存在しない"):
        record_veto(conn, -1, "対象なし否認", vetoed_by=OWNER)
    assert record_deemed_approval(conn, "after-bad-veto", "pr", NOTICE).id > 0
    conn.rollback()


@pytest.mark.parametrize("field", ["reason", "vetoed_by"])
def test_veto_requires_reason_and_actor(conn, field):
    deemed = record_deemed_approval(conn, f"veto-blank-{field}", "other", NOTICE)
    kwargs = {"reason": "理由", "vetoed_by": OWNER}
    kwargs[field] = "   "
    with pytest.raises(ValueError, match=field):
        record_veto(conn, deemed.id, kwargs["reason"], vetoed_by=kwargs["vetoed_by"])
    conn.rollback()


def test_veto_is_append_only(conn):
    """記録した否認は書き換えられない(0021 の追記オンリートリガ)。"""
    deemed = record_deemed_approval(conn, "veto-immutable", "other", NOTICE)
    veto = record_veto(conn, deemed.id, "否認", vetoed_by=OWNER)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.decision_vetoes SET reason = '改竄' WHERE veto_id = %s",
                (veto.veto_id,),
            )
    conn.rollback()
