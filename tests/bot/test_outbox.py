"""outbox 配送の冪等性テスト(受け入れ基準: 二重送信なし）。

discord API は同期のフェイク send_fn で代替する。各テストは rollback で隔離するため、
``deliver_pending`` の内部 commit を避けたい。そこで配送のオーケストレーションは
claim_pending / mark_sent を直接組み合わせて検証し、deliver_pending 相当の冪等性を確認する。
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from ryza import org
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


# ── 内部キーの構造分離(0032・独立役員審査 0020 C-10)────────────────────────
def _row(conn, oid: int) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT embed_json, author_member_id FROM press.outbox WHERE id = %s", (oid,)
        )
        return cur.fetchone()


def test_enqueue_moves_member_id_out_of_embed_json(conn, run_id):
    """embed_json には Discord のフィールドだけが入り、内部キーは列へ移る。"""
    embed = {"title": "朝刊", "author": org.embed_author("aya")}
    oid = outbox.enqueue(conn, "press", embed, run_id)
    embed_json, author_member_id = _row(conn, oid)
    assert author_member_id == "aya"
    assert org.AUTHOR_MEMBER_KEY not in embed_json["author"]
    assert embed_json["author"]["name"] == org.get_member("aya").display_name
    # 呼び出し元が渡した dict は壊さない。
    assert embed["author"][org.AUTHOR_MEMBER_KEY] == "aya"


def test_claim_pending_exposes_author_member_id(conn, run_id):
    oid = outbox.enqueue(conn, "press", {"author": org.embed_author("aya")}, run_id)
    msg = next(m for m in outbox.claim_pending(conn) if m.id == oid)
    assert msg.author_member_id == "aya"
    assert org.AUTHOR_MEMBER_KEY not in msg.embed["author"]


def test_enqueue_explicit_author_member_id_wins(conn, run_id):
    """author を持たない embed にも発信者を付けられる(明示引数が優先)。"""
    oid = outbox.enqueue(conn, "ops", {"title": "t"}, run_id, author_member_id="tanya")
    _, author_member_id = _row(conn, oid)
    assert author_member_id == "tanya"


def test_enqueue_without_author_leaves_column_null(conn, run_id):
    oid = outbox.enqueue(conn, "ops", {"title": "起動通知"}, run_id)
    embed_json, author_member_id = _row(conn, oid)
    assert author_member_id is None
    assert embed_json == {"title": "起動通知"}


def test_schema_rejects_internal_key_in_embed_json(conn, run_id):
    """enqueue を通さない書込経路が内部キーを混ぜたら、DB が書込時に落とす(0032)。

    C-10 の懸念は「除去が送信直前の1関数に依存する」ことだった。除去を投入時へ移した
    だけでは「新しい書込経路が strip を忘れる」余地が残るため、表の性質として禁じる。
    """
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
            VALUES ('ops', %s, false, %s)
            """,
            (Jsonb({"author": {"name": "n", org.AUTHOR_MEMBER_KEY: "aya"}}), run_id),
        )


def test_legacy_row_still_resolves_via_embedded_key(conn, run_id):
    """0032 以前に投入された行(列 NULL・embed 内にキー)は従来経路で解決する。

    CHECK 制約は NOT VALID なので既存行には効かない。過去行を模すため制約を明示的に
    外して INSERT し、配送側(``org.apply_icon_overrides``)が旧キーで解決できることと、
    Discord へ渡る embed から旧キーが落ちることを確認する。
    """
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE press.outbox DROP CONSTRAINT outbox_embed_has_no_internal_keys_check"
        )
        cur.execute(
            """
            INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
            VALUES ('press', %s, false, %s) RETURNING id
            """,
            (
                Jsonb(
                    {
                        "title": "旧朝刊",
                        "author": {
                            "name": "n",
                            "icon_url": "https://old/x.png",
                            org.AUTHOR_MEMBER_KEY: "aya",
                        },
                    }
                ),
                run_id,
            ),
        )
        oid = cur.fetchone()[0]
    msg = next(m for m in outbox.claim_pending(conn) if m.id == oid)
    assert msg.author_member_id is None
    delivered = org.apply_icon_overrides(
        msg.embed, {"aya": "https://new/y.png"}, member_id=msg.author_member_id
    )
    assert delivered["author"]["icon_url"] == "https://new/y.png"
    assert org.AUTHOR_MEMBER_KEY not in delivered["author"]
