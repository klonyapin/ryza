"""ingest.base — 取込パイプラインの共通基盤。

全ソース（J-Quants / TDnet / EDINET / ニュース RSS / 経済カレンダー）が共有する部品を
1 か所に集約する。設計原則（docs/design/10-data-accounting.md・20-research.md §2）:

- **Run 経由**: 書き込む全行に ``run_id`` を刻む（``ryza.provenance.runs``）。
- **as_of / ingested_at**: 情報を知り得た時点（as_of）を全行に持たせる。
- **証憑保存**: 原文は必ず証憑ストア（``ryza.provenance.evidence``）へ不変保存する。
- **content_hash 冪等**: 同一内容の再取込で行が増えない（``docs.documents`` は
  ``UNIQUE(source_name, content_hash)``、``market.bars`` は PK、
  ``market.calendar_events`` は ``UNIQUE NULLS NOT DISTINCT`` で担保）。
- **リネージ**: 生成行 → 入力（証憑）の辺を ``meta.lineage_edges`` に登録する。

## HTTP 抽象

実 API へのアクセスは ``Fetcher`` プロトコル越しに行う。本番は ``UrllibFetcher``
（標準ライブラリのみ・追加依存なし）、テストはフェイクを注入する。**取込コードは
``Fetcher`` にしか依存しないため、テストは HTTP を一切飛ばさずモックで完結する**
（受け入れ基準）。

DB 書き込みは渡された ``conn`` のトランザクションに参加し、本モジュールは commit しない
（provenance 各 API と同じ規約）。
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from ryza.provenance import EvidenceStore, LocalStorage, Run, record

# ────────────────────────────────────────────────────────────────────────────
# HTTP 抽象（テストはフェイクを注入）
# ────────────────────────────────────────────────────────────────────────────
_DEFAULT_TIMEOUT = 30
_USER_AGENT = "ryza-ingest/1.0 (+https://github.com/sukifura/ryza)"


@dataclass(frozen=True)
class FetchResult:
    """HTTP レスポンス（取込に必要な最小限）。"""

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """ボディを JSON として解釈する。"""
        return json.loads(self.body.decode("utf-8"))

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Fetcher(Protocol):
    """取込が依存する HTTP クライアントの最小インターフェース。"""

    def fetch(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        data: bytes | None = None,
    ) -> FetchResult: ...


class UrllibFetcher:
    """本番用 ``Fetcher``。標準ライブラリ ``urllib`` のみ（追加依存なし）。"""

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    def fetch(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        data: bytes | None = None,
    ) -> FetchResult:
        if params:
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(params)}"
        req_headers = {"User-Agent": _USER_AGENT}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                body = resp.read()
                return FetchResult(
                    status=resp.status,
                    body=body,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
        except urllib.error.HTTPError as exc:  # ステータス付きエラーも結果として返す
            return FetchResult(
                status=exc.code,
                body=exc.read() if exc.fp else b"",
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            )


# ────────────────────────────────────────────────────────────────────────────
# ハッシュ・証憑
# ────────────────────────────────────────────────────────────────────────────
def content_hash(payload: bytes | str) -> bytes:
    """重複排除・改竄検知用の sha256 ダイジェスト（bytes）を返す。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).digest()


def save_raw(
    conn: psycopg.Connection,
    store: EvidenceStore,
    *,
    kind: str,
    payload: bytes | dict[str, Any] | list[Any],
    source: str,
) -> tuple[int, str]:
    """原文を証憑ストアへ保存し ``(evidence_id, payload_ref)`` を返す。

    ``EvidenceStore`` が sha256 で重複排除するため、同一原文は 1 度しか保存されない。
    ``payload_ref`` は ``docs.documents.raw_ref`` に格納する原文 URI。
    """
    evidence_id = store.store(conn, kind, payload, source)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_ref FROM ledger.evidence WHERE evidence_id = %s",
            (evidence_id,),
        )
        payload_ref = cur.fetchone()[0]
    return evidence_id, payload_ref


# ────────────────────────────────────────────────────────────────────────────
# 文書取込（docs.documents）
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DocResult:
    """``upsert_document`` の結果。"""

    doc_id: int
    created: bool          # True=新規挿入 / False=既存（冪等ヒット）
    evidence_id: int | None  # 新規時のみ証憑を保存。既存時は None


