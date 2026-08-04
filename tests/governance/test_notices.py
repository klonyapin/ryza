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

import ast
from pathlib import Path

import pytest

from ryza.bot.approvals import NotOwnerError, parse_proposal, record_decision
from ryza.bot.outbox import mark_sent
from ryza.db.conn import connect
from ryza.governance import notices
from ryza.governance.decisions import (
    VETO_ORIGINS,
    DuplicateDecisionError,
    NotVetoableError,
    ReservedMatterError,
    current_decision,
)
from ryza.provenance import start_run

OWNER = "424242"
OWNERS = (OWNER,)
# 0030 の origin。Bot のボタン経路を模す(notices は Discord 経路の配線)。
ORIGIN = "discord_button"
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


@pytest.fixture
def no_denial_record(monkeypatch):
    """非オーナー拒否の記録(別接続で commit する)をテスト中は捕捉するだけにする。

    実装は拒否の痕跡を**呼び出し側の rollback から独立して**残すため autocommit の別接続を
    使う(中-6)。テストでそのまま走らせるとテスト DB に commit 済みの残留行ができるので、
    ここでは呼び出しの記録だけを取る。実際の書込は
    :func:`test_denied_attempt_is_recorded_on_its_own_connection` が検証する。
    """
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notices, "record_denied_attempt",
        lambda action, proposal_ref, actor: calls.append((action, proposal_ref, actor)),
    )
    return calls


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


@pytest.mark.parametrize("ref", ["", "   ", "pr proposal:other-ref", "x deemed:y"])
def test_deemed_embed_rejects_marker_collision(ref):
    """参照にマーカー文字列を含めない(復元が壊れ、承認ボタンが付きうる — 軽微-9)。"""
    with pytest.raises(ValueError):
        notices.build_deemed_notice_embed(ref, "pr", NOTICE)


# ── 配送時の実在照合(偽装 embed に否認ボタンを付けない — 重要-4)──────────────
def test_resolve_deemed_view_accepts_a_real_deemed_notice(conn, run_id):
    notices.announce_deemed_approval(conn, "manual:view-real", "pr", NOTICE, run_id)
    embed = notices.build_deemed_notice_embed("manual:view-real", "pr", NOTICE)
    target = notices.resolve_deemed_view(conn, embed)
    assert target.ref == "manual:view-real" and target.warning is None
    conn.rollback()


def test_resolve_deemed_view_rejects_a_forged_notice(conn):
    """DB に対応する deemed 決定が無い通知にはボタンを付けない(fail-closed)。"""
    embed = notices.build_deemed_notice_embed("view-forged", "pr", NOTICE)
    target = notices.resolve_deemed_view(conn, embed)
    assert target.ref is None
    assert "偽装" in target.warning
    conn.rollback()


def test_resolve_deemed_view_rejects_non_deemed_decision(conn):
    """明示承認(approve)の proposal_ref を騙る通知も弾く。"""
    record_decision(conn, "manual:view-explicit", "approve", OWNER, OWNERS, kind="pr")
    embed = notices.build_deemed_notice_embed("manual:view-explicit", "pr", NOTICE)
    target = notices.resolve_deemed_view(conn, embed)
    assert target.ref is None and "みなし承認でない" in target.warning
    conn.rollback()


