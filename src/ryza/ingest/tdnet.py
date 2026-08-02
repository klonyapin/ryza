"""ingest.tdnet — TDnet 適時開示 RSS の生取込。

決算・業績修正・材料等の適時開示 RSS を 5 分間隔でポーリングする用途。**本タスクでは
生取込のみ**（開示種別の辞書分類・銘柄タグ付けは階層0 前処理 T-010 の範囲）。各エントリを
``docs.documents``（source_type='filing', source_name='TDnet'）へ冪等取込し、原文（エントリ
XML 断片）を証憑ストアへ保存する。

RSS 解析は ``base.parse_feed``（RSS/Atom 共通）。冪等キーはエントリの guid → link →
title の優先で content_hash 化する。

実行: ``python -m ryza.ingest.tdnet [--feed-url URL]``
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime

import psycopg

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.base import Fetcher
from ryza.provenance import EvidenceStore, Run
from ryza.provenance import run as run_ctx

SOURCE_NAME = "TDnet"
# TDnet 適時開示情報の RSS。実 URL は運用時に環境変数で上書き可能にしておく。
DEFAULT_FEED_URL = os.environ.get(
    "RYZA_TDNET_FEED_URL", "https://www.release.tdnet.info/inbs/I_list_001_rss.xml"
)


def _entry_key(item: base.FeedItem) -> str:
    return item.guid or item.link or item.title or item.raw


def ingest_feed(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    *,
    feed_url: str = DEFAULT_FEED_URL,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """RSS を取得・解析し ``docs.documents`` へ冪等取込する。``{'written', 'total'}``。"""
    as_of = as_of or datetime.now(UTC)
    resp = fetcher.fetch(feed_url)
    if not resp.ok:
        raise RuntimeError(f"TDnet RSS 取得失敗: status={resp.status}")
    items = base.parse_feed(resp.body)

    written = 0
    for item in items:
        res = base.upsert_document(
            conn, run, store,
            source_type="filing", source_name=SOURCE_NAME,
            title=item.title, body=item.summary, url=item.link, lang="ja",
            published_at=item.published_at, as_of=as_of,
            meta={"guid": item.guid},
            raw_payload=item.raw.encode("utf-8"),
            evidence_kind="tdnet_rss",
            hash_source=f"{SOURCE_NAME}:{_entry_key(item)}",
        )
        if res.created:
            written += 1
    return {"written": written, "total": len(items)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TDnet 適時開示 RSS 取込")
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    args = parser.parse_args(argv)

    store = base.default_store()
    fetcher = base.default_fetcher()
    conn = connect(autocommit=True)
    try:
        with run_ctx("ingest.tdnet.rss", {"feed_url": args.feed_url}, conn=conn) as r:
            result = ingest_feed(conn, r, store, fetcher, feed_url=args.feed_url)
        print(f"tdnet rss: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
