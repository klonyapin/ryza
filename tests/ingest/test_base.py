"""ingest.base の受け入れ基準テスト。

- upsert_document: 冪等（同一で行が増えない）・証憑保存・リネージ辺
- resolve_instrument: SCD2 自動登録
- write_bar: PK 冪等
- write_calendar_event: UNIQUE NULLS NOT DISTINCT 冪等
- parse_feed: RSS/Atom 双方
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ryza.ingest import base


def _count_docs(conn, source_name) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM docs.documents WHERE source_name = %s", (source_name,)
        )
        return cur.fetchone()[0]


def test_upsert_document_idempotent_with_evidence_and_lineage(conn, run, store):
    kwargs = dict(
        source_type="news", source_name="TEST_SRC",
        title="タイトル", body="本文テキスト", url="https://ex/1",
        raw_payload=b"<item>raw</item>", evidence_kind="test",
    )
    r1 = base.upsert_document(conn, run, store, **kwargs)
    assert r1.created is True
    assert r1.evidence_id is not None

    # 全書込行に run_id / as_of / raw_ref。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, as_of, raw_ref, content_hash FROM docs.documents "
            "WHERE doc_id = %s",
            (r1.doc_id,),
        )
        run_id, as_of, raw_ref, chash = cur.fetchone()
    assert run_id == run.run_id
    assert as_of is not None
    assert raw_ref is not None
    assert chash is not None

    # リネージ: documents → evidence が張られている。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='documents' AND from_id=%s AND to_kind='evidence'",
            (str(r1.doc_id),),
        )
        assert cur.fetchone()[0] == 1

    # 証憑は verify を通る。
    assert store.verify(conn, r1.evidence_id) is True

    # 再取込は冪等: 行が増えず同じ doc_id、created=False。
    r2 = base.upsert_document(conn, run, store, **kwargs)
    assert r2.created is False
    assert r2.doc_id == r1.doc_id
    assert r2.evidence_id is None
    assert _count_docs(conn, "TEST_SRC") == 1


def test_resolve_instrument_scd2_autoregister(conn):
    symbol = "9999.T"
    id1 = base.resolve_instrument(conn, symbol, asset_class="equity")
    id2 = base.resolve_instrument(conn, symbol, asset_class="equity")
    assert id1 == id2  # 2 度目は既存を返す（新規行を作らない）

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.instruments "
            "WHERE symbol=%s AND valid_to IS NULL",
            (symbol,),
        )
        assert cur.fetchone()[0] == 1


def test_write_bar_idempotent(conn, run):
    instrument_id = base.resolve_instrument(conn, "7203.T")
    ts = datetime.now(UTC).replace(microsecond=0)
    as_of = datetime.now(UTC)
    kw = dict(
        instrument_id=instrument_id, ts=ts, timeframe="1d",
        open=100.0, high=110.0, low=95.0, close=105.0, volume=1000.0,
        source="jquants", as_of=as_of,
    )
    assert base.write_bar(conn, run, **kw) is True
    assert base.write_bar(conn, run, **kw) is False  # 同一 PK は無視

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.bars WHERE instrument_id=%s AND source='jquants'",
            (instrument_id,),
        )
        assert cur.fetchone()[0] == 1


def test_write_calendar_event_idempotent_with_null_instrument(conn, run):
    sched = datetime(2026, 12, 18, 3, 0, tzinfo=UTC)
    kw = dict(
        event_type="policy", title="テスト会合",
        scheduled_at=sched, instrument_id=None, importance=3,
    )
    eid = base.write_calendar_event(conn, run, **kw)
    assert eid is not None
    # instrument_id NULL でも NULLS NOT DISTINCT で重複扱い → 2 度目は None。
    assert base.write_calendar_event(conn, run, **kw) is None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.calendar_events WHERE title='テスト会合'"
        )
        assert cur.fetchone()[0] == 1


def test_parse_feed_rss():
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>決算発表</title>
        <link>https://ex/a</link>
        <guid>guid-a</guid>
        <description>本文A</description>
        <pubDate>Mon, 03 Aug 2026 09:00:00 +0900</pubDate>
      </item>
      <item>
        <title>業績修正</title>
        <link>https://ex/b</link>
        <guid>guid-b</guid>
      </item>
    </channel></rss>""".encode()
    items = base.parse_feed(xml)
    assert len(items) == 2
    assert items[0].title == "決算発表"
    assert items[0].guid == "guid-a"
    assert items[0].published_at is not None


def test_parse_feed_atom():
    xml = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Policy statement</title>
        <link href="https://frb/x"/>
        <id>tag:frb,2026:x</id>
        <summary>summary text</summary>
        <updated>2026-08-03T13:00:00Z</updated>
      </entry>
    </feed>"""
    items = base.parse_feed(xml)  # ASCII のみ → bytes リテラルで可
    assert len(items) == 1
    assert items[0].title == "Policy statement"
    assert items[0].link == "https://frb/x"
    assert items[0].published_at == datetime(2026, 8, 3, 13, 0, tzinfo=UTC)


def test_content_hash_stable():
    assert base.content_hash("abc") == base.content_hash(b"abc")


def test_freshness_helper_age(conn, run, store):
    # upsert_document の as_of を過去に設定できることの確認（鮮度検査の前提）。
    past = datetime.now(UTC) - timedelta(hours=48)
    base.upsert_document(
        conn, run, store,
        source_type="filing", source_name="AGETEST",
        title="t", body="b", as_of=past,
        raw_payload=b"x", evidence_kind="test",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT max(as_of) FROM docs.documents WHERE source_name='AGETEST'")
        assert cur.fetchone()[0] == past
