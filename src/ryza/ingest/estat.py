"""ingest.estat — e-Stat API 3.0（日本政府統計）。

T-012 一括拡張バッチ（設計 20-research §2）。対象統計表は ``config/estat_series.yaml``
（``active: true`` のみ）。``getStatsData`` で統計表の値を取得し ``market.indicators``
へ書き込む。系列コードは ``ESTAT:{statsDataId}:{分類事項の値...}``（時間・単位を除く
分類次元をキー名順に連結。1 統計表に複数系列が含まれるため名前空間はここで分離）。

## e-Stat API の仕様メモ（https://www.e-stat.go.jp/api/api-info/api-spec）

- 認証: **appId**（アプリケーション ID）。Secret ``estat-app-id`` / 環境変数
  ``RYZA_ESTAT_APP_ID``（Secret Manager 連携は運用基盤側で env に注入される想定）
- エラーは HTTP 200 のまま ``RESULT.STATUS``（0=正常）で返る → STATUS を必ず検査
- 大きい統計表は ``RESULT_INF.NEXT_KEY`` でページング（``startPosition`` に渡す）
- 明確なレート制限の公表は無いが、利用規約上の大量アクセス禁止に従い逐次取得のみ
  （並列取得しない）
- 時間コード（``@time``）は 10 桁 ``YYYY00MMMM``: 年次 ``2026000000`` /
  月次 ``2026000606``（6月）/ 四半期 ``2026000103``（1〜3月）。期末月の 1 日を ts とする

HTTP は ``Fetcher`` 越し（テストはモック）。取得レスポンス（ページ単位）は証憑ストアへ
保存し、各書込点から ``indicators → evidence`` のリネージ辺を張る。

実行: ``python -m ryza.ingest.estat [--config PATH] [--stats-data-id ID ...]``
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

_API_BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"
SOURCE_NAME = "e-Stat"
_PREFIX = "ESTAT:"

# config/estat_series.yaml はリポジトリルート直下。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "estat_series.yaml"


class EstatAuthError(RuntimeError):
    """appId 未設定。"""


def app_id() -> str:
    """appId を取得する（Secret 'estat-app-id' → env 注入・環境変数フォールバック）。"""
    key = os.environ.get("RYZA_ESTAT_APP_ID") or os.environ.get("ESTAT_APP_ID")
    if not key:
        raise EstatAuthError(
            "e-Stat appId 未設定（Secret 'estat-app-id' / env RYZA_ESTAT_APP_ID）"
        )
    return key


@dataclass(frozen=True)
class StatsTable:
    """取込対象の統計表 1 件。"""

    id: str              # statsDataId
    name: str = ""
    active: bool = True


def load_tables(path: str | Path = _CONFIG_PATH) -> list[StatsTable]:
    """``estat_series.yaml`` を読み ``active: true`` の統計表のみ返す。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[StatsTable] = []
    for entry in data.get("tables", []):
        t = StatsTable(
            id=str(entry["id"]),
            name=entry.get("name", ""),
            active=entry.get("active", True),
        )
        if t.active:
            out.append(t)
    return out


# ────────────────────────────────────────────────────────────────────────────
# 取得・解析
# ────────────────────────────────────────────────────────────────────────────
def fetch_stats_data(
    fetcher: Fetcher,
    stats_data_id: str,
    *,
    key: str,
    start_position: int | None = None,
) -> dict[str, Any]:
    """``getStatsData`` を 1 ページ取得する（STATUS 検査込み。JSON 全体を返す）。"""
    params = {
        "appId": key,
        "statsDataId": stats_data_id,
        "metaGetFlg": "N",   # メタ情報は不要（分類コードのみで系列を分離する）
        "cntGetFlg": "N",
    }
    if start_position is not None:
        params["startPosition"] = str(start_position)
    resp = fetcher.fetch(f"{_API_BASE}/getStatsData", params=params)
    if not resp.ok:
        raise RuntimeError(
            f"e-Stat getStatsData 失敗（{stats_data_id}）: status={resp.status}"
        )
    payload = resp.json()
    result = payload.get("GET_STATS_DATA", {}).get("RESULT", {})
    status = int(result.get("STATUS", -1))
    if status != 0:
        # e-Stat は HTTP 200 のままボディでエラーを返す（docstring 参照）。
        raise RuntimeError(
            f"e-Stat getStatsData エラー（{stats_data_id}）: "
            f"STATUS={status} {result.get('ERROR_MSG', '')}"
        )
    return payload


def next_key(payload: dict[str, Any]) -> int | None:
    """次ページの ``startPosition``（``NEXT_KEY``）を返す。最終ページなら None。"""
    result_inf = (
        payload.get("GET_STATS_DATA", {})
        .get("STATISTICAL_DATA", {})
        .get("RESULT_INF", {})
    )
    nk = result_inf.get("NEXT_KEY")
    return int(nk) if nk else None


