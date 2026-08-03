"""webhooks — Webhook 方式の発信者表示配送(代表指示 2026-08-03)。

Discord Bot の通常投稿は Bot 自身の名前・アイコンでしか表示できないが、**Webhook は
投稿ごとに username / avatar_url を自由に設定できる**。そこで配送は次の方式とする:

1. Bot が起動時に各チャンネルへ webhook(名前 ``ryza-org``)を ensure し
   (Manage Webhooks 権限)、URL を ``ops.discord_webhooks`` に記録する
   (``ops.discord_channels`` と同じ流儀。実 I/O は ``main`` の担当)
2. 配送時は embed_json の author(発信者キャラクター — ``config/org.yaml`` 由来)を
   webhook の username=「名前(役職)」/ avatar_url に**昇格**させて投稿する
3. webhook を確保できないチャンネル(権限不足等)は従来の Bot 投稿
   (embed author 方式)へ自動フォールバックする(``main`` 側で分岐)

本モジュールは discord.py に依存しない。投稿(``post``)は webhook URL への
REST POST(stdlib urllib)で、``payload_for`` / ``build_post_body`` は純関数として
テストする。承認ボタン付きメッセージ(ApprovalView)は webhook では送れない
(incoming webhook にコンポーネントは付けられない)ため、``main`` は #承認 の
提案 embed を常に Bot 投稿経路に残す。
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any

import psycopg

from ryza.bot import COLOR_FLASH

# ensure する webhook の表示名(全チャンネル共通)。
WEBHOOK_NAME = "ryza-org"


@dataclass(frozen=True)
class WebhookPayload:
    """webhook 投稿 1 件分の表示者と embed。"""

    username: str | None
    avatar_url: str | None
    embed: dict[str, Any]


def payload_for(embed: dict[str, Any]) -> WebhookPayload:
    """embed_json から webhook の表示者(username / avatar_url)を導く。

    author(org.yaml 由来)があれば webhook の表示者へ昇格させ、embed 側の author は
    重複表示を避けるため取り除く。author が無ければ素の embed のまま
    (username=None → webhook の既定名で投稿)。
    """
    author = embed.get("author") or {}
    name = author.get("name")
    if not name:
        return WebhookPayload(None, None, embed)
    rest = {k: v for k, v in embed.items() if k != "author"}
    return WebhookPayload(str(name), author.get("icon_url"), rest)


def build_post_body(embed: dict[str, Any], *, urgent: bool = False) -> dict[str, Any]:
    """webhook Execute API のリクエスト body を組む(純関数)。

    - ``content`` キー(速報の @メンション — embeds.build_flash_embed)はメッセージ
      本文へ移す
    - ``urgent`` は Bot 投稿経路(``main.dict_to_embed`` 後の上書き)と同じく赤へ上書き
    """
    payload = payload_for(embed)
    body_embed = dict(payload.embed)
    content = body_embed.pop("content", None)
    if urgent:
        body_embed["color"] = COLOR_FLASH
    body: dict[str, Any] = {"embeds": [body_embed]}
    if content:
        body["content"] = str(content)
    if payload.username:
        body["username"] = payload.username
    if payload.avatar_url:
        body["avatar_url"] = payload.avatar_url
    return body


def post(webhook_url: str, embed: dict[str, Any], *, urgent: bool = False) -> str:
    """webhook へ 1 件投稿し Discord メッセージ ID を返す(失敗時は例外 → outbox 再送)。

    ``?wait=true`` でメッセージ本体を受け取り、``outbox.mark_sent`` に渡す ID を得る。
    """
    data = json.dumps(build_post_body(embed, urgent=urgent)).encode("utf-8")
    req = urllib.request.Request(
        f"{webhook_url}?wait=true",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "RyzaBot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - Discord API 固定
        message = json.load(resp)
    return str(message["id"])


# ── DB 記録(ops.discord_webhooks — channels.py と同じ流儀)───────────────────
def record_webhook(
    conn: psycopg.Connection, logical: str, webhook_id: str, webhook_url: str
) -> None:
    """ensure した webhook を記録する(呼び出し側が commit)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.discord_webhooks (logical, webhook_id, webhook_url, resolved_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (logical) DO UPDATE
            SET webhook_id = EXCLUDED.webhook_id,
                webhook_url = EXCLUDED.webhook_url,
                resolved_at = now()
            """,
            (logical, str(webhook_id), webhook_url),
        )


def resolve_webhook(conn: psycopg.Connection, logical: str) -> str | None:
    """論理チャンネルの webhook URL。未確保(権限不足等)なら None → Bot 投稿へ。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT webhook_url FROM ops.discord_webhooks WHERE logical = %s", (logical,)
        )
        row = cur.fetchone()
        return row[0] if row else None


__all__ = [
    "WEBHOOK_NAME",
    "WebhookPayload",
    "build_post_body",
    "payload_for",
    "post",
    "record_webhook",
    "resolve_webhook",
]