def test_resolve_deemed_view_skips_already_vetoed(conn, run_id):
    """否認済みならボタンを出さない(押しても失敗するだけ。撤回は /unveto)。"""
    notices.announce_deemed_approval(conn, "manual:view-vetoed", "pr", NOTICE, run_id)
    notices.apply_veto(
        conn, "manual:view-vetoed", "否認",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    embed = notices.build_deemed_notice_embed("manual:view-vetoed", "pr", NOTICE)
    target = notices.resolve_deemed_view(conn, embed)
    assert target.ref is None and "既に否認済み" in target.warning
    conn.rollback()


def test_resolve_deemed_view_ignores_non_deemed_embeds(conn):
    from ryza.bot.approvals import build_approval_embed

    assert notices.resolve_deemed_view(conn, {"title": "x"}).ref is None
    approval = build_approval_embed("some-ref", "t", "b", "pr")
    assert notices.resolve_deemed_view(conn, approval).ref is None
    conn.rollback()


# ── みなし承認: 記録+通知の同時成立 ─────────────────────────────────────────
def test_announce_writes_notice_and_decision_together(conn, run_id):
    result = notices.announce_deemed_approval(
        conn, "https://github.com/x/y/pull/101", "pr", NOTICE, run_id
    )
    rows = _outbox_rows(conn, run_id)
    assert len(rows) == 1
    outbox_id, channel, urgent, embed = rows[0]
    assert channel == "approval" and urgent is False
    assert notices.parse_deemed_notice(embed) == "https://github.com/x/y/pull/101"
    # 通知参照は outbox 行を指す(Discord メッセージ ID は配送後にしか確定しない)。
    assert result.notice_ref == f"outbox:{outbox_id}"
    row = current_decision(conn, "https://github.com/x/y/pull/101")
    assert row["effective_decision"] == "deemed"
    assert row["decided_by"] == "system:deemed"
    conn.rollback()


def test_announce_source_reaches_decided_by(conn, run_id):
    result = notices.announce_deemed_approval(
        conn, "manual:ips-2026-09", "other", NOTICE, run_id, source="ips_monthly_review"
    )
    assert result.decision.decided_by == "system:ips_monthly_review"
    conn.rollback()


def test_notice_message_id_resolves_after_delivery(conn, run_id):
    result = notices.announce_deemed_approval(conn, "manual:ref-msgid", "pr", NOTICE, run_id)
    assert notices.notice_message_id(conn, result.notice_ref) is None  # 未配送
    mark_sent(conn, result.outbox_id, "999888777")
    assert notices.notice_message_id(conn, result.notice_ref) == "999888777"
    assert notices.notice_message_id(conn, "discord://承認/1") is None  # 別形式
    conn.rollback()


# ── 原子性(片方失敗で両方ロールバック)────────────────────────────────────
def test_duplicate_decision_rolls_back_the_notice(conn, run_id):
    """二重通知でも承認記録は増えず、**通知も残らない**(記録なき通知を作らない)。"""
    notices.announce_deemed_approval(conn, "manual:dup-ref", "pr", NOTICE, run_id)
    assert len(_outbox_rows(conn, run_id)) == 1
    with pytest.raises(DuplicateDecisionError):
        notices.announce_deemed_approval(conn, "manual:dup-ref", "pr", NOTICE, run_id)
    assert len(_outbox_rows(conn, run_id)) == 1  # 2件目の通知は SAVEPOINT ごと消えた
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decisions WHERE proposal_ref = 'manual:dup-ref'"
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_reserved_kind_rolls_back_the_notice(conn, run_id):
    """3専決事項(定款第3条)は通知も出ない — 発効していないものを告知しない。"""
    with pytest.raises(ReservedMatterError):
        notices.announce_deemed_approval(
            conn, "manual:reserved-1", "breaker_resume", NOTICE, run_id
        )
    assert _outbox_rows(conn, run_id) == []
    # 呼び出し側のトランザクションは生きている(SAVEPOINT で巻き戻したため)。
    assert notices.announce_deemed_approval(conn, "manual:after-reserved", "pr", NOTICE, run_id)
    assert len(_outbox_rows(conn, run_id)) == 1
    conn.rollback()


def test_outbox_failure_leaves_no_decision(conn, run_id, monkeypatch):
    """**逆方向のフォールト注入**(独立役員審査 軽微-12): 通知側が落ちたら記録も残らない。

    既存の原子性テストは「記録の失敗で通知が消える」方向だけを見ていた。逆向き
    ——通知の書込が途中まで進んでから失敗する——は、放置すると「記録なき通知」ではなく
    **書きかけの通知行が残ったまま記録が無い**状態を作りうる。enqueue が行を書いた直後に
    落ちる障害を注入し、SAVEPOINT が両方を巻き戻すことを確かめる。
    """
    real_enqueue = notices.enqueue

    def failing_enqueue(c, channel, embed, rid, **kwargs):
        real_enqueue(c, channel, embed, rid, **kwargs)  # 行を書いてから落ちる
        raise RuntimeError("通知配送系の障害を模擬")

    monkeypatch.setattr(notices, "enqueue", failing_enqueue)
    with pytest.raises(RuntimeError, match="模擬"):
        notices.announce_deemed_approval(conn, "manual:fault-outbox", "pr", NOTICE, run_id)
    assert _outbox_rows(conn, run_id) == []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decisions WHERE proposal_ref = 'manual:fault-outbox'"
        )
        assert cur.fetchone()[0] == 0
    # 呼び出し側のトランザクションは生きている(巻き戻しは SAVEPOINT まで)。
    monkeypatch.undo()
    assert notices.announce_deemed_approval(conn, "manual:fault-after", "pr", NOTICE, run_id)
    assert len(_outbox_rows(conn, run_id)) == 1
    conn.rollback()


