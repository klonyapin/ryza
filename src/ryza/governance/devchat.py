"""devchat — 開発室(代表 ⇄ 設計リードの非同期連絡窓口。0024・代表指示 2026-08-03)。

代表がダッシュボードの「開発室」ページから設計リード(Claude Code セッション)へ
連絡し、設計リードが CLI から返信する経路の**DB 層と中継ロジック**。UI(Streamlit)と
discord.py には一切依存せず、テストはライブ DB のみで通る(``ryza.bot.outbox`` と同じ流儀)。

三つの経路がこのモジュールを共有する:

1. **代表の投稿**  … ``dashboard/app.py`` の開発室ページ → ``post_representative``
2. **Discord 中継** … Bot の配送ループ → ``relay_pending``(``press.outbox`` へ enqueue し
   ``relayed_at`` を立てる。実配送は既存の outbox 配送が担う)
3. **設計リードの返信** … ``python -m ryza.governance.devchat --reply "..."``

**なぜ Discord へ直接投げず outbox へ enqueue するのか**: 配送の冪等
(``sent_at`` の条件付き UPDATE)・リトライ・webhook によるキャラクター表示は
``ryza.bot.outbox`` と ``ryza.bot.webhooks`` に既にある。中継が自前で Discord API を
叩くと、その全てを二重に実装することになる。本モジュールが持つ状態は
「outbox へ載せたか(``relayed_at``)」だけで、そこから先は既存経路に委ねる。

**``~/.ryza/`` にヘルパを置かない理由**(設計リード裁定 2026-08-03): ホーム配下は
リポジトリ外でありレビューも CI も届かない。``ryza.bridge_send`` が
``~/.ryza/discord_bot.env`` に資格情報を置いている分散状態は既知の負債であり
(``ops/reminders.yaml`` の ``db-role-separation-webhook-url``)、新しい経路を同じ場所へ
増やさない。本 CLI は ``RYZA_DATABASE_URL`` さえあれば VM でも手元でも動く。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from ryza.bot import COLOR_NORMAL, outbox

log = logging.getLogger("ryza.governance.devchat")

#: 発言者。DB の CHECK(0024)と一致させること。
REPRESENTATIVE = "representative"
DESIGN_LEAD = "design_lead"
SENDERS = (REPRESENTATIVE, DESIGN_LEAD)

#: 中継先の論理チャンネル。Bot が開発対話に使っている #dev(``ryza.bot.CHANNELS``)。
RELAY_CHANNEL = "dev"

#: 中継 embed の見出し。Discord 側で通常の Bot 通知と即座に区別できるようにする。
RELAY_PREFIX = "🛠️【開発室】"

#: Discord embed の description 上限は 4096。中継時に本文を切る位置(接頭辞込みの余裕)。
RELAY_BODY_LIMIT = 3800


@dataclass(frozen=True)
class DevChatMessage:
    """スレッドの 1 発言。"""

    id: int
    sender: str
    body: str
    created_at: datetime
    relayed_at: datetime | None = None

    @property
    def relayed(self) -> bool:
        return self.relayed_at is not None


class SenderError(ValueError):
    """未知の発言者(DB の CHECK に触れる前にアプリ側で弾く)。"""


# ── 書込 ──────────────────────────────────────────────────────────────────────
def post(conn: psycopg.Connection, sender: str, body: str) -> int:
    """1 発言を追記して id を返す(呼び出し側が commit / autocommit)。

    空文字・空白のみの本文は書かない。誤操作の空発言が Discord へ中継されると、
    受け手には「何か送られたが中身が無い」という解釈不能な通知になる。
    """
    if sender not in SENDERS:
        raise SenderError(f"未知の発言者: {sender!r}(許可: {', '.join(SENDERS)})")
    text = body.strip()
    if not text:
        raise ValueError("本文が空(投稿しない)")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.dev_chat (sender, body) VALUES (%s, %s) RETURNING id",
            (sender, text),
        )
        return cur.fetchone()[0]


def post_representative(conn: psycopg.Connection, body: str) -> int:
    """代表の発言(ダッシュボードの開発室ページ)。中継対象になる。"""
    return post(conn, REPRESENTATIVE, body)


def post_design_lead(conn: psycopg.Connection, body: str) -> int:
    """設計リードの発言(CLI)。中継はしない — 代表はダッシュボードで読む。"""
    return post(conn, DESIGN_LEAD, body)


# ── 読取 ──────────────────────────────────────────────────────────────────────
def fetch_thread(conn: psycopg.Connection, *, limit: int = 200) -> list[DevChatMessage]:
    """スレッドを**時系列(古い順)**で返す。会話として読むため新しい順にはしない。

    件数が上限を超えたときは**直近 ``limit`` 件**を返す(古い方を落とす)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, sender, body, created_at, relayed_at
            FROM (
                SELECT id, sender, body, created_at, relayed_at
                FROM ops.dev_chat ORDER BY id DESC LIMIT %s
            ) recent
            ORDER BY id
            """,
            (limit,),
        )
        return [DevChatMessage(*row) for row in cur.fetchall()]


def has_pending(conn: psycopg.Connection) -> bool:
    """未中継の代表発言があるか(中継ループが Run を起こす前の軽い判定)。

    5 秒間隔のポーリングで毎回 ``meta.runs`` に行を作ると、実行記録が中継の空振りで
    埋まる。実際に中継するものがあるときだけ Run を開始するための述語。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM ops.dev_chat
            WHERE sender = %s AND relayed_at IS NULL LIMIT 1
            """,
            (REPRESENTATIVE,),
        )
        return cur.fetchone() is not None


def claim_unrelayed(
    conn: psycopg.Connection, *, limit: int = 20
) -> list[DevChatMessage]:
    """未中継の代表発言を占有して古い順に取得する(``FOR UPDATE SKIP LOCKED``)。

    ``outbox.claim_pending`` と同じ冪等の作法。並行ポーラー(Bot の再起動直後など)が
    同じ行を掴まないため、二重に Discord へ流れない。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, sender, body, created_at, relayed_at
            FROM ops.dev_chat
            WHERE sender = %s AND relayed_at IS NULL
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (REPRESENTATIVE, limit),
        )
        return [DevChatMessage(*row) for row in cur.fetchall()]


