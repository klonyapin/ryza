"""画像ボードのタグ検索の単体テスト(30-press §6)。実 API を叩かない。"""

from __future__ import annotations

import random

from ryza.press.config import ImagesConfig
from ryza.press.images import (
    build_query_url,
    fetch_image,
    fetch_mascot,
    select_from_posts,
)


def _cfg() -> ImagesConfig:
    return ImagesConfig(
        base_url="https://safebooru.org/index.php",
        exclude_tags=("ai_generated", "stable_diffusion"),
        mascot_tags=("iwakura_lain",),
        thumbnail_tags=("1girl", "original"),
    )


def test_query_url_excludes_ai_tags():
    url = build_query_url(_cfg(), ["iwakura_lain"])
    assert "-ai_generated" in url
    assert "-stable_diffusion" in url
    assert "iwakura_lain" in url


def test_select_excludes_ai_generated_posts():
    posts = [
        {"file_url": "https://x/ai.png", "owner": "bot", "tags": "1girl ai_generated"},
        {"file_url": "https://x/ok.png", "owner": "human", "tags": "1girl original"},
    ]
    res = select_from_posts(posts, _cfg(), source="safebooru", rng=random.Random(0))
    assert res is not None
    assert res.url == "https://x/ok.png"  # AI 生成は除外
    assert res.artist == "human"


def test_select_returns_none_when_all_ai():
    posts = [{"file_url": "https://x/ai.png", "owner": "bot", "tags": "ai_generated"}]
    assert select_from_posts(posts, _cfg(), source="safebooru") is None


def test_select_builds_url_from_directory_image():
    posts = [{"directory": "12", "image": "abc.png", "owner": "human", "tags": "1girl"}]
    res = select_from_posts(posts, _cfg(), source="safebooru", rng=random.Random(0))
    assert res is not None
    assert res.url.endswith("/images/12/abc.png")


def test_fetch_image_failure_returns_none():
    def failing(url: str):
        raise OSError("network down")

    assert fetch_image(_cfg(), ["iwakura_lain"], http=failing) is None


def test_fetch_image_empty_returns_none():
    assert fetch_image(_cfg(), ["x"], http=lambda url: []) is None


def test_fetch_mascot_credits_artist():
    posts = [{"file_url": "https://x/lain.png", "owner": "artist_a", "tags": "iwakura_lain"}]
    res = fetch_mascot(_cfg(), http=lambda url: posts, rng=random.Random(0))
    assert res is not None
    assert res.artist == "artist_a"
    assert res.source == "safebooru"


def test_fetch_image_handles_dict_payload():
    payload = {"post": [{"file_url": "https://x/ok.png", "owner": "human", "tags": "1girl"}]}
    res = fetch_image(_cfg(), ["1girl"], http=lambda url: payload, rng=random.Random(0))
    assert res is not None
    assert res.url == "https://x/ok.png"