def test_outbox_constraint_violation_rolls_back_both(conn, run_id):
    """DB 側の障害(``press.outbox.run_id`` の NOT NULL 違反)でも双方が消える。

    Python 側の例外(前のテスト)と違い、制約違反は**トランザクションを abort 状態にする**。
    SAVEPOINT で包んでいなければ、以降の文が全て失敗して呼び出し側の作業ごと道連れになる。
    """
    import psycopg

    with pytest.raises(psycopg.errors.NotNullViolation):
        # run_id=None で outbox の NOT NULL に触れさせる(通知側だけを DB 層で失敗させる)。
        notices.announce_deemed_approval(conn, "manual:fault-null", "pr", NOTICE, None)  # type: ignore[arg-type]
    assert _outbox_rows(conn, run_id) == []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decisions"
            " WHERE proposal_ref = 'manual:fault-null'"
        )
        assert cur.fetchone()[0] == 0
    # abort した文の後でも呼び出し側のトランザクションは使える(SAVEPOINT まで戻った)。
    assert notices.announce_deemed_approval(conn, "manual:fault-null-after", "pr", NOTICE, run_id)
    conn.rollback()


def test_announce_on_an_idle_connection_does_not_commit(migrated_db):
    """CLI 経路(新規接続で announce)で ``transaction()`` が COMMIT に化けないこと。

    psycopg の ``conn.transaction()`` は**最も外側なら exit 時に COMMIT する**。CLI は
    まっさらな接続(``IDLE``)で announce を呼ぶため、無害な文でトランザクションを開いて
    おかないと、SAVEPOINT のつもりの束ねが確定してしまい、呼び出し側の rollback が効かない
    (= 失敗しても記録と通知が残る)。軽微-12 で未カバーだった分岐。
    """
    from psycopg import pq

    c = connect()
    run = start_run("test.governance.notices.idle", conn=c)
    c.commit()  # Run だけ確定させ、接続を IDLE(CLI 起動直後と同じ状態)に戻す
    try:
        assert c.info.transaction_status == pq.TransactionStatus.IDLE
        result = notices.announce_deemed_approval(c, "manual:idle-ref", "pr", NOTICE, run.run_id)
        assert current_decision(c, "manual:idle-ref") is not None
        c.rollback()
        assert current_decision(c, "manual:idle-ref") is None
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM press.outbox WHERE id = %s", (result.outbox_id,))
            assert cur.fetchone()[0] == 0
    finally:
        with c.cursor() as cur:  # 確定させた Run 行だけ後始末する
            cur.execute("DELETE FROM meta.runs WHERE run_id = %s", (run.run_id,))
        c.commit()
        c.close()