def mark_relayed(conn: psycopg.Connection, message_id: int) -> bool:
    """中継済みを記録する。未中継 → 中継済みへ実際に遷移させたときだけ True。

    条件付き UPDATE(``WHERE relayed_at IS NULL``)なので、既中継の行を渡しても
    False を返して二重中継にならない(0024 のトリガも同じ遷移だけを通す)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.dev_chat SET relayed_at = now()
            WHERE id = %s AND relayed_at IS NULL
            RETURNING id
            """,
            (message_id,),
        )
        return cur.fetchone() is not None


# ── Discord 中継 ──────────────────────────────────────────────────────────────
def relay_embed(message: DevChatMessage) -> dict[str, Any]:
    """中継 embed(純関数)。書式は ``🛠️【開発室】<本文>``。

    author(キャラクター)は載せない — 代表は ``config/org.yaml`` の members に
    いない(人間であり、台帳はモデル担当者の台帳)。接頭辞だけで発信元は判る。
    """
    body = message.body
    if len(body) > RELAY_BODY_LIMIT:
        body = body[:RELAY_BODY_LIMIT] + "…(以下略 — 全文はダッシュボードの開発室)"
    return {
        "description": f"{RELAY_PREFIX}{body}",
        "color": COLOR_NORMAL,
        "footer": {"text": f"開発室 #{message.id} / 代表 → 設計リード"},
    }


