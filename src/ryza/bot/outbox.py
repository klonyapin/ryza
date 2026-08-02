"""outbox 配送(§5「通知配送」)。

``press.outbox`` をポーリングして未送(``sent_at IS NULL``)の embed を Discord へ配送する。
**冪等の核**: 配送候補の取得は ``FOR UPDATE SKIP LOCKED`` で行を占有し、配送成功時の
``mark_sent`` は ``WHERE sent_at IS NULL`` の条件付き UPDATE。これにより複数ポーラーが同時に
走っても、また同一メッセージを二度処理しても、Discord への送信は高々1回になる。

discord API には一切依存しない。実際の送信は ``deliver_pending`` に渡す ``send_fn``
(embed → メッセージ ID)が担い、テストではこれをフェイクに差し替える。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

# embed(dict）を受け取り、送信して Discord メッセージ ID を返す関数。失敗時は例外。
SendFn = Callable[["OutboxMessage"], str]


@dataclass(frozen=True)
class OutboxMessage:
    """配送対象1件。"""

    id: int
    channel: str
    embed: dict[str, Any]
    urgent: bool


def enqueue(
    conn: psycopg.Connection,
    channel: str,
    embed: dict[str, Any],
    run_id: int,
    *,
    urgent: bool = False,
) -> int:
    """``press.outbox`` に1件投入し id を返す(未送状態)。

    投入は生成側(朝刊/速報ジョブ・日報)の責務。Bot 自身も日報・起動通知などで使う。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (channel, Jsonb(embed), urgent, run_id),
        )
        return cur.fetchone()[0]


def claim_pending(
    conn: psycopg.Connection,
    *,
    limit: int = 20,
    urgent_first: bool = True,
) -> list[OutboxMessage]:
    """未送メッセージを占有して取得する(``FOR UPDATE SKIP LOCKED``)。

    呼び出し側の1トランザクション内で ``mark_sent`` まで完了させること。占有により
    並行ポーラーは同じ行を掴まない。緊急(urgent)を優先して古い順に返す。
    """
    order = "urgent DESC, created_at ASC" if urgent_first else "created_at ASC"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, channel, embed_json, urgent
            FROM press.outbox
            WHERE sent_at IS NULL
            ORDER BY {order}
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        )
        return [
            OutboxMessage(id=r[0], channel=r[1], embed=r[2], urgent=r[3])
            for r in cur.fetchall()
        ]


def mark_sent(conn: psycopg.Connection, outbox_id: int, message_id: str) -> bool:
    """配送成功を記録する。実際に未送→送済へ遷移させたときだけ True。

    条件付き UPDATE(``WHERE sent_at IS NULL``)なので、既送行を再度渡しても False を返し
    二重送信にならない(冪等)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE press.outbox
            SET sent_at = now(), sent_message_id = %s
            WHERE id = %s AND sent_at IS NULL
            RETURNING id
            """,
            (message_id, outbox_id),
        )
        return cur.fetchone() is not None


def deliver_pending(
    conn: psycopg.Connection,
    send_fn: SendFn,
    *,
    limit: int = 20,
) -> list[int]:
    """未送メッセージを配送し、配送できた outbox id 一覧を返す。

    各メッセージについて ``send_fn`` を1回だけ呼び、成功したら ``mark_sent`` する。
    ``send_fn`` が例外を投げた行は未送のまま残し(次回リトライ)、後続の配送は続ける。
    全体を1トランザクションで囲み、占有ロックが配送完了まで有効になるようにする。
    """
    delivered: list[int] = []
    try:
        pending = claim_pending(conn, limit=limit)
        for msg in pending:
            try:
                message_id = send_fn(msg)
            except Exception:  # noqa: BLE001 - 個別失敗は握り、未送のまま次回リトライ
                continue
            if mark_sent(conn, msg.id, message_id):
                delivered.append(msg.id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return delivered
