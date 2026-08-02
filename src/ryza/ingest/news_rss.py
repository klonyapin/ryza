"""ingest.news_rss — 汎用ニュース RSS 取込。

取込対象フィードは ``config/feeds.yaml``（``active: true`` のみ処理）。各エントリを
``docs.documents`` へ冪等取込し、原文（エントリ XML 断片）を証憑ストアへ保存する。
本文はフィードの description/summary の範囲に留める（リンク先本文の取得は robots を
尊重しつつ将来拡張。設計 20-research §2）。

RSS/Atom 解析は ``base.parse_feed``。冪等キーは ``source_name + guid/link/title``。

実行: ``python -m ryza.ingest.news_rss [--config PATH] [--feed NAME ...]``
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import yaml

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.base import Fetcher
from ryza.provenance import EvidenceStore, Run, run as run_ctx

# config/feeds.yaml はリポジトリルート直下。src/ryza/ingest/news_rss.py から 3 つ上。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "feeds.yaml"


@dataclass(frozen=True)
class Feed:
    """取込対象フィード 1 件。"""

    name: str
    url: str
    source_type: str = "news"
    active: bool = True


def load_feeds(path: str | Path = _CONFIG_PATH) -> list[Feed]:
    """``feeds.yaml`` を読み ``active: true`` のフィードのみ返す。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    feeds: list[Feed] = []
    for entry in data.get("feeds", []):
        feed = Feed(
            name=entry["name"],
            url=entry["url"],
            source_type=entry.get("source_type", "news"),
            active=entry.get("active", True),
        )
        if feed.active:
            feeds.append(feed)
    return feeds


def _entry_key(item: base.FeedItem) -> str:
    return item.guid or item.link or item.title or item.raw


def ingest_one(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    feed: Feed,
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """1 フィードを取得・解析し ``docs.documents`` へ冪等取込する。"""
    as_of = as_of or datetime.now(UTC)
    resp = fetcher.fetch(feed.url)
    if not resp.ok:
        raise RuntimeError(f"RSS 取得失敗（{feed.name}）: status={resp.status}")
    items = base.parse_feed(resp.body)
    written = 0
    for item in items:
        res = base.upsert_document(
            conn, run, store,
            source_type=feed.source_type, source_name=feed.name,
            title=item.title, body=item.summary, url=item.link,
            published_at=item.published_at, as_of=as_of,
            meta={"guid": item.guid, "feed": feed.name},
            raw_payload=item.raw.encode("utf-8"),
            evidence_kind="news_rss",
            hash_source=f"{feed.name}:{_entry_key(item)}",
        )
        if res.created:
            written += 1
    return {"written": written, "total": len(items)}


def ingest_all(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    feeds: list[Feed],
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """複数フィードを取り込む。1 フィードの失敗は握って他を継続する。"""
    as_of = as_of or datetime.now(UTC)
    written = total = errors = 0
    for feed in feeds:
        try:
            r = ingest_one(conn, run, store, fetcher, feed, as_of=as_of)
            written += r["written"]
            total += r["total"]
        except Exception:  # noqa: BLE001 - 1 ソース障害で全体を止めない
            errors += 1
    return {"written": written, "total": total, "feeds": len(feeds), "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汎用ニュース RSS 取込")
    parser.add_argument("--config", default=str(_CONFIG_PATH))
    parser.add_argument("--feed", action="append", help="特定フィード名のみ（複数可）")
    args = parser.parse_args(argv)

    feeds = load_feeds(args.config)
    if args.feed:
        feeds = [f for f in feeds if f.name in set(args.feed)]

    store = base.default_store()
    fetcher = base.default_fetcher()
    conn = connect(autocommit=True)
    try:
        with run_ctx("ingest.news_rss", {"feeds": [f.name for f in feeds]}, conn=conn) as r:
            result = ingest_all(conn, r, store, fetcher, feeds)
        print(f"news_rss: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
