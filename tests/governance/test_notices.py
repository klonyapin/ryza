"""みなし承認の通知配線(src/ryza/governance/notices.py)のテスト。

検証の主眼は**原子性**である: 記録(governance.decisions)と通知(press.outbox)は
定款第3条により不可分でなければならず、片方だけが残る状態は「通知なき発効」
(= A-18 の無承認変更)か「記録なき通知」(= deemed_ratio の過少計上)を意味する。
したがって失敗経路ごとに「両方が消えていること」と「呼び出し側のトランザクションが
生きていること」を確認する。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行し、commit せず
rollback で隔離する。接続不可なら skip。
"""

from __future__ import annotations

import pytest

from ryza.bot.approvals import NotOwnerError, parse_proposal, record_decision
from ryza.bot.outbox import mark_sent
from ryza.db.conn import connect
from ryza.governance import notices
from ryza.governance.decisions import (
    DuplicateDecisionError,
    NotVetoableError,
    ReservedMatterError,
    current_decision,
)
from ryza.provenance import start_run

OWNER = "424242"
OWNERS = (OWNER,)
NOTICE = "保護領域 src/ryza/gate/** の変更。独立役員審査は docs/reviews/xxxx で完了"


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run_id(conn):
    return start_run("test.governance.notices", conn=conn).run_id


def _outbox_rows(conn, run_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, channel, urgent, embed_json FROM press.outbox "
            "WHERE run_id = %s ORDER BY id",
            (run_id,),
        )
        return cur.fetchall()


# ── embed の組立と復元 ──────────────────────────────────────────────────────
def test_deemed_embed_round_trips_proposal_ref():
    embed = notices.build_deemed_notice_embed("https://x/pull/1", "pr", NOTICE)
    assert notices.parse_deemed_notice(embed) == "https://x/pull/1"


def test_deemed_embed_is_not_an_approval_proposal():
    """承認/却下ボタンは付かない — みなし承認は通知時点で発効済みだから。"""
    embed = notices.build_deemed_notice_embed("https://x/pull/1", "pr", NOTICE)
    assert parse_proposal(embed) is None


def test_parse_deemed_notice_ignores_other_embeds():
    from ryza.bot.approvals import build_approval_embed

    assert notices.parse_deemed_notice(build_approval_embed("ref-1", "t", "b", "pr")) is None
    assert notices.parse_deemed_notice({"title": "no footer"}) is None


@pytest.mark.parametrize(
    ("kind", "notice", "match"),
    [("wishlist", NOTICE, "未知の提案種別"), ("pr", "   ", "notice")],
)
def test_deemed_embed_rejects_invalid_input(kind, notice, match):
    with pytest.raises(ValueError, match=match):
        notices.build_deemed_notice_embed("ref", kind, notice)


# ── みなし承認: 記録+通知の同時成立 ─────────────────────────────────────────
def test_announce_writes_notice_and_decision_together(conn, run_id):
    result = notices.announce_deemed_approval(
        conn, "https://x/pull/101", "pr", NOTICE, run_id
    )
    rows = _outbox_rows(conn, run_id)
    assert len(rows) == 1
    outbox_id, channel, urgent, embed = rows[0]
    assert channel == "approval" and urgent is False
    assert notices.parse_deemed_notice(embed) == "https://x/pull/101"
    # 通知参照は outbox 行を指す(Discord メッセージ ID は配送後にしか確定しない)。
    assert result.notice_ref == f"outbox:{outbox_id}"
    row = current_decision(conn, "https://x/pull/101")
    assert row["effective_decision"] == "deemed"
    assert row["decided_by"] == "system:deemed"
    conn.rollback()


def test_announce_source_reaches_decided_by(conn, run_id):
    result = notices.announce_deemed_approval(
        conn, "ips-2026-09", "other", NOTICE, run_id, source="ips_monthly_review"
    )
    assert result.decision.decided_by == "system:ips_monthly_review"
    conn.rollback()


def test_notice_message_id_resolves_after_delivery(conn, run_id):
    result = notices.announce_deemed_approval(conn, "ref-msgid", "pr", NOTICE, run_id)
    assert notices.notice_message_id(conn, result.notice_ref) is None  # 未配送
    mark_sent(conn, result.outbox_id, "999888777")
    assert notices.notice_message_id(conn, result.notice_ref) == "999888777"
    assert notices.notice_message_id(conn, "discord://承認/1") is None  # 別形式
    conn.rollback()


# ── 原子性(片方失敗で両方ロールバック)────────────────────────────────────
def test_duplicate_decision_rolls_back_the_notice(conn, run_id):
    """二重通知でも承認記録は増えず、**通知も残らない**(記録なき通知を作らない)。"""
    notices.announce_deemed_approval(conn, "dup-ref", "pr", NOTICE, run_id)
    assert len(_outbox_rows(conn, run_id)) == 1
    with pytest.raises(DuplicateDecisionError):
        notices.announce_deemed_approval(conn, "dup-ref", "pr", NOTICE, run_id)
    assert len(_outbox_rows(conn, run_id)) == 1  # 2件目の通知は SAVEPOINT ごと消えた
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decisions WHERE proposal_ref = 'dup-ref'"
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_reserved_kind_rolls_back_the_notice(conn, run_id):
    """3専決事項(定款第3条)は通知も出ない — 発効していないものを告知しない。"""
    with pytest.raises(ReservedMatterError):
        notices.announce_deemed_approval(conn, "reserved-1", "breaker_resume", NOTICE, run_id)
    assert _outbox_rows(conn, run_id) == []
    # 呼び出し側のトランザクションは生きている(SAVEPOINT で巻き戻したため)。
    assert notices.announce_deemed_approval(conn, "after-reserved", "pr", NOTICE, run_id)
    assert len(_outbox_rows(conn, run_id)) == 1
    conn.rollback()


