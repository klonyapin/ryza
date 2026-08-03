"""TDnet RSS 取込テスト（HTTP 全モック）。正常・重複・異常・URL 構築。"""

from __future__ import annotations

from datetime import date

import pytest

from ryza.ingest import tdnet

_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>7203 トヨタ 通期業績予想の修正</title>
    <link>https://www.release.tdnet.info/a.pdf</link>
    <guid>tdnet-0001</guid>
    <pubDate>Mon, 03 Aug 2026 15:00:00 +0900</pubDate>
  </item>
  <item>
    <title>6758 ソニー 決算短信</title>
    <link>https://www.release.tdnet.info/b.pdf</link>
    <guid>tdnet-0002</guid>
    <pubDate>Mon, 03 Aug 2026 15:05:00 +0900</pubDate>
  </item>
</channel></rss>""".encode()


def test_ingest_feed_ok(conn, run, store, fetcher):
    fetcher.add_bytes("tdnet", _RSS)
    res = tdnet.ingest_feed(conn, run, store, fetcher, feed_url="http://x/tdnet")
    assert res == {"written": 2, "total": 2}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM docs.documents "
            "WHERE source_name='TDnet' AND source_type='filing'"
        )
        assert cur.fetchone()[0] == 2


def test_ingest_feed_idempotent(conn, run, store, fetcher):
    fetcher.add_bytes("tdnet", _RSS)
    r1 = tdnet.ingest_feed(conn, run, store, fetcher, feed_url="http://x/tdnet")
    r2 = tdnet.ingest_feed(conn, run, store, fetcher, feed_url="http://x/tdnet")
    assert r1["written"] == 2
    assert r2["written"] == 0  # 同一開示は再取込で増えない
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.documents WHERE source_name='TDnet'")
        assert cur.fetchone()[0] == 2


def test_ingest_feed_http_error_raises(conn, run, store, fetcher):
    fetcher.add_status("tdnet", 503)
    with pytest.raises(RuntimeError):
        tdnet.ingest_feed(conn, run, store, fetcher, feed_url="http://x/tdnet")


def test_evidence_saved_for_entry(conn, run, store, fetcher):
    fetcher.add_bytes("tdnet", _RSS)
    tdnet.ingest_feed(conn, run, store, fetcher, feed_url="http://x/tdnet")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ledger.evidence WHERE kind='tdnet_rss'")
        assert cur.fetchone()[0] == 2


# ────────────────────────────────────────────────────────────────────────────
# フィード URL 構築（日付範囲指定。決算集中日 1000 件超/日の取りこぼし対策）
# ────────────────────────────────────────────────────────────────────────────
def test_feed_url_for_range():
    url = tdnet.feed_url_for(date(2026, 5, 12))
    assert url == "https://webapi.yanoshin.jp/webapi/tdnet/list/20260511-20260512.rss?limit=3000"


def test_feed_url_for_single_day():
    url = tdnet.feed_url_for(date(2026, 5, 12), lookback_days=0, limit=500)
    assert url == "https://webapi.yanoshin.jp/webapi/tdnet/list/20260512.rss?limit=500"


def test_resolve_feed_url_explicit_wins(monkeypatch):
    monkeypatch.setenv("RYZA_TDNET_FEED_URL", "http://env/feed.rss")
    assert tdnet.resolve_feed_url(feed_url="http://cli/feed.rss") == "http://cli/feed.rss"


def test_resolve_feed_url_env_over_default(monkeypatch):
    monkeypatch.setenv("RYZA_TDNET_FEED_URL", "http://env/feed.rss")
    assert tdnet.resolve_feed_url(date_str="2026-05-12") == "http://env/feed.rss"


def test_resolve_feed_url_default_is_date_range(monkeypatch):
    monkeypatch.delenv("RYZA_TDNET_FEED_URL", raising=False)
    url = tdnet.resolve_feed_url(date_str="2026-05-12")
    assert url == tdnet.feed_url_for(date(2026, 5, 12))
