"""ingest.tdnet — TDnet 適時開示 RSS の生取込。

決算・業績修正・材料等の適時開示 RSS の取込。**本タスクでは生取込のみ**（開示種別の
辞書分類・銘柄タグ付けは階層0 前処理 T-010 の範囲）。各エントリを
``docs.documents``（source_type='filing', source_name='TDnet'）へ冪等取込し、原文（エントリ
XML 断片）を証憑ストアへ保存する。

RSS 解析は ``base.parse_feed``（RSS/Atom 共通）。冪等キーはエントリの guid → link →
title の優先で content_hash 化する。

既定のフィード URL は **日付範囲指定**（対象日から ``--lookback-days`` 日前まで、
``list/YYYYMMDD-YYYYMMDD.rss``）。決算集中日は 1 日 1000 件超の開示があり、直近 N 件を
返す ``recent.rss`` では日次実行で取りこぼすため（検証: 2026-05-12 は 1146 件）。
5 分間隔ポーリングへ移行する場合も、冪等取込のため同じ CLI をそのまま高頻度実行してよい
（その場合は ``--feed-url``/env で ``recent.rss`` を指定した方が転送量が小さい）。

実行: ``python -m ryza.ingest.tdnet [--date YYYY-MM-DD] [--lookback-days N]
[--limit N] [--feed-url URL]``
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.base import Fetcher
from ryza.provenance import EvidenceStore, Run
from ryza.provenance import run as run_ctx

SOURCE_NAME = "TDnet"
JST = ZoneInfo("Asia/Tokyo")

# TDnet 公式(release.tdnet.info)は HTML 閲覧のみで無料の RSS/API を提供していない
# (公式 TDnet API と J-Quants の TDnet アドオンはいずれも有料)。ここでは準公式の
# 「東証TDnet WEB-API by やのしん」(https://webapi.yanoshin.jp/tdnet/、公式 TDnet を
# 数分間隔で同期)を既定とする(Issue #29)。個人運営サービスのため、停止時は
# RYZA_TDNET_FEED_URL で代替 URL に切り替えるか、有料 API への移行を検討する。
API_BASE = "https://webapi.yanoshin.jp/webapi/tdnet"

# 1 リクエストの最大取得件数。API 既定は 300 件・決算集中日は 1 日 1000 件超のため、
# 2 日分(範囲取得)でも全件収まる値にする(検証: limit=3000 で 2 日分 1851 件を一括返却)。
DEFAULT_LIMIT = 3000

# 対象日から何日さかのぼるか。日次実行(09:00 JST)では前日 15〜17 時の開示集中帯が
# 朝刊の主材料になるため、既定で前日を含める(冪等取込のため重複取得は無害)。
DEFAULT_LOOKBACK_DAYS = 1


def feed_url_for(
    target_date: date,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """対象日(+ルックバック)の全開示を返す日付範囲 RSS の URL を組む。"""
    start = target_date - timedelta(days=lookback_days)
    cond = (
        f"{start:%Y%m%d}-{target_date:%Y%m%d}"
        if start != target_date
        else f"{target_date:%Y%m%d}"
    )
    return f"{API_BASE}/list/{cond}.rss?limit={limit}"


def resolve_feed_url(
    *,
    feed_url: str | None = None,
    date_str: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """フィード URL を決める。優先度: ``--feed-url`` > env > 日付範囲の既定 URL。"""
    if feed_url:
        return feed_url
    env_url = os.environ.get("RYZA_TDNET_FEED_URL")
    if env_url:
        return env_url
    target = (
        date.fromisoformat(date_str) if date_str else datetime.now(JST).date()
    )
    return feed_url_for(target, lookback_days=lookback_days, limit=limit)


def _entry_key(item: base.FeedItem) -> str:
    return item.guid or item.link or item.title or item.raw


def ingest_feed(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    *,
    feed_url: str,
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
    parser.add_argument(
        "--date", default=None, help="対象日(JST, YYYY-MM-DD)。既定は今日"
    )
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help=f"対象日から何日さかのぼるか(既定 {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"最大取得件数(既定 {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--feed-url", default=None,
        help="フィード URL の明示指定(日付指定・env より優先)",
    )
    args = parser.parse_args(argv)
    feed_url = resolve_feed_url(
        feed_url=args.feed_url, date_str=args.date,
        lookback_days=args.lookback_days, limit=args.limit,
    )

    store = base.default_store()
    fetcher = base.default_fetcher()
    conn = connect(autocommit=True)
    try:
        with run_ctx("ingest.tdnet.rss", {"feed_url": feed_url}, conn=conn) as r:
            result = ingest_feed(conn, r, store, fetcher, feed_url=feed_url)
        print(f"tdnet rss: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