#: ``press.outbox`` への投入関数(テストはフェイクに差し替える)。
EnqueueFn = Callable[[psycopg.Connection, str, dict[str, Any], int], int]


def relay_pending(
    conn: psycopg.Connection,
    run_id: int,
    *,
    channel: str = RELAY_CHANNEL,
    enqueue: EnqueueFn | None = None,
    limit: int = 20,
) -> list[int]:
    """未中継の代表発言を outbox へ載せ、中継できた dev_chat id 一覧を返す。

    1 件ずつ ``conn.transaction()``(= SAVEPOINT)で囲み、**enqueue と relayed_at の
    更新を必ず同じ単位にする**。片方だけが残ると、Discord に出ないまま中継済みに
    なる(連絡が消える)か、同じ連絡が何度も流れる。

    失敗した件は ``relayed_at IS NULL`` のまま残して次回リトライする。**黙って
    飛ばさない** — 代表の連絡が届かない事象は運用者が気付ける必要があるため
    ``log.warning`` に理由を残す(独立役員審査 0020 C-2 と同じ考え方: 到達性を
    優先しつつ、沈黙はさせない)。

    commit は呼び出し側の責務(Bot の配送ループが 1 トランザクションで束ねる)。
    """
    send = enqueue if enqueue is not None else outbox.enqueue
    relayed: list[int] = []
    for message in claim_unrelayed(conn, limit=limit):
        try:
            with conn.transaction():
                send(conn, channel, relay_embed(message), run_id)
                if not mark_relayed(conn, message.id):
                    # 占有済みの行なので通常起きない。起きたなら並行更新であり、
                    # enqueue を巻き戻して二重配送を避ける。
                    raise RuntimeError(
                        f"dev_chat #{message.id} は既に中継済みだった(並行更新)"
                    )
        except Exception:  # noqa: BLE001 - 個別失敗は次回リトライ。理由は必ず残す
            log.warning(
                "開発室 #%s の Discord 中継に失敗した(未中継のまま次回リトライ)",
                message.id,
                exc_info=True,
            )
            continue
        relayed.append(message.id)
    return relayed


# ── CLI(設計リードの返信経路)──────────────────────────────────────────────
def _format_thread(messages: list[DevChatMessage]) -> str:
    labels = {REPRESENTATIVE: "代表", DESIGN_LEAD: "設計リード"}
    lines = []
    for m in messages:
        mark = "" if m.sender != REPRESENTATIVE else ("" if m.relayed else " [未中継]")
        lines.append(
            f"#{m.id} [{labels.get(m.sender, m.sender)}] "
            f"{m.created_at:%Y-%m-%d %H:%M}{mark}\n{m.body}"
        )
    return "\n\n".join(lines) if lines else "(発言なし)"


def main(argv: list[str] | None = None) -> int:
    """``python -m ryza.governance.devchat --reply "..."`` / ``--list``。

    ``RYZA_DATABASE_URL``(``ryza.db.conn``)が指す DB へ書く。VM でも手元でも同じ。
    ``--reply`` を省くと stdin を本文として読む(長文を here-doc で渡すため)。
    """
    parser = argparse.ArgumentParser(
        description="開発室(ops.dev_chat)へ設計リードとして返信する / スレッドを読む"
    )
    parser.add_argument("--reply", default=None, help="返信本文(省略時は stdin を読む)")
    parser.add_argument(
        "--list", type=int, nargs="?", const=20, default=None,
        metavar="N", help="直近 N 件のスレッドを表示して終了(既定 20)",
    )
    args = parser.parse_args(argv)

    from ryza.db.conn import connect  # 遅延 import(--help に DB 接続を要求しない)

    with connect() as conn:
        if args.list is not None:
            print(_format_thread(fetch_thread(conn, limit=args.list)))
            return 0
        body = args.reply if args.reply is not None else sys.stdin.read()
        message_id = post_design_lead(conn, body)
        conn.commit()
    print(f"posted dev_chat #{message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
