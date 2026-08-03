"""webhooks — Webhook 方式配送の純ロジックと DB 記録のテスト(discord 非依存)。"""

from __future__ import annotations

from ryza.bot import COLOR_FLASH, webhooks


def _embed_with_author() -> dict:
    return {
        "title": "朝刊",
        "color": 0xDC2626,
        "author": {"name": "射命丸 文(報道部アナリスト)", "icon_url": "https://x/aya.jpg"},
        "fields": [],
    }


# ── payload_for(author → username/avatar_url 昇格)────────────────────────────
def test_payload_promotes_author_to_username():
    p = webhooks.payload_for(_embed_with_author())
    assert p.username == "射命丸 文(報道部アナリスト)"
    assert p.avatar_url == "https://x/aya.jpg"
    # 重複表示を避けるため embed 側の author は取り除く。
    assert "author" not in p.embed
    assert p.embed["title"] == "朝刊"


def test_payload_without_author_is_passthrough():
    embed = {"title": "起動通知", "color": 1}
    p = webhooks.payload_for(embed)
    assert p.username is None and p.avatar_url is None
    assert p.embed == embed


# ── build_post_body ──────────────────────────────────────────────────────────
def test_post_body_moves_mention_to_content_and_sets_urgent_color():
    embed = _embed_with_author()
    embed["content"] = "@here"
    body = webhooks.build_post_body(embed, urgent=True)
    assert body["content"] == "@here"
    assert body["username"] == "射命丸 文(報道部アナリスト)"
    assert body["avatar_url"] == "https://x/aya.jpg"
    (posted,) = body["embeds"]
    assert "content" not in posted and "author" not in posted
    assert posted["color"] == COLOR_FLASH  # urgent は Bot 投稿経路と同じく赤へ上書き


def test_post_body_plain():
    body = webhooks.build_post_body({"title": "t", "color": 2})
    assert body == {"embeds": [{"title": "t", "color": 2}]}


# ── DB 記録(ops.discord_webhooks・0017)─────────────────────────────────────
def test_record_and_resolve_webhook(conn):
    assert webhooks.resolve_webhook(conn, "press") is None
    webhooks.record_webhook(conn, "press", "111", "https://discord.com/api/webhooks/111/tok")
    assert (
        webhooks.resolve_webhook(conn, "press")
        == "https://discord.com/api/webhooks/111/tok"
    )
    # upsert: 再 ensure で URL が更新される。
    webhooks.record_webhook(conn, "press", "222", "https://discord.com/api/webhooks/222/tok")
    assert (
        webhooks.resolve_webhook(conn, "press")
        == "https://discord.com/api/webhooks/222/tok"
    )