def _as_list(value: Any) -> list[Any]:
    """e-Stat JSON は要素 1 件のとき配列でなく単一オブジェクトを返すため正規化する。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_time_code(code: str) -> datetime | None:
    """e-Stat 時間コード（``YYYY00MMMM``）を期末月 1 日の UTC datetime に変換する。

    年次 ``2026000000`` → 2026-01-01 / 月次 ``2026000606`` → 2026-06-01 /
    四半期 ``2026000103``（1〜3月）→ 2026-03-01（期末月）。解釈不能なら None。
    """
    code = str(code).strip()
    if len(code) < 4 or not code[:4].isdigit():
        return None
    year = int(code[:4])
    month = 1
    if len(code) == 10 and code[4:].isdigit():
        mm_end = int(code[8:10])
        mm_start = int(code[6:8])
        if 1 <= mm_end <= 12:
            month = mm_end
        elif 1 <= mm_start <= 12:
            month = mm_start
    try:
        return datetime(year, month, 1, tzinfo=UTC)
    except ValueError:
        return None


def series_code(stats_data_id: str, value_row: dict[str, Any]) -> str:
    """VALUE 1 行の系列コードを組み立てる。

    ``@time``（時間軸）と ``@unit``（単位表示）を除く全分類次元（``@tab`` /
    ``@cat01``… / ``@area`` 等）をキー名順に連結し、1 統計表内の系列を一意にする。
    """
    dims = [
        str(v)
        for k, v in sorted(value_row.items())
        if k.startswith("@") and k not in ("@time", "@unit")
    ]
    suffix = ":".join(dims)
    return f"{_PREFIX}{stats_data_id}" + (f":{suffix}" if suffix else "")


def ingest_payload(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    payload: dict[str, Any],
    *,
    stats_data_id: str,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """getStatsData 1 ページ分を ``market.indicators`` へ取り込む。

    非数値（``-`` ``…`` ``***`` 等の秘匿・欠測記号）は飛ばす。``{'written', 'total'}``。
    """
    as_of = as_of or datetime.now(UTC)
    values = _as_list(
        payload.get("GET_STATS_DATA", {})
        .get("STATISTICAL_DATA", {})
        .get("DATA_INF", {})
        .get("VALUE")
    )

    evidence_id, _ = base.save_raw(
        conn, store, kind="estat_stats_data", payload=payload, source=SOURCE_NAME
    )

    written = 0
    for row in values:
        try:
            value = float(row.get("$", ""))
        except (TypeError, ValueError):
            continue  # 秘匿・欠測記号
        ts = parse_time_code(row.get("@time", ""))
        if ts is None:
            continue
        code = series_code(stats_data_id, row)
        if base.write_indicator(
            conn, run, series_code=code, ts=ts, value=value, as_of=as_of
        ):
            written += 1
            record(
                conn, run,
                [("indicators", base.indicator_ref(code, ts))],
                [("evidence", evidence_id)],
            )
    return {"written": written, "total": len(values)}


def ingest_table(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    table: StatsTable,
    *,
    key: str,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """1 統計表を全ページ取り込む（NEXT_KEY を辿る）。"""
    as_of = as_of or datetime.now(UTC)
    written = total = 0
    start: int | None = None
    while True:
        payload = fetch_stats_data(
            fetcher, table.id, key=key, start_position=start
        )
        r = ingest_payload(
            conn, run, store, payload, stats_data_id=table.id, as_of=as_of
        )
        written += r["written"]
        total += r["total"]
        start = next_key(payload)
        if start is None:
            break
    return {"written": written, "total": total}


def ingest_all(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    tables: list[StatsTable],
    *,
    key: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """複数統計表を取り込む。1 表の失敗は握って他を継続する。"""
    as_of = as_of or datetime.now(UTC)
    key = key if key is not None else app_id()
    written = total = errors = 0
    for table in tables:
        try:
            r = ingest_table(conn, run, store, fetcher, table, key=key, as_of=as_of)
            written += r["written"]
            total += r["total"]
        except Exception:  # noqa: BLE001 - 1 表障害で全体を止めない
            errors += 1
    return {"written": written, "total": total, "tables": len(tables), "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="e-Stat 統計取込")
    parser.add_argument("--config", default=str(_CONFIG_PATH))
    parser.add_argument(
        "--stats-data-id", action="append", help="特定 statsDataId のみ（複数可）"
    )
    args = parser.parse_args(argv)

    tables = load_tables(args.config)
    if args.stats_data_id:
        wanted = set(args.stats_data_id)
        tables = [t for t in tables if t.id in wanted]

    store = base.default_store()
    fetcher = base.default_fetcher()
    conn = connect(autocommit=True)
    try:
        params = {"tables": [t.id for t in tables]}
        with run_ctx("ingest.estat", params, conn=conn) as r:
            result = ingest_all(conn, r, store, fetcher, tables)
        print(f"estat: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
