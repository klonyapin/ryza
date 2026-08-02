"""ニュース RSS 取込テスト（HTTP 全モック）。設定読込・正常・重複・1 ソース障害の継続。"""

from __future__ import annotations

from ryza.ingest import news_rss
from ryza.ingest.news_rss import Feed

_ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Fed holds rates</title><link href="https://frb/1"/>
    <id>frb-1</id><updated>2026-08-03T18:00:00Z</updated></entry>
  <entry><title>Balance sheet update</title><link href="https://frb/2"/>
    <id>frb-2</id><updated>2026-08-03T18:05:00Z</updated></entry>
</feed>"""


def test_load_feeds_active_only():
    feeds = news_rss.load_feeds()
    names = {f.name for f in feeds}
    # 官公庁・中銀は確定枠（active）で必ず含まれる（§2）。
    assert {"日銀", "財務省", "金融庁", "FRB"} <= names


def test_ingest_one_ok(conn, run, store, fetcher):
    fetcher.add_bytes("frb", _ATOM)
    feed = Feed(name="FRB", url="http://x/frb", source_type="central_bank")
    res = news_rss.ingest_one(conn, run, store, fetcher, feed)
    assert res == {"written": 2, "total": 2}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type FROM docs.documents WHERE source_name='FRB' LIMIT 1"
        )
        assert cur.fetchone()[0] == "central_bank"


def test_ingest_idempotent(conn, run, store, fetcher):
    fetcher.add_bytes("frb", _ATOM)
    feed = Feed(name="FRB", url="http://x/frb", source_type="central_bank")
    news_rss.ingest_one(conn, run, store, fetcher, feed)
    r2 = news_rss.ingest_one(conn, run, store, fetcher, feed)
    assert r2["written"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.documents WHERE source_name='FRB'")
        assert cur.fetchone()[0] == 2


def test_ingest_all_continues_on_error(conn, run, store, fetcher):
    fetcher.add_bytes("frb", _ATOM)
    # boj は 503 を返す（未登録 → 404 でも RuntimeError）。それでも frb は取り込まれる。
    fetcher.add_status("boj", 503)
    feeds = [
        Feed(name="日銀", url="http://x/boj", source_type="gov"),
        Feed(name="FRB", url="http://x/frb", source_type="central_bank"),
    ]
    res = news_rss.ingest_all(conn, run, store, fetcher, feeds)
    assert res["errors"] == 1
    assert res["written"] == 2  # FRB 分は成功
