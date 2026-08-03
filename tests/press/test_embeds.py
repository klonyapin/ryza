"""Discord embed 組立の単体テスト(30-press §6)。純関数。"""

from __future__ import annotations

from ryza.press import embeds
from ryza.press.images import ImageResult
from ryza.press.linter import Prediction, Sentence, Topic, TradeImplication


def _topic() -> Topic:
    return Topic(
        argument="きょうの相場。",
        sentences=[Sentence("半導体が上昇した。", 1, [10]), Sentence("……明日のことだよ。", 5, [])],
        trade_implication=TradeImplication(action="watch", target="日経平均", condition="上抜け"),
        title="半導体主導",
    )


def test_morning_embed_color_and_disclaimer():
    embed = embeds.build_morning_embed([_topic()])
    assert embed["color"] == embeds.COLOR_NORMAL == 0x5B54C7
    assert embeds.DISCLAIMER in embed["footer"]["text"]
    assert len(embed["fields"]) == 1


def test_morning_embed_includes_trade_implication_ja():
    embed = embeds.build_morning_embed([_topic()])
    value = embed["fields"][0]["value"]
    assert "取引への含意" in value
    assert "ウォッチ追加" in value  # action=watch の日本語表示
    assert "日経平均" in value


def test_morning_embed_image_credit_in_footer():
    img = ImageResult(url="https://x/y.png", artist="artist_a", source="safebooru")
    embed = embeds.build_morning_embed([_topic()], image=img)
    assert embed["image"]["url"] == "https://x/y.png"
    assert "artist_a" in embed["footer"]["text"]


def test_morning_embed_no_image_when_none():
    embed = embeds.build_morning_embed([_topic()], image=None)
    assert "image" not in embed


def test_morning_nav_provisional_marked():
    embed = embeds.build_morning_embed([_topic()], nav={"value": 10_000_000, "change_pct": 1.2,
                                                        "provisional": True})
    nav_field = [f for f in embed["fields"] if f["name"] == "ポートフォリオ概況"][0]
    assert "暫定" in nav_field["value"]
    assert "10,000,000" in nav_field["value"]


def test_flash_embed_is_red():
    embed = embeds.build_flash_embed(_topic())
    assert embed["color"] == embeds.COLOR_FLASH == 0xC24E3A


def test_flash_prediction_embed_shows_label():
    t = Topic(
        argument="予兆。",
        sentences=[Sentence("シグナルが揃った。", 1, [10]), Sentence("……続くよ。", 5, [])],
        prediction=Prediction(claim="円安継続", confidence=0.6, verify_by="2026-08-10T00:00:00Z"),
    )
    embed = embeds.build_flash_embed(t, is_prediction=True)
    assert "予測" in embed["title"]
    assert "確度" in embed["description"]
    assert "60%" in embed["description"]


def test_flash_embed_mention_and_image():
    img = ImageResult(url="https://x/lain.png", artist="a", source="safebooru")
    embed = embeds.build_flash_embed(_topic(), image=img, mention="@here")
    assert embed["content"] == "@here"
    assert embed["image"]["url"] == "https://x/lain.png"


def test_digest_embed_combines_topics():
    embed = embeds.build_digest_embed([_topic(), _topic(), _topic()])
    assert "まとめ速報" in embed["title"]
    assert "3件" in embed["title"]
    assert embed["color"] == embeds.COLOR_FLASH