def test_autocommit_connection_is_refused(migrated_db):
    """autocommit では SAVEPOINT が成立せず片方だけ永続化されうる — 書込前に拒否する。"""
    c = connect(autocommit=True)
    try:
        with pytest.raises(notices.AtomicityError, match="autocommit"):
            notices.announce_deemed_approval(c, "autocommit-ref", "pr", NOTICE, 1)
        with c.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM governance.decisions WHERE proposal_ref = 'autocommit-ref'"
            )
            assert cur.fetchone()[0] == 0
    finally:
        c.close()


# ── 否認(#承認 の否認ボタン → record_veto → #運営 の取消義務リマインド)─────
def test_apply_veto_records_and_notifies(conn, run_id):
    notices.announce_deemed_approval(conn, "veto-ref", "pr", NOTICE, run_id)
    result = notices.apply_veto(
        conn, "veto-ref", "リスク上限を緩める方向のため",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id,
    )
    assert current_decision(conn, "veto-ref")["effective_decision"] == "vetoed"
    ops = [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]
    assert len(ops) == 1
    _, _, urgent, embed = ops[0]
    assert urgent is True  # 取消義務は「遅滞なく」— 速報と同じ優先度で配送する
    assert "取消義務" in embed["title"]
    assert any(f["value"] == "veto-ref" for f in embed["fields"])
    assert result.veto.vetoed_by == OWNER
    conn.rollback()


def test_apply_veto_works_on_explicit_approval(conn, run_id):
    """ボタン経路は明示承認(approve)も否認できる(定款は撤回を禁じていない)。"""
    record_decision(conn, "explicit-ref", "approve", OWNER, OWNERS, kind="pr")
    notices.apply_veto(
        conn, "explicit-ref", "前提データの誤り",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id,
    )
    assert current_decision(conn, "explicit-ref")["effective_decision"] == "vetoed"
    conn.rollback()


def test_non_owner_veto_leaves_nothing(conn, run_id):
    """非オーナーの否認は記録もリマインドも残さない(否認は代表の専権)。"""
    notices.announce_deemed_approval(conn, "veto-nonowner", "pr", NOTICE, run_id)
    with pytest.raises(NotOwnerError):
        notices.apply_veto(
            conn, "veto-nonowner", "越権否認",
            vetoed_by="999999", owner_ids=OWNERS, run_id=run_id,
        )
    assert [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"] == []
    assert current_decision(conn, "veto-nonowner")["is_vetoed"] is False
    conn.rollback()


def test_double_veto_is_refused(conn, run_id):
    """ボタンの二度押しでリマインドを二重投稿しない。"""
    notices.announce_deemed_approval(conn, "veto-twice", "pr", NOTICE, run_id)
    notices.apply_veto(
        conn, "veto-twice", "否認", vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id
    )
    with pytest.raises(notices.AlreadyVetoedError):
        notices.apply_veto(
            conn, "veto-twice", "もう一度否認", vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id
        )
    assert len([r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]) == 1
    conn.rollback()


def test_veto_of_unknown_proposal_raises(conn, run_id):
    with pytest.raises(notices.UnknownProposalError):
        notices.apply_veto(
            conn, "no-such-ref", "対象なし", vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id
        )
    conn.rollback()


def test_reject_cannot_be_vetoed_through_the_button_path(conn, run_id):
    """却下は否認できない(阻止の根拠を消さない — 0021 審査 C-2)。"""
    record_decision(conn, "rejected-ref", "reject", OWNER, OWNERS, kind="pr")
    with pytest.raises(NotVetoableError):
        notices.apply_veto(
            conn, "rejected-ref", "却下を覆す",
            vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id,
        )
    assert [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"] == []
    conn.rollback()


# ── 否認の撤回(誤操作からの復旧)───────────────────────────────────────────
def test_withdraw_veto_restores_and_notifies(conn, run_id):
    notices.announce_deemed_approval(conn, "veto-undo", "pr", NOTICE, run_id)
    notices.apply_veto(
        conn, "veto-undo", "誤った対象", vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id
    )
    notices.withdraw_veto(
        conn, "veto-undo", "対象取り違えのため撤回",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id,
    )
    row = current_decision(conn, "veto-undo")
    assert row["is_vetoed"] is False
    assert row["effective_decision"] == "deemed"
    ops = [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]
    assert len(ops) == 2  # 否認 → 撤回の2通知(取消作業を止めるため同じ場所に出す)
    assert "撤回" in ops[1][3]["title"]
    conn.rollback()


def test_withdraw_without_veto_raises(conn, run_id):
    notices.announce_deemed_approval(conn, "veto-none", "pr", NOTICE, run_id)
    with pytest.raises(notices.NotVetoedError):
        notices.withdraw_veto(
            conn, "veto-none", "撤回", vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id
        )
    assert [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"] == []
    conn.rollback()


def test_non_owner_withdrawal_leaves_nothing(conn, run_id):
    notices.announce_deemed_approval(conn, "veto-undo-nonowner", "pr", NOTICE, run_id)
    notices.apply_veto(
        conn, "veto-undo-nonowner", "否認", vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id
    )
    with pytest.raises(NotOwnerError):
        notices.withdraw_veto(
            conn, "veto-undo-nonowner", "越権撤回",
            vetoed_by="999999", owner_ids=OWNERS, run_id=run_id,
        )
    assert len([r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]) == 1
    assert current_decision(conn, "veto-undo-nonowner")["is_vetoed"] is True
    conn.rollback()
