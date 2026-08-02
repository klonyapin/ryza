"""outbox 配送の冪等性テスト(受け入れ基準: 二重送信なし）。

discord API は同期のフェイク send_fn で代替する。各テストは rollback で隔離するため、
``deliver_pending`` の内部 commit を避けたい。そこで配送のオーケストレーションは
claim_pending / mark_sent を直接組み合わせて検証し、deliver_pending 相当の冪等性を確認する。
"""

from __future__ import annotations

from ryza.bot import outbox


def _pending_ids(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM press.outbox WHERE sent_at IS NULL")
        return {r[0] for r in cur.fetchall()}


def test_enqueue_and_claim(conn, run_id):
    oid = outbox.enqueue(conn, "ops", {"title": "t"}, run_id)
    pending = outbox.claim_pending(conn)
    ids = {m.id for m in pending}
    assert oid in ids
    msg = next(m for m in pending if m.id == oid)
    assert msg.channel == "ops"
    assert msg.embed == {"title": "t"}


def test_mark_sent_is_conditional(conn, run_id):
    oid = outbox.enqueue(conn, "ops", {"title": "x"}, run_id)
    # 初回は未送→送済に遷移し True。
    assert outbox.mark_sent(conn, oid, "msg-1") is True
    # 2回目は既送なので False(二重送信防止)。
    assert outbox.mark_sent(conn, oid, "msg-2") is False
    # sent_message_id は最初の配送のものが残る。
    with conn.cursor() as cur:
        cur.execute("SELECT sent_message_id FROM press.outbox WHERE id = %s", (oid,))
        assert cur.fetchone()[0] == "msg-1"


def test_no_double_send_across_two_delivery_passes(conn, run_id):
    """同一メッセージを2周の配送に通しても send_fn は高々1回しか呼ばれない。"""
    oid = outbox.enqueue(conn, "flash", {"title": "flash"}, run_id, urgent=True)
    sent: list[int] = []

    def deliver_pass() -> None:
        for msg in outbox.claim_pending(conn):
            # フェイク送信(冪等判定は mark_sent が担う)。
            if outbox.mark_sent(conn, msg.id, f"m-{msg.id}"):
                sent.append(msg.id)

    deliver_pass()
    deliver_pass()  # 2周目: 既送なので拾わない
    assert sent.count(oid) == 1


def test_claim_skips_already_sent(conn, run_id):
    oid1 = outbox.enqueue(conn, "daily", {"n": 1}, run_id)
    oid2 = outbox.enqueue(conn, "daily", {"n": 2}, run_id)
    outbox.mark_sent(conn, oid1, "m1")
    remaining = {m.id for m in outbox.claim_pending(conn)}
    assert oid1 not in remaining
    assert oid2 in remaining


def test_urgent_first_ordering(conn, run_id):
    normal = outbox.enqueue(conn, "daily", {"n": "normal"}, run_id, urgent=False)
    urgent = outbox.enqueue(conn, "flash", {"n": "urgent"}, run_id, urgent=True)
    ordered = [m.id for m in outbox.claim_pending(conn) if m.id in {normal, urgent}]
    assert ordered.index(urgent) < ordered.index(normal)


def test_failed_send_leaves_row_pending(conn, run_id):
    """send_fn 相当が失敗したら mark_sent を呼ばず、行は未送のまま残る(次回リトライ)。"""
    oid = outbox.enqueue(conn, "audit", {"title": "a"}, run_id)
    for msg in outbox.claim_pending(conn):
        if msg.id == oid:
            # 送信失敗を模し mark_sent しない。
            pass
    assert oid in _pending_ids(conn)
