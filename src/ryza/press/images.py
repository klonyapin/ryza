"""images — 画像ボード（safebooru 等）のタグ検索（30-press §6）。

共通規則（§6）:
①``ai_generated`` 等の AI 生成系タグを除外条件に入れる（ユーザー指示: AI 生成画像は除外）
②再アップロードせず URL 参照 ③出典・アーティストを embed footer にクレジット
④取得失敗時は画像なし（None）で投稿 ⑤完全非公開サーバー限定運用。

**ネットワーク I/O は差し替え可能**（``HttpGetJson`` プロトコル）。実装は urllib で JSON を引くが、
テストはフェイクを注入して実 API を叩かない。クエリ組立・除外・選択・クレジット抽出は純関数。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

from ryza.press.config import ImagesConfig


@dataclass(frozen=True)
class ImageResult:
    """選択された 1 枚。URL 参照＋クレジット（§6）。"""

    url: str
    artist: str
    source: str
    tags: str = ""


class HttpGetJson(Protocol):
    """URL を GET して JSON（list/dict）を返す差し替え口。失敗時は例外。"""

    def __call__(self, url: str) -> Any: ...


def build_query_url(
    cfg: ImagesConfig, tags: list[str], *, limit: int = 50
) -> str:
    """safebooru dapi のクエリ URL を組む。AI 生成タグは ``-tag`` で除外（§6①）。"""
    positive = list(tags)
    negative = [f"-{t}" for t in cfg.exclude_tags]
    tag_expr = " ".join(positive + negative)
    params = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": str(limit),
        "tags": tag_expr,
    }
    return f"{cfg.base_url}?{urlencode(params)}"


def _post_is_ai(post: dict[str, Any], exclude_tags: tuple[str, ...]) -> bool:
    """念のため結果側でも AI 生成タグを二重チェック（API の -tag 漏れ対策）。"""
    tag_str = str(post.get("tags", "")).lower()
    tokens = set(tag_str.split())
    return any(t.lower() in tokens for t in exclude_tags)


def _to_result(post: dict[str, Any], source: str) -> ImageResult | None:
    """safebooru の 1 post を ImageResult に。URL 欠落なら None。"""
    url = post.get("file_url") or post.get("sample_url")
    if not url:
        directory = post.get("directory")
        image = post.get("image")
        if directory and image:
            url = f"https://safebooru.org/images/{directory}/{image}"
    if not url:
        return None
    artist = str(post.get("owner") or post.get("author") or "unknown")
    return ImageResult(url=str(url), artist=artist, source=source, tags=str(post.get("tags", "")))


def select_from_posts(
    posts: list[dict[str, Any]],
    cfg: ImagesConfig,
    *,
    source: str,
    rng: random.Random | None = None,
) -> ImageResult | None:
    """post リストから AI 生成を除外し 1 枚をランダム選択する（純関数）。"""
    rng = rng or random.Random()
    clean = [
        p for p in posts if isinstance(p, dict) and not _post_is_ai(p, cfg.exclude_tags)
    ]
    results = [r for p in clean if (r := _to_result(p, source)) is not None]
    if not results:
        return None
    return rng.choice(results)


def fetch_image(
    cfg: ImagesConfig,
    tags: list[str],
    *,
    http: HttpGetJson,
    source: str | None = None,
    rng: random.Random | None = None,
    limit: int = 50,
) -> ImageResult | None:
    """タグ検索して 1 枚返す。**取得失敗時は None**（§6④・例外を握る）。"""
    url = build_query_url(cfg, tags, limit=limit)
    try:
        payload = http(url)
    except Exception:  # noqa: BLE001 - 取得失敗は画像なしで続行（§6④）
        return None
    posts = payload if isinstance(payload, list) else payload.get("post") if isinstance(payload, dict) else None
    if not posts:
        return None
    return select_from_posts(list(posts), cfg, source=source or cfg.board, rng=rng)


def fetch_mascot(
    cfg: ImagesConfig, *, http: HttpGetJson, rng: random.Random | None = None
) -> ImageResult | None:
    """玲音の投稿画像（iwakura_lain 系タグ・§6）。"""
    tags = [cfg.mascot_tags[0]] if cfg.mascot_tags else []
    return fetch_image(cfg, tags, http=http, source=cfg.board, rng=rng)


def fetch_thumbnail(
    cfg: ImagesConfig, *, http: HttpGetJson, rng: random.Random | None = None
) -> ImageResult | None:
    """記事サムネイル（curated「かわいい任意のキャラクター」タグ・§6）。"""
    return fetch_image(cfg, list(cfg.thumbnail_tags), http=http, source=cfg.board, rng=rng)


def urllib_get_json(url: str) -> Any:
    """既定の実 HTTP 実装（urllib）。テストでは使わない（フェイクを注入する）。"""
    import json
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "ryza-press/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - 固定の画像ボード
        return json.loads(resp.read().decode("utf-8"))
