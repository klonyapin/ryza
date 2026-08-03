"""bridge_send — 設計リード名義ブリッジの純ロジック(分割・embed 組立)のテスト。"""

from __future__ import annotations

from ryza import bridge_send, org


def test_split_chunks_respects_paragraphs():
    text = "A" * 3000 + "\n" + "B" * 3000
    chunks = bridge_send.split_chunks(text, limit=3800)
    assert chunks == ["A" * 3000, "B" * 3000]


def test_split_chunks_short_text_is_single():
    assert bridge_send.split_chunks("hello\nworld") == ["hello\nworld"]


def test_build_embeds_uses_dev_lead_identity():
    member = org.member_for_role(bridge_send.BRIDGE_ROLE)
    embeds = bridge_send.build_embeds("報告です", member, title="統合報告")
    assert len(embeds) == 1
    (embed,) = embeds
    assert embed["title"] == "統合報告"
    assert embed["author"]["name"] == member.display_name  # あおば(設計リード(開発部門))
    assert embed["author"]["icon_url"] == member.icon_url
    assert embed["color"] == member.color_int


def test_build_embeds_title_only_on_first_chunk():
    member = org.member_for_role(bridge_send.BRIDGE_ROLE)
    text = "A" * 3500 + "\n" + "B" * 3500
    embeds = bridge_send.build_embeds(text, member, title="長文")
    assert len(embeds) == 2
    assert embeds[0]["title"] == "長文"
    assert "title" not in embeds[1]
    assert all(e["author"]["name"] == member.display_name for e in embeds)


def test_load_env_missing_file(tmp_path):
    assert bridge_send.load_env(tmp_path / "nope.env") == {}


def test_load_env_parses_key_values(tmp_path):
    p = tmp_path / "discord_bot.env"
    p.write_text("# comment\nDISCORD_BOT_TOKEN=abc\nDISCORD_CHANNEL_ID= 123 \n")
    env = bridge_send.load_env(p)
    assert env == {"DISCORD_BOT_TOKEN": "abc", "DISCORD_CHANNEL_ID": "123"}