def upsert_document(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    *,
    source_type: str,
    source_name: str,
    title: str | None = None,
    body: str | None = None,
    url: str | None = None,
    lang: str | None = None,
    published_at: datetime | None = None,
    as_of: datetime | None = None,
    meta: dict[str, Any] | None = None,
    raw_payload: bytes | dict[str, Any] | list[Any] | None = None,
    evidence_kind: str = "scrape",
    hash_source: bytes | str | None = None,
) -> DocResult:
    """``docs.documents`` に 1 件取り込む（冪等・証憑保存・リネージ込み）。

    冪等キーは ``(source_name, content_hash)``。既存があれば証憑を再保存せず既存
    ``doc_id`` を返す。新規時のみ原文を証憑ストアへ保存し、``documents → evidence`` の
    リネージ辺を張る。``hash_source`` 未指定時は ``body`` →（無ければ）``raw_payload``
    →（無ければ）``url``/``title`` から content_hash を計算する。
    """
    if hash_source is not None:
        digest = content_hash(hash_source)
    elif body:
        digest = content_hash(body)
    elif raw_payload is not None:
        raw_bytes = raw_payload if isinstance(raw_payload, bytes) else json.dumps(
            raw_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        digest = content_hash(raw_bytes)
    else:
        digest = content_hash(f"{url or ''}\n{title or ''}")

    # 冪等: 既存があれば証憑もリネージも触らず返す。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id FROM docs.documents WHERE source_name = %s AND content_hash = %s",
            (source_name, digest),
        )
        existing = cur.fetchone()
    if existing is not None:
        return DocResult(doc_id=existing[0], created=False, evidence_id=None)

    as_of = as_of or datetime.now(UTC)
    evidence_id: int | None = None
    raw_ref: str | None = None
    if raw_payload is not None:
        evidence_id, raw_ref = save_raw(
            conn, store, kind=evidence_kind, payload=raw_payload, source=source_name
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.documents
                (source_type, source_name, url, title, body, lang,
                 published_at, as_of, content_hash, raw_ref, meta, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_name, content_hash) DO NOTHING
            RETURNING doc_id
            """,
            (
                source_type, source_name, url, title, body, lang,
                published_at, as_of, digest, raw_ref,
                Jsonb(meta) if meta is not None else None, run.run_id,
            ),
        )
        row = cur.fetchone()

    if row is None:
        # 競合（並行取込）で他ジョブが先に挿入した。既存を引く。
        with conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id FROM docs.documents "
                "WHERE source_name = %s AND content_hash = %s",
                (source_name, digest),
            )
            return DocResult(doc_id=cur.fetchone()[0], created=False, evidence_id=None)

    doc_id = row[0]
    if evidence_id is not None:
        record(conn, run, [("documents", doc_id)], [("evidence", evidence_id)])
    return DocResult(doc_id=doc_id, created=True, evidence_id=evidence_id)


# ────────────────────────────────────────────────────────────────────────────
# 銘柄マスタ（market.instruments・SCD2 自動登録）
# ────────────────────────────────────────────────────────────────────────────
def resolve_instrument(
    conn: psycopg.Connection,
    symbol: str,
    *,
    asset_class: str = "equity",
    venue: str = "TSE",
    currency: str = "JPY",
    multiplier: float = 1,
    tick_size: float | None = None,
    margin_params: dict[str, Any] | None = None,
    as_of: datetime | None = None,
) -> int:
    """``symbol`` の現行 ``instrument_id`` を返す。無ければ SCD2 で新規登録する。

    現行版は ``valid_to IS NULL`` の行。存在しなければ ``valid_from=as_of``・
    ``valid_to=NULL`` で新規行を挿入して自動登録する（20-research §2 の要件:
    「market.instruments に無い銘柄は SCD2 で自動登録」）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_id FROM market.instruments "
            "WHERE symbol = %s AND valid_to IS NULL "
            "ORDER BY valid_from DESC LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
    if row is not None:
        return row[0]

    as_of = as_of or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instruments
                (symbol, asset_class, venue, currency, multiplier,
                 tick_size, margin_params, valid_from, valid_to)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)
            RETURNING instrument_id
            """,
            (
                symbol, asset_class, venue, currency, multiplier, tick_size,
                Jsonb(margin_params) if margin_params is not None else None, as_of,
            ),
        )
        return cur.fetchone()[0]


# ────────────────────────────────────────────────────────────────────────────
# 時系列バー（market.bars）
# ────────────────────────────────────────────────────────────────────────────
def bar_ref(instrument_id: int, timeframe: str, ts: datetime) -> str:
    """バー行のリネージ用合成 ID（bars は複合 PK で単一の代理キーを持たないため）。"""
    return f"{instrument_id}:{timeframe}:{ts.isoformat()}"


def write_bar(
    conn: psycopg.Connection,
    run: Run,
    *,
    instrument_id: int,
    ts: datetime,
    timeframe: str,
    open: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    volume: float | None,
    source: str,
    as_of: datetime | None = None,
) -> bool:
    """``market.bars`` に 1 本書き込む。既存（同一 PK）は無視。新規挿入なら True。

    冪等キーは PK ``(instrument_id, timeframe, ts, source, as_of)``。同一データの
    再取込は ``ON CONFLICT DO NOTHING`` で行が増えない。
    """
    as_of = as_of or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.bars
                (instrument_id, ts, timeframe, open, high, low, close,
                 volume, source, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                instrument_id, ts, timeframe, open, high, low, close,
                volume, source, as_of, run.run_id,
            ),
        )
        return cur.rowcount == 1


# ────────────────────────────────────────────────────────────────────────────
# 経済カレンダー（market.calendar_events）
# ────────────────────────────────────────────────────────────────────────────
def write_calendar_event(
    conn: psycopg.Connection,
    run: Run,
    *,
    event_type: str,
    title: str,
    scheduled_at: datetime,
    instrument_id: int | None = None,
    importance: int = 1,
    meta: dict[str, Any] | None = None,
    as_of: datetime | None = None,
) -> int | None:
    """``market.calendar_events`` に 1 件書き込む。冪等（重複は None を返す）。

    冪等キーは ``UNIQUE NULLS NOT DISTINCT (event_type, title, scheduled_at,
    instrument_id)``。新規挿入時のみ ``event_id`` を返し、重複時は None。
    """
    as_of = as_of or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.calendar_events
                (event_type, title, scheduled_at, instrument_id,
                 importance, meta, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING event_id
            """,
            (
                event_type, title, scheduled_at, instrument_id, importance,
                Jsonb(meta) if meta is not None else None, as_of, run.run_id,
            ),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def add_to_watchlist(
    conn: psycopg.Connection,
    *,
    instrument_id: int,
    added_by: str,
    reason: str | None = None,
) -> bool:
    """``market.watchlist`` に登録する。既存は無視（冪等）。新規なら True。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.watchlist (instrument_id, added_by, reason)
            VALUES (%s, %s, %s)
            ON CONFLICT (instrument_id, added_by) DO NOTHING
            """,
            (instrument_id, added_by, reason),
        )
        return cur.rowcount == 1