def test_autocommit_connection_is_refused(migrated_db):
    """autocommit では SAVEPOINT が成立せず片方だけ永続化されうる — 書込前に拒否する。"""
    c = connect(autocommit=True)
    try:
        with pytest.raises(notices.AtomicityError, match="autocommit"):
            notices.announce_deemed_approval(c, "manual:autocommit-ref", "pr", NOTICE, 1)
        with c.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM governance.decisions"
                " WHERE proposal_ref = 'manual:autocommit-ref'"
            )
            assert cur.fetchone()[0] == 0
    finally:
        c.close()


# ── 否認(#承認 の否認ボタン → record_veto → #運営 の取消義務リマインド)─────
def test_apply_veto_records_and_notifies(conn, run_id):
    notices.announce_deemed_approval(conn, "manual:veto-ref", "pr", NOTICE, run_id)
    result = notices.apply_veto(
        conn, "manual:veto-ref", "リスク上限を緩める方向のため",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    assert current_decision(conn, "manual:veto-ref")["effective_decision"] == "vetoed"
    ops = [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]
    assert len(ops) == 1
    _, _, urgent, embed = ops[0]
    assert urgent is True  # 取消義務は「遅滞なく」— 速報と同じ優先度で配送する
    assert "取消義務" in embed["title"]
    assert any(f["value"] == "manual:veto-ref" for f in embed["fields"])
    assert result.veto.vetoed_by == OWNER
    conn.rollback()


def test_apply_veto_works_on_explicit_approval(conn, run_id):
    """ボタン経路は明示承認(approve)も否認できる(定款は撤回を禁じていない)。"""
    record_decision(conn, "manual:explicit-ref", "approve", OWNER, OWNERS, kind="pr")
    notices.apply_veto(
        conn, "manual:explicit-ref", "前提データの誤り",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    assert current_decision(conn, "manual:explicit-ref")["effective_decision"] == "vetoed"
    conn.rollback()


def test_non_owner_veto_leaves_nothing(conn, run_id, no_denial_record):
    """非オーナーの否認は記録もリマインドも残さず、拒否の痕跡だけが残る(中-6)。"""
    notices.announce_deemed_approval(conn, "manual:veto-nonowner", "pr", NOTICE, run_id)
    with pytest.raises(NotOwnerError):
        notices.apply_veto(
            conn, "manual:veto-nonowner", "越権否認",
            vetoed_by="999999", owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
        )
    assert [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"] == []
    assert current_decision(conn, "manual:veto-nonowner")["is_vetoed"] is False
    assert no_denial_record == [("veto", "manual:veto-nonowner", "999999")]
    conn.rollback()


def test_owner_check_precedes_db_read(conn, run_id, no_denial_record):
    """権限検査は DB 読取より前 — 存在しない提案でも NotOwnerError が先に出る(中-6)。

    権限の無い呼び出しに現決定を読ませない(読取自体が情報の露出)。
    """
    with pytest.raises(NotOwnerError):
        notices.apply_veto(
            conn, "no-such-proposal", "越権否認",
            vetoed_by="999999", owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
        )
    assert no_denial_record == [("veto", "no-such-proposal", "999999")]
    conn.rollback()


def test_denied_attempt_is_recorded_on_its_own_connection(migrated_db):
    """拒否の記録は呼び出し側の rollback に巻き込まれない(別接続で commit する)。"""
    outbox_id = notices.record_denied_attempt("veto", "denied-ref", "999999")
    assert outbox_id is not None
    c = connect()
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT channel, urgent, embed_json->>'title' FROM press.outbox WHERE id = %s",
                (outbox_id,),
            )
            channel, urgent, title = cur.fetchone()
            assert channel == "ops" and urgent is True and "拒否" in title
            # テスト DB に残留させない(記録は commit 済みなので明示的に消す)。
            cur.execute("DELETE FROM press.outbox WHERE id = %s", (outbox_id,))
        c.commit()
    finally:
        c.close()


def test_veto_records_run_id(conn, run_id):
    """否認の出所を事後に辿れるよう run_id を記録する(独立役員審査 重要-5 後段)。"""
    notices.announce_deemed_approval(conn, "manual:veto-runid", "pr", NOTICE, run_id)
    result = notices.apply_veto(
        conn, "manual:veto-runid", "否認",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id FROM governance.decision_vetoes WHERE veto_id = %s",
            (result.veto.veto_id,),
        )
        assert cur.fetchone()[0] == run_id
    notices.withdraw_veto(
        conn, "manual:veto-runid", "撤回",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decision_vetoes "
            "WHERE run_id IS NULL AND decision_id = "
            "(SELECT id FROM governance.decisions WHERE proposal_ref = 'manual:veto-runid')"
        )
        assert cur.fetchone()[0] == 0
    conn.rollback()


def test_double_veto_is_refused(conn, run_id):
    """ボタンの二度押しでリマインドを二重投稿しない。"""
    notices.announce_deemed_approval(conn, "manual:veto-twice", "pr", NOTICE, run_id)
    notices.apply_veto(
        conn, "manual:veto-twice", "否認",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    with pytest.raises(notices.AlreadyVetoedError):
        notices.apply_veto(
            conn, "manual:veto-twice", "もう一度否認",
            vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
        )
    assert len([r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]) == 1
    conn.rollback()


def test_veto_of_unknown_proposal_raises(conn, run_id):
    with pytest.raises(notices.UnknownProposalError):
        notices.apply_veto(
            conn, "no-such-ref", "対象なし",
            vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
        )
    conn.rollback()


def test_reject_cannot_be_vetoed_through_the_button_path(conn, run_id):
    """却下は否認できない(阻止の根拠を消さない — 0021 審査 C-2)。"""
    record_decision(conn, "manual:rejected-ref", "reject", OWNER, OWNERS, kind="pr")
    with pytest.raises(NotVetoableError):
        notices.apply_veto(
            conn, "manual:rejected-ref", "却下を覆す",
            vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
        )
    assert [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"] == []
    conn.rollback()


# ── 否認の撤回(誤操作からの復旧)───────────────────────────────────────────
def test_withdraw_veto_restores_and_notifies(conn, run_id):
    notices.announce_deemed_approval(conn, "manual:veto-undo", "pr", NOTICE, run_id)
    notices.apply_veto(
        conn, "manual:veto-undo", "誤った対象",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    notices.withdraw_veto(
        conn, "manual:veto-undo", "対象取り違えのため撤回",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    row = current_decision(conn, "manual:veto-undo")
    assert row["is_vetoed"] is False
    assert row["effective_decision"] == "deemed"
    ops = [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]
    assert len(ops) == 2  # 否認 → 撤回の2通知(取消作業を止めるため同じ場所に出す)
    assert "撤回" in ops[1][3]["title"]
    conn.rollback()


def test_withdraw_without_veto_raises(conn, run_id):
    notices.announce_deemed_approval(conn, "manual:veto-none", "pr", NOTICE, run_id)
    with pytest.raises(notices.NotVetoedError):
        notices.withdraw_veto(
            conn, "manual:veto-none", "撤回",
            vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
        )
    assert [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"] == []
    conn.rollback()


def test_non_owner_withdrawal_leaves_nothing(conn, run_id, no_denial_record):
    notices.announce_deemed_approval(conn, "manual:veto-undo-nonowner", "pr", NOTICE, run_id)
    notices.apply_veto(
        conn, "manual:veto-undo-nonowner", "否認",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
    )
    with pytest.raises(NotOwnerError):
        notices.withdraw_veto(
            conn, "manual:veto-undo-nonowner", "越権撤回",
            vetoed_by="999999", owner_ids=OWNERS, run_id=run_id, origin=ORIGIN,
        )
    assert len([r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]) == 1
    assert current_decision(conn, "manual:veto-undo-nonowner")["is_vetoed"] is True
    conn.rollback()


# ── 否認の出所 origin(0030 / 独立役員審査 0021 C-8・重要-5)────────────────────
@pytest.mark.parametrize("origin", ["discord_button", "discord_command"])
def test_apply_veto_records_origin(conn, run_id, origin):
    """記録経路がそのまま行に残り、``#運営`` の通知にも出る。

    run_id では代替できない: ボタン経路と ``/veto`` は同じ job_name で Run を開くため、
    meta.runs を辿っても両者は区別できない(0030)。
    """
    ref = f"manual:veto-origin-{origin}"
    notices.announce_deemed_approval(conn, ref, "pr", NOTICE, run_id)
    result = notices.apply_veto(
        conn, ref, "否認", vetoed_by=OWNER, owner_ids=OWNERS,
        run_id=run_id, origin=origin,
    )
    assert result.veto.origin == origin
    with conn.cursor() as cur:
        cur.execute(
            "SELECT origin FROM governance.decision_vetoes WHERE veto_id = %s",
            (result.veto.veto_id,),
        )
        assert cur.fetchone()[0] == origin
    assert current_decision(conn, ref)["veto_origin"] == origin
    ops = [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"]
    assert any(f["value"] == origin for f in ops[-1][3]["fields"])
    conn.rollback()


def test_withdraw_veto_records_its_own_origin(conn, run_id):
    """撤回の出所は否認の出所と独立に残る(経路をまたいだ撤回がありうる)。"""
    notices.announce_deemed_approval(conn, "manual:veto-origin-undo", "pr", NOTICE, run_id)
    notices.apply_veto(
        conn, "manual:veto-origin-undo", "否認", vetoed_by=OWNER, owner_ids=OWNERS,
        run_id=run_id, origin="discord_button",
    )
    result = notices.withdraw_veto(
        conn, "manual:veto-origin-undo", "撤回", vetoed_by=OWNER, owner_ids=OWNERS,
        run_id=run_id, origin="discord_command",
    )
    assert result.veto.origin == "discord_command"
    # 現決定 view は最新行(= 撤回)の出所を返す。
    assert current_decision(conn, "manual:veto-origin-undo")["veto_origin"] == "discord_command"
    conn.rollback()


def test_unknown_origin_is_rejected_before_any_write(conn, run_id):
    """語彙外の出所は書き込む前に落とす(CheckViolation で通知まで巻き添えにしない)。"""
    notices.announce_deemed_approval(conn, "manual:veto-origin-bad", "pr", NOTICE, run_id)
    with pytest.raises(ValueError, match="未知の否認の出所"):
        notices.apply_veto(
            conn, "manual:veto-origin-bad", "否認", vetoed_by=OWNER, owner_ids=OWNERS,
            run_id=run_id, origin="webhook",
        )
    assert current_decision(conn, "manual:veto-origin-bad")["is_vetoed"] is False
    assert [r for r in _outbox_rows(conn, run_id) if r[1] == "ops"] == []
    conn.rollback()


def test_bot_entry_points_declare_their_origin():
    """Discord の3経路が正しい origin を渡していることを AST で固定する。

    ``_veto_sync`` はボタン経路と ``/veto`` で共有されており、経路の申告はその上でしか
    できない(同じ job_name で Run を開くため run_id では区別できない)。ここが黙って
    1つの値へ退化すると、0030 の列は「常に同じ値が入るだけの列」になり統制として死ぬ。
    discord.py の UI を実際に叩くのは現実的でないので、呼び出しの実引数を静的に見る。
    """
    src = Path(__file__).resolve().parents[2] / "src" / "ryza" / "bot" / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"_veto_sync", "_withdraw_veto_sync"}:
            continue
        last = node.args[-1]
        assert isinstance(last, ast.Constant) and isinstance(last.value, str), (
            f"{node.func.id} の origin がリテラルでない(経路の申告が読み取れない)"
        )
        assert last.value in VETO_ORIGINS, f"未知の origin: {last.value}"
        found.append((node.func.id, last.value))
    assert sorted(found) == [
        ("_veto_sync", "discord_button"),  # #承認 の否認ボタン → VetoModal
        ("_veto_sync", "discord_command"),  # /veto
        ("_withdraw_veto_sync", "discord_command"),  # /unveto
    ]
