"""みなし承認・事後否認の writer(src/ryza/governance/decisions.py)のテスト。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行し、
commit せず rollback で隔離する。接続不可なら skip。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.bot.approvals import NotOwnerError, record_decision
from ryza.db.conn import connect
from ryza.governance.decisions import (
    RESERVED_KINDS,
    DuplicateDecisionError,
    NotVetoableError,
    ProposalRefMismatchError,
    ReservedMatterError,
    current_decision,
    record_deemed_approval,
    record_revert_completion,
    record_veto,
    record_veto_withdrawal,
)
from ryza.provenance import start_run

OWNER = "424242"
OWNERS = (OWNER,)
NOTICE = "discord://承認/1234567890"


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _deemed(conn, ref: str, kind: str = "other"):
    return record_deemed_approval(conn, ref, kind, NOTICE)


def _veto(conn, decision, reason: str = "リスク上限を緩める方向のため否認", **kw):
    """既定のオーナー・proposal_ref で否認を1件記録する。"""
    return record_veto(
        conn, decision.id, reason,
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=decision.proposal_ref, **kw,
    )


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
    """現決定 view から読める(A-18 の突合・deemed_ratio 集計の読み口)。"""
    _deemed(conn, "mandate-rev-2026-09")
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
    """通知参照は必須 — 定款第3条は通知を発効要件とする(通知なき発効は A-18 違反)。"""
    args = {"proposal_ref": "blank-test", "notice_ref": NOTICE}
    args[missing] = "  "
    with pytest.raises(ValueError, match=missing):
        record_deemed_approval(conn, args["proposal_ref"], "pr", args["notice_ref"])
    conn.rollback()


# ── 1提案=1決定(0007 の UNIQUE)──────────────────────────────────────────
def test_duplicate_proposal_ref_raises_clear_error(conn):
    """二重通知・リトライでも承認記録は増えない。UniqueViolation を包んで返す。"""
    _deemed(conn, "dup-ref", "pr")
    with pytest.raises(DuplicateDecisionError, match="1提案=1決定"):
        _deemed(conn, "dup-ref", "pr")
    # 事前検査で弾くためトランザクションは生きている(通知の書込を巻き添えにしない)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decisions WHERE proposal_ref = 'dup-ref'"
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_duplicate_against_explicit_decision_raises(conn):
    """明示承認済みの提案をみなし承認で上書きできない(承認経路の格上げ防止)。"""
    record_decision(conn, "explicit-then-deemed", "approve", OWNER, OWNERS, kind="pr")
    with pytest.raises(DuplicateDecisionError, match="approve"):
        _deemed(conn, "explicit-then-deemed", "pr")
    conn.rollback()


# ── 事後否認(定款第3条2号・0021)──────────────────────────────────────────
def test_record_veto_marks_decision_vetoed(conn):
    deemed = _deemed(conn, "veto-target")
    veto = _veto(conn, deemed)
    assert veto.veto_id > 0
    assert veto.kind == "veto"
    row = current_decision(conn, "veto-target")
    assert row["effective_decision"] == "vetoed"
    assert row["recorded_decision"] == "deemed"  # 何が発効していたかは残る
    assert row["vetoed_by"] == OWNER
    conn.rollback()


def test_record_veto_accepts_revert_and_derived_effects(conn):
    """取消コミットと取消不能な派生効果の参照を記録できる(第3条の報告義務)。"""
    deemed = _deemed(conn, "veto-with-revert")
    run_id = start_run("test.governance", conn=conn).run_id
    _veto(
        conn, deemed, "否認",
        revert_commit="0123abc", derived_effects_ref="discord://運営/999", run_id=run_id,
    )
    row = current_decision(conn, "veto-with-revert")
    assert row["revert_commit"] == "0123abc"
    assert row["derived_effects_ref"] == "discord://運営/999"
    conn.rollback()


def test_revert_completion_is_appended(conn):
    """取消完了は追記で表現し、現決定に反映される(追記オンリーのため UPDATE 不可)。"""
    deemed = _deemed(conn, "veto-two-step")
    _veto(conn, deemed, "否認(取消未完了)")
    assert current_decision(conn, "veto-two-step")["revert_commit"] is None
    got = record_revert_completion(
        conn, deemed.id, "否認に伴う取消完了",
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, revert_commit="feedface",
    )
    assert got.kind == "revert_complete"
    row = current_decision(conn, "veto-two-step")
    assert row["revert_commit"] == "feedface"
    assert row["is_vetoed"] is True  # 取消完了は否認を解除しない
    conn.rollback()


def test_uninformative_append_does_not_erase_revert_commit(conn):
    """情報の無い追記が既記録を消さない(独立役員審査 0021 C-4)。"""
    deemed = _deemed(conn, "veto-column-wise-writer")
    _veto(conn, deemed, "否認")
    record_revert_completion(
        conn, deemed.id, "取消完了", vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, revert_commit="cafebabe",
    )
    record_revert_completion(
        conn, deemed.id, "派生効果の追加報告", vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref,
        derived_effects_ref="discord://運営/777",
    )
    row = current_decision(conn, "veto-column-wise-writer")
    assert row["revert_commit"] == "cafebabe"
    assert row["derived_effects_ref"] == "discord://運営/777"
    conn.rollback()


def test_veto_withdrawal_restores_previous_state(conn):
    """否認の撤回で現決定は否認前に戻る(誤った対象への否認からの復旧 — C-3)。"""
    deemed = _deemed(conn, "veto-withdraw-writer")
    _veto(conn, deemed, "誤った対象への否認")
    got = record_veto_withdrawal(
        conn, deemed.id, "対象取り違えのため撤回",
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref,
    )
    assert got.kind == "withdrawal"
    row = current_decision(conn, "veto-withdraw-writer")
    assert row["is_vetoed"] is False
    assert row["effective_decision"] == "deemed"
    assert row["veto_kind"] == "withdrawal"  # 履歴は残る
    conn.rollback()


def test_explicit_approval_can_be_vetoed(conn):
    """明示承認も否認できる(定款は明示承認の撤回を禁じていない)。"""
    got = record_decision(
        conn, "explicit-veto", "approve", OWNER, OWNERS, kind="strategy_promotion"
    )
    record_veto(
        conn, got.id, "前提データの誤りが判明したため",
        vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="explicit-veto",
    )
    assert current_decision(conn, "explicit-veto")["effective_decision"] == "vetoed"
    conn.rollback()


# ── 否認できない決定・非オーナー・対象取り違え(独立役員審査 C-2 / C-3)────────
@pytest.mark.parametrize("decision", ["reject", "question"])
def test_reject_and_question_are_not_vetoable(conn, decision):
    """却下・質問は否認できない — 否認できると阻止の根拠が fail-open で消える。"""
    got = record_decision(
        conn, f"nonvetoable-{decision}", decision, OWNER, OWNERS, kind="pr"
    )
    with pytest.raises(NotVetoableError, match="否認できない"):
        record_veto(
            conn, got.id, "却下を覆す",
            vetoed_by=OWNER, owner_ids=OWNERS,
            expected_proposal_ref=f"nonvetoable-{decision}",
        )
    # 事前検査で弾くためトランザクションは生きている。
    assert _deemed(conn, f"after-nonvetoable-{decision}").id > 0
    conn.rollback()


def test_reject_veto_blocked_by_schema_trigger(conn):
    """アプリ検証を迂回してもトリガが最後の防衛線(一次統制はスキーマ側)。"""
    got = record_decision(conn, "nonvetoable-raw", "reject", OWNER, OWNERS, kind="pr")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="否認できない"):
            cur.execute(
                """
                INSERT INTO governance.decision_vetoes (decision_id, vetoed_by, reason)
                VALUES (%s, %s, '直接 INSERT')
                """,
                (got.id, OWNER),
            )
    conn.rollback()


def test_non_owner_veto_rejected(conn):
    """否認は代表の専権(定款第3条)— record_decision と同型のオーナー検証。"""
    deemed = _deemed(conn, "veto-by-non-owner")
    with pytest.raises(NotOwnerError):
        record_veto(
            conn, deemed.id, "越権否認",
            vetoed_by="999999", owner_ids=OWNERS,
            expected_proposal_ref=deemed.proposal_ref,
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decision_vetoes WHERE decision_id = %s",
            (deemed.id,),
        )
        assert cur.fetchone()[0] == 0
    conn.rollback()


def test_proposal_ref_mismatch_rejected(conn):
    """decision_id の取り違えは INSERT 前に失敗する(無関係な承認を汚染しない)。"""
    a = _deemed(conn, "veto-ref-a")
    _deemed(conn, "veto-ref-b")
    with pytest.raises(ProposalRefMismatchError, match="veto-ref-a"):
        record_veto(
            conn, a.id, "取り違え否認",
            vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="veto-ref-b",
        )
    assert current_decision(conn, "veto-ref-a")["is_vetoed"] is False
    conn.rollback()


def test_veto_of_unknown_decision_raises_clear_error(conn):
    """FK 違反を待たず明確なエラーにする(FK 違反はトランザクションを中断させる)。"""
    with pytest.raises(ValueError, match="存在しない"):
        record_veto(
            conn, -1, "対象なし否認",
            vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="whatever",
        )
    assert _deemed(conn, "after-bad-veto").id > 0
    conn.rollback()


@pytest.mark.parametrize("field", ["reason", "vetoed_by", "expected_proposal_ref"])
def test_veto_requires_non_blank_fields(conn, field):
    deemed = _deemed(conn, f"veto-blank-{field}")
    kwargs = {
        "reason": "理由",
        "vetoed_by": OWNER,
        "expected_proposal_ref": deemed.proposal_ref,
    }
    kwargs[field] = "   "
    with pytest.raises(ValueError, match=field):
        record_veto(
            conn, deemed.id, kwargs["reason"],
            vetoed_by=kwargs["vetoed_by"], owner_ids=OWNERS,
            expected_proposal_ref=kwargs["expected_proposal_ref"],
        )
    conn.rollback()


def test_veto_is_append_only(conn):
    """記録した否認は書き換えられない(0021 の追記オンリートリガ)。"""
    deemed = _deemed(conn, "veto-immutable")
    veto = _veto(conn, deemed, "否認")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.decision_vetoes SET reason = '改竄' WHERE veto_id = %s",
                (veto.veto_id,),
            )
    conn.rollback()