# ────────────────────────────────────────────────────────────────────────────
# RSS / Atom フィード解析（TDnet・ニュース RSS 共通）
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FeedItem:
    """RSS/Atom の 1 エントリ（取込に必要な最小限）。"""

    title: str | None
    link: str | None
    guid: str | None
    summary: str | None
    published_at: datetime | None
    raw: str  # このエントリの原文 XML 断片（証憑・content_hash 用）


_RSS_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",   # RFC822（RSS pubDate）
    "%a, %d %b %Y %H:%M:%S %Z",
)


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    for fmt in _RSS_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # ISO8601 / Atom（末尾 Z を +00:00 に）
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_bytes: bytes) -> list[FeedItem]:
    """RSS 2.0 / Atom を要素名で緩く解析し ``FeedItem`` のリストを返す。

    名前空間の有無に依存しない（``_localname`` でローカル名のみ見る）。TDnet の
    適時開示 RSS と汎用ニュース RSS の双方を扱えるようにしている。
    """
    root = ET.fromstring(xml_bytes)
    items: list[FeedItem] = []
    for elem in root.iter():
        if _localname(elem.tag) not in ("item", "entry"):
            continue
        title = link = guid = summary = pub = None
        for child in elem:
            name = _localname(child.tag)
            if name == "title":
                title = (child.text or "").strip() or None
            elif name == "link":
                # RSS は text、Atom は href 属性。
                link = (child.text or "").strip() or child.get("href")
            elif name in ("guid", "id"):
                guid = (child.text or "").strip() or None
            elif name in ("description", "summary", "content"):
                summary = (child.text or "").strip() or None
            elif name in ("pubDate", "published", "updated"):
                pub = _parse_date(child.text)
        items.append(
            FeedItem(
                title=title,
                link=link,
                guid=guid,
                summary=summary,
                published_at=pub,
                raw=ET.tostring(elem, encoding="unicode"),
            )
        )
    return items


# ────────────────────────────────────────────────────────────────────────────
# エントリポイント補助（Cloud Run Jobs / ローカル実行）
# ────────────────────────────────────────────────────────────────────────────
def default_store() -> EvidenceStore:
    """環境変数から証憑ストアを構築する。

    ``RYZA_EVIDENCE_BUCKET`` があれば GCS、無ければローカル
    （``RYZA_EVIDENCE_ROOT`` 既定 ``./_evidence``）。ローカルは開発・Cloud Run Jobs の
    デバッグ用。本番デプロイでは GCS を設定する。
    """
    bucket_name = os.environ.get("RYZA_EVIDENCE_BUCKET")
    if bucket_name:
        from google.cloud import storage  # 遅延 import（GCS 使用時のみ）

        from ryza.provenance import GcsStorage

        client = storage.Client()
        return EvidenceStore(GcsStorage(client.bucket(bucket_name), bucket_name))
    root = os.environ.get("RYZA_EVIDENCE_ROOT", "./_evidence")
    return EvidenceStore(LocalStorage(root))


def default_fetcher() -> Fetcher:
    """本番用 ``Fetcher``（``UrllibFetcher``）。"""
    return UrllibFetcher()
