"""ingest.fred — FRED（セントルイス連銀）マクロ統計系列の取込。

2026-08-03 追加（設計 20-research §2）。米・グローバルのマクロ統計（金利・物価・雇用等）を
FRED API から取得し ``market.indicators`` へ書き込む。対象系列は
``config/fred_series.yaml``（``active: true`` のみ）。

``series_code`` は FRED の series ID に接頭辞 ``FRED:`` を付す（例 ``FRED:DGS10``）。
統計改定は ``as_of``（発表時点）と ``revision`` で表現する。原文（API 生レスポンス）は
証憑ストアへ保存し、``indicators → evidence`` のリネージ辺を張る。

認証: 無料 API キー。Secret ``fred-api-key`` / 環境変数 ``FRED_API_KEY``。HTTP は
``Fetcher`` 越し（テストはモック）。

実行: ``python -m ryza.ingest.fred [--config PATH] [--series ID ...]``
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.base import Fetcher
from ryza.provenance import EvidenceStore, Run, record
from ryza.provenance import run as run_ctx

_API_BASE = "https://api.stlouisfed.org/fred"
SOURCE_NAME = "FRED"
_PREFIX = "FRED:"

# config/fred_series.yaml はリポジトリルート直下。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "fred_series.yaml"


class FredAuthError(RuntimeError):
    """API キー未設定。"""


def api_key() -> str:
    key = os.environ.get("RYZA_FRED_API_KEY") or os.environ.get("FRED_API_KEY")
    if not key:
        raise FredAuthError(
            "FRED API キー未設定（Secret 'fred-api-key' / env FRED_API_KEY）"
        )
    return key


@dataclass(frozen=True)
class Series:
    """取込対象系列 1 件。"""

    id: str
    active: bool = True


def load_series(path: str | Path = _CONFIG_PATH) -> list[Series]:
    """``fred_series.yaml`` を読み ``active: true`` の系列のみ返す。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[Series] = []
    for entry in data.get("series", []):
        s = Series(id=entry["id"], active=entry.get("active", True))
        if s.active:
            out.append(s)
    return out


def fetch_observations(
    fetcher: Fetcher, series_id: str, *, key: str, start: str | None = None
) -> dict[str, Any]:
    """系列の観測値を取得する（JSON 全体を返す＝証憑保存用に生も保持）。"""
    params = {
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
    }
    if start:
        params["observation_start"] = start
    resp = fetcher.fetch(f"{_API_BASE}/series/observations", params=params)
    if not resp.ok:
        raise RuntimeError(f"FRED observations 失敗（{series_id}）: status={resp.status}")
    return resp.json()


def _write_indicator(
    conn: psycopg.Connection,
    run: Run,
    *,
    series_code: str,
    ts: datetime,
    value: float,
    as_of: datetime,
) -> bool:
    """``market.indicators`` に 1 点書き込む。既存（同一 PK）は無視。新規なら True。

    改定対応: 同一 (series_code, ts) に別 value が来たら revision を進めて追記する
    （追記オンリー。既存 revision と同値なら何もしない）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT revision, value FROM market.indicators "
            "WHERE series_code = %s AND ts = %s ORDER BY revision DESC LIMIT 1",
            (series_code, ts),
        )
        row = cur.fetchone()
    revision = 0
    if row is not None:
        last_rev, last_val = row
        if float(last_val) == value:
            return False  # 同値の再取込 → 冪等スキップ
        revision = last_rev + 1
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.indicators (series_code, ts, value, revision, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (series_code, ts, revision) DO NOTHING
            """,
            (series_code, ts, value, revision, as_of, run.run_id),
        )
        return cur.rowcount == 1


def ingest_series(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    payload: dict[str, Any],
    *,
    series_id: str,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """FRED observations レスポンスを ``market.indicators`` へ取り込む。

    ``.``（欠測）は飛ばす。各書込点は生レスポンス（証憑）へのリネージ辺を張る。
    ``{'written', 'total'}``。
    """
    as_of = as_of or datetime.now(UTC)
    series_code = f"{_PREFIX}{series_id}"
    observations = payload.get("observations", [])

    evidence_id, _ = base.save_raw(
        conn, store, kind="fred_observations", payload=payload, source=SOURCE_NAME
    )

    written = 0
    for obs in observations:
        raw_value = obs.get("value")
        if raw_value in (None, ".", ""):
            continue
        try:
            value = float(raw_value)
            ts = datetime.fromisoformat(obs["date"]).replace(tzinfo=UTC)
        except (ValueError, KeyError):
            continue
        if _write_indicator(
            conn, run, series_code=series_code, ts=ts, value=value, as_of=as_of
        ):
            written += 1
            record(
                conn, run,
                [("indicators", f"{series_code}:{ts.isoformat()}")],
                [("evidence", evidence_id)],
            )
    return {"written": written, "total": len(observations)}


def ingest_all(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    series: list[Series],
    *,
    key: str | None = None,
    start: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """複数系列を取り込む。1 系列の失敗は握って他を継続する。"""
    as_of = as_of or datetime.now(UTC)
    key = key if key is not None else api_key()
    written = total = errors = 0
    for s in series:
        try:
            payload = fetch_observations(fetcher, s.id, key=key, start=start)
            r = ingest_series(conn, run, store, payload, series_id=s.id, as_of=as_of)
            written += r["written"]
            total += r["total"]
        except Exception:  # noqa: BLE001 - 1 系列障害で全体を止めない
            errors += 1
    return {"written": written, "total": total, "series": len(series), "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FRED マクロ統計取込")
    parser.add_argument("--config", default=str(_CONFIG_PATH))
    parser.add_argument("--series", action="append", help="特定 series ID のみ（複数可）")
    parser.add_argument("--start", default=None, help="observation_start YYYY-MM-DD")
    args = parser.parse_args(argv)

    series = load_series(args.config)
    if args.series:
        wanted = set(args.series)
        series = [s for s in series if s.id in wanted]

    store = base.default_store()
    fetcher = base.default_fetcher()
    conn = connect(autocommit=True)
    try:
        params = {"series": [s.id for s in series]}
        with run_ctx("ingest.fred", params, conn=conn) as r:
            result = ingest_all(conn, r, store, fetcher, series, start=args.start)
        print(f"fred: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
