"""開発室(migration 0024 / ``ryza.governance.devchat``)の DB 層テスト。

代表指示 2026-08-03「ダッシュボードから設計リードへ開発の連絡を送れるように」。
検証するのは4点:

1. **追記オンリー**(DELETE / 本文 UPDATE / TRUNCATE が DB 層で拒まれる)
2. **``relayed_at`` だけが例外**(NULL → 時刻の一方向遷移のみ通る)
3. **中継クエリ**(未中継の代表発言だけを拾い、outbox へ載せて相印を立てる)
4. **CLI**(``--reply`` が design_lead として追記する)

テスト DB に対して実行し commit しない(rollback 隔離)。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.db.conn import connect
from ryza.governance import devchat
from ryza.provenance import start_run


@pytest.fixture
def conn(migrated_db):
    """rollback で隔離する接続。

    ``relay_pending`` が ``conn.transaction()``(SAVEPOINT)を使うため、先に 1 文
    実行してトランザクションを開いておく(未開始だと脱出時に COMMIT され隔離が壊れる
    — tests/ops/test_org_icon_overrides.py と同じ理由)。
    """
    c = connect()
    c.execute("SELECT 1")
    with c.cursor() as cur:
        # 他セッションが commit した残留行があると「先頭 1 件」系の assert が壊れる。
        # 追記オンリーのためトリガを一時的に外して消し、rollback で全て巻き戻す。
        cur.execute("ALTER TABLE ops.dev_chat DISABLE TRIGGER USER")
        cur.execute("DELETE FROM ops.dev_chat")
        cur.execute("ALTER TABLE ops.dev_chat ENABLE TRIGGER USER")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run(conn):
    return start_run("test.devchat", {"task": "devchat"}, conn=conn)


def _rows(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, sender, body, relayed_at FROM ops.dev_chat ORDER BY id")
        return cur.fetchall()


def _expect_rejected(conn, sql: str, error):
    """``sql`` が ``error`` で拒否されることを検証する(SAVEPOINT で外側 tx を守る)。"""
    with pytest.raises(error), conn.transaction():
        conn.execute(sql)


# ── 投稿と読み出し ────────────────────────────────────────────────────────────
def test_post_returns_id_and_thread_is_chronological(conn):
    first = devchat.post_representative(conn, "0024 を実装して")
    second = devchat.post_design_lead(conn, "了解。worktree で着手する")
    thread = devchat.fetch_thread(conn)
    assert [m.id for m in thread] == [first, second]  # 新しい順ではなく時系列
    assert [m.sender for m in thread] == ["representative", "design_lead"]
    assert thread[0].relayed_at is None


def test_thread_limit_keeps_the_most_recent_messages(conn):
    for i in range(5):
        devchat.post_representative(conn, f"連絡 {i}")
    thread = devchat.fetch_thread(conn, limit=2)
    assert [m.body for m in thread] == ["連絡 3", "連絡 4"]  # 直近 2 件・時系列


def test_post_strips_and_rejects_empty_body(conn):
    with pytest.raises(ValueError):
        devchat.post_representative(conn, "   \n ")
    devchat.post_representative(conn, "  余白付き  ")
    assert devchat.fetch_thread(conn)[-1].body == "余白付き"


def test_unknown_sender_is_rejected_before_reaching_the_check(conn):
    with pytest.raises(devchat.SenderError):
        devchat.post(conn, "auditor", "本文")
    assert _rows(conn) == []


def test_schema_check_blocks_unknown_sender_from_raw_sql(conn):
    """アプリを迂回した INSERT でも未知の発言者は入らない(最後の防壁)。"""
    _expect_rejected(
        conn,
        "INSERT INTO ops.dev_chat (sender, body) VALUES ('auditor', 'x')",
        psycopg.errors.CheckViolation,
    )


# ── 追記オンリー(0024)────────────────────────────────────────────────────────
def test_delete_is_rejected(conn):
    devchat.post_representative(conn, "消せないこと")
    _expect_rejected(conn, "DELETE FROM ops.dev_chat", psycopg.errors.RaiseException)
    assert len(_rows(conn)) == 1


def test_body_and_sender_updates_are_rejected(conn):
    message_id = devchat.post_representative(conn, "原文")
    for sql in (
        f"UPDATE ops.dev_chat SET body = '改竄' WHERE id = {message_id}",
        f"UPDATE ops.dev_chat SET sender = 'design_lead' WHERE id = {message_id}",
        f"UPDATE ops.dev_chat SET created_at = now() WHERE id = {message_id}",
    ):
        _expect_rejected(conn, sql, psycopg.errors.RaiseException)  # noqa: S608
    assert _rows(conn)[0][2] == "原文"


def test_truncate_is_blocked(conn):
    _expect_rejected(conn, "TRUNCATE ops.dev_chat", psycopg.errors.RaiseException)


# ── relayed_at だけが可変(列レベルの例外)─────────────────────────────────────
def test_mark_relayed_sets_the_timestamp_once(conn):
    message_id = devchat.post_representative(conn, "中継対象")
    assert devchat.mark_relayed(conn, message_id) is True
    assert _rows(conn)[0][3] is not None
    # 二度目は条件付き UPDATE が 0 行になり False(トリガまで到達しない)。
    assert devchat.mark_relayed(conn, message_id) is False


def test_relayed_at_cannot_be_rewritten_or_cleared(conn):
    message_id = devchat.post_representative(conn, "中継対象")
    devchat.mark_relayed(conn, message_id)
    _expect_rejected(
        conn,
        f"UPDATE ops.dev_chat SET relayed_at = now() WHERE id = {message_id}",  # noqa: S608
        psycopg.errors.RaiseException,
    )
    _expect_rejected(
        conn,
        f"UPDATE ops.dev_chat SET relayed_at = NULL WHERE id = {message_id}",  # noqa: S608
        psycopg.errors.RaiseException,
    )
    assert _rows(conn)[0][3] is not None


def test_relayed_at_cannot_be_cleared_on_an_unrelayed_row(conn):
    """未中継の行を「NULL のまま UPDATE」する経路も塞ぐ(遷移でない UPDATE は通さない)。"""
    message_id = devchat.post_representative(conn, "未中継")
    _expect_rejected(
        conn,
        f"UPDATE ops.dev_chat SET relayed_at = NULL WHERE id = {message_id}",  # noqa: S608
        psycopg.errors.RaiseException,
    )


# ── 中継クエリ ────────────────────────────────────────────────────────────────
def _fake_enqueue(recorded: list[tuple]):
    def _enqueue(conn, channel, embed, run_id):  # noqa: ANN001, ANN202
        recorded.append((channel, embed, run_id))
        return len(recorded)

    return _enqueue


def test_claim_unrelayed_only_takes_representative_messages(conn):
    rep = devchat.post_representative(conn, "代表の連絡")
    devchat.post_design_lead(conn, "設計リードの返信")
    claimed = devchat.claim_unrelayed(conn)
    assert [m.id for m in claimed] == [rep]


def test_has_pending_reflects_relay_state(conn):
    assert devchat.has_pending(conn) is False
    devchat.post_design_lead(conn, "返信だけでは中継対象にならない")
    assert devchat.has_pending(conn) is False
    message_id = devchat.post_representative(conn, "連絡")
    assert devchat.has_pending(conn) is True
    devchat.mark_relayed(conn, message_id)
    assert devchat.has_pending(conn) is False


def test_relay_pending_enqueues_and_marks(conn):
    message_id = devchat.post_representative(conn, "デプロイをお願い")
    recorded: list[tuple] = []
    assert devchat.relay_pending(conn, 0, enqueue=_fake_enqueue(recorded)) == [message_id]
    channel, embed, run_id = recorded[0]
    assert channel == devchat.RELAY_CHANNEL == "dev"
    assert embed["description"] == f"{devchat.RELAY_PREFIX}デプロイをお願い"
    assert run_id == 0
    assert devchat.has_pending(conn) is False


def test_relay_pending_is_idempotent_across_calls(conn):
    devchat.post_representative(conn, "一度だけ流れること")
    recorded: list[tuple] = []
    enqueue = _fake_enqueue(recorded)
    devchat.relay_pending(conn, 0, enqueue=enqueue)
    assert devchat.relay_pending(conn, 0, enqueue=enqueue) == []
    assert len(recorded) == 1


def test_relay_pending_leaves_failed_messages_unrelayed(conn, caplog):
    """enqueue が落ちた件は未中継のまま残り、**黙って飛ばされない**。"""
    devchat.post_representative(conn, "失敗する連絡")

    def _boom(conn, channel, embed, run_id):  # noqa: ANN001, ANN202
        raise RuntimeError("outbox への投入に失敗")

    with caplog.at_level("WARNING", logger="ryza.governance.devchat"):
        assert devchat.relay_pending(conn, 0, enqueue=_boom) == []
    assert devchat.has_pending(conn) is True  # 次回リトライされる
    assert any("中継に失敗" in r.message for r in caplog.records)


def test_relay_pending_does_not_mark_when_enqueue_fails(conn):
    """enqueue と relayed_at は同じ単位。片方だけが残る経路を作らない。"""
    message_id = devchat.post_representative(conn, "原子性")

    def _boom_after_mark(conn, channel, embed, run_id):  # noqa: ANN001, ANN202
        devchat.mark_relayed(conn, message_id)  # enqueue の前段で相印が立った状況
        raise RuntimeError("投入に失敗")

    devchat.relay_pending(conn, 0, enqueue=_boom_after_mark)
    assert _rows(conn)[0][3] is None  # SAVEPOINT で巻き戻っている


def test_relay_pending_continues_after_one_failure(conn):
    first = devchat.post_representative(conn, "1 件目")
    second = devchat.post_representative(conn, "2 件目")
    recorded: list[tuple] = []
    ok = _fake_enqueue(recorded)

    def _fail_first(conn, channel, embed, run_id):  # noqa: ANN001, ANN202
        if f"{devchat.RELAY_PREFIX}1 件目" == embed["description"]:
            raise RuntimeError("1 件目だけ失敗")
        return ok(conn, channel, embed, run_id)

    assert devchat.relay_pending(conn, 0, enqueue=_fail_first) == [second]
    assert [m.id for m in devchat.claim_unrelayed(conn)] == [first]


def test_relay_embed_truncates_overlong_bodies(conn):
    body = "あ" * (devchat.RELAY_BODY_LIMIT + 100)
    message_id = devchat.post_representative(conn, body)
    embed = devchat.relay_embed(devchat.fetch_thread(conn)[-1])
    assert len(embed["description"]) < devchat.RELAY_BODY_LIMIT + 100
    assert embed["description"].endswith("ダッシュボードの開発室)")
    assert f"#{message_id}" in embed["footer"]["text"]


def test_relay_pending_uses_the_real_outbox_by_default(conn, run):
    """既定の enqueue は ``press.outbox``(配送の冪等・リトライを再実装しない)。"""
    devchat.post_representative(conn, "実 outbox 経由")
    assert devchat.relay_pending(conn, run.run_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, embed_json FROM press.outbox ORDER BY id DESC LIMIT 1"
        )
        channel, embed = cur.fetchone()
    assert channel == "dev"
    assert embed["description"] == f"{devchat.RELAY_PREFIX}実 outbox 経由"


# ── CLI(設計リードの返信経路)──────────────────────────────────────────────
def test_cli_reply_posts_as_design_lead(conn, monkeypatch):
    """``python -m ryza.governance.devchat --reply`` 相当。commit は monkeypatch で無効化。

    CLI は自前で接続を開くため、テストの隔離トランザクションへ差し替える
    (``connect`` は context manager として使われるので with を通せる形にする)。
    """
    from contextlib import nullcontext

    from ryza.db import conn as conn_mod

    monkeypatch.setattr(conn_mod, "connect", lambda *a, **k: nullcontext(conn))
    monkeypatch.setattr(conn, "commit", lambda: None)
    assert devchat.main(["--reply", "実装を始める"]) == 0
    last = devchat.fetch_thread(conn)[-1]
    assert (last.sender, last.body) == ("design_lead", "実装を始める")
    assert last.relayed_at is None  # 設計リードの発言は中継対象外


def test_cli_reply_reads_stdin_when_flag_omitted(conn, monkeypatch):
    import io
    from contextlib import nullcontext

    from ryza.db import conn as conn_mod

    monkeypatch.setattr(conn_mod, "connect", lambda *a, **k: nullcontext(conn))
    monkeypatch.setattr(conn, "commit", lambda: None)
    monkeypatch.setattr("sys.stdin", io.StringIO("here-doc の長文"))
    assert devchat.main([]) == 0
    assert devchat.fetch_thread(conn)[-1].body == "here-doc の長文"


def test_cli_list_prints_thread_without_writing(conn, monkeypatch, capsys):
    from contextlib import nullcontext

    from ryza.db import conn as conn_mod

    devchat.post_representative(conn, "代表からの連絡")
    monkeypatch.setattr(conn_mod, "connect", lambda *a, **k: nullcontext(conn))
    assert devchat.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "代表からの連絡" in out and "[代表]" in out and "[未中継]" in out
    assert len(_rows(conn)) == 1
