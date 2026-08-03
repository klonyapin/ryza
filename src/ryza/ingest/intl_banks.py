"""ingest.intl_banks — 海外中銀・国際機関の統計 API（ECB / BOE / IMF）。

T-012 一括拡張バッチ（設計 20-research §2）。対象系列は ``config/intl_series.yaml``
（``active: true`` のみ）。3 プロバイダを 1 ジョブに束ね ``market.indicators`` へ
書き込む。系列コードは ``{ECB|BOE|IMF}:{系列 ID の '/' を '.' に置換}``。1 リクエストに
複数系列が含まれる場合のみ、系列次元の値をサフィックスに付けて分離する。

## 各プロバイダの API・利用規約メモ

- **ECB Data Portal API**（``https://data-api.ecb.europa.eu/service/data/{flow}/{key}``、
  SDMX-JSON）: 認証不要。フェアユース（大量並列・全量スクレイプ禁止）に従い
  系列単位の逐次取得のみ。https://data.ecb.europa.eu/help/api/overview
- **BOE IADB**（``…/boeapps/iadb/fromshowcolumns.asp``、CSV）: 認証不要・公式レート
  制限なし。日付は ``01 Jan 2020`` 形式・欠測は空欄
- **IMF SDMX_JSON**（``https://dataservices.imf.org/REST/SDMX_JSON.svc/CompactData/
  {dataset}/{key}``）: 認証不要だが**約 10 リクエスト/5 秒の制限**あり → 系列を絞り
  逐次取得のみ。要素 1 件のとき Series/Obs が配列でなく単一オブジェクトになる

観測期間は SDMX 表記（``2026`` / ``2026-06`` / ``2026-Q2`` / ``2026-S1`` /
``2026-W05`` / ``2026-06-15``）。四半期・半期は**期末月**の 1 日を ts とする
（e-Stat と同じ規約）。

HTTP は ``Fetcher`` 越し（テストはモック）。取得レスポンスは証憑ストアへ保存し、
各書込点から ``indicators → evidence`` のリネージ辺を張る。

実行: ``python -m ryza.ingest.intl_banks [--config PATH] [--series PATH ...]``
"""

from __future__ import annotations

import argparse
import csv
import io
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

_ECB_BASE = "https://data-api.ecb.europa.eu/service/data"
_BOE_BASE = "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
_IMF_BASE = "https://dataservices.imf.org/REST/SDMX_JSON.svc/CompactData"
SOURCE_NAME = "intl_banks"
PROVIDERS = ("ecb", "boe", "imf")

# config/intl_series.yaml はリポジトリルート直下。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "intl_series.yaml"


@dataclass(frozen=True)
class IntlSeries:
    """取込対象系列 1 件。``path`` はプロバイダ固有の系列指定:

    - ecb: ``{flow}/{key}``（例 ``EXR/D.USD.EUR.SP00.A``）
    - boe: IADB 系列コード（例 ``IUDBEDR``）
    - imf: ``{dataset}/{key}``（例 ``IFS/M.JP.PCPI_IX``）
    """

    provider: str
    path: str
    name: str = ""
    active: bool = True

    @property
    def code_base(self) -> str:
        """``market.indicators`` の系列コード接頭部（例 ``ECB:EXR.D.USD.EUR.SP00.A``）。"""
        return f"{self.provider.upper()}:{self.path.replace('/', '.')}"


def load_series(path: str | Path = _CONFIG_PATH) -> list[IntlSeries]:
    """``intl_series.yaml`` を読み ``active: true`` の系列のみ返す。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[IntlSeries] = []
    for entry in data.get("series", []):
        s = IntlSeries(
            provider=entry["provider"],
            path=entry["path"],
            name=entry.get("name", ""),
            active=entry.get("active", True),
        )
        if s.provider not in PROVIDERS:
            raise ValueError(f"未知のプロバイダ: {s.provider}")
        if s.active:
            out.append(s)
    return out


# ────────────────────────────────────────────────────────────────────────────
# 期間解析（SDMX 表記）
# ────────────────────────────────────────────────────────────────────────────
def parse_period(text: str) -> datetime | None:
    """SDMX の観測期間表記を UTC datetime に変換する（解釈不能なら None）。

    ``2026``→01-01 / ``2026-06``→06-01 / ``2026-Q2``→06-01（期末月）/
    ``2026-S1``→06-01 / ``2026-W05``→当該 ISO 週の月曜 / ``2026-06-15``→当日。
    """
    text = str(text).strip()
    try:
        if len(text) == 4 and text.isdigit():
            return datetime(int(text), 1, 1, tzinfo=UTC)
        if len(text) == 7 and text[4] == "-" and text[5:].isdigit():
            return datetime(int(text[:4]), int(text[5:]), 1, tzinfo=UTC)
        if len(text) == 7 and text[5] in "QqSsWw" or (len(text) == 8 and text[5] in "Ww"):
            year = int(text[:4])
            marker, num = text[5].upper(), int(text[6:])
            if marker == "Q" and 1 <= num <= 4:
                return datetime(year, num * 3, 1, tzinfo=UTC)
            if marker == "S" and 1 <= num <= 2:
                return datetime(year, num * 6, 1, tzinfo=UTC)
            if marker == "W":
                return datetime.fromisocalendar(year, num, 1).replace(tzinfo=UTC)
            return None
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None


# ────────────────────────────────────────────────────────────────────────────
# プロバイダ別 取得・解析 → (サフィックス, 期間, 値) の列
# ────────────────────────────────────────────────────────────────────────────
def fetch_ecb(fetcher: Fetcher, series: IntlSeries) -> dict[str, Any]:
    """ECB Data Portal から SDMX-JSON を取得する。"""
    flow, _, key = series.path.partition("/")
    resp = fetcher.fetch(f"{_ECB_BASE}/{flow}/{key}", params={"format": "jsondata"})
    if not resp.ok:
        raise RuntimeError(f"ECB 取得失敗（{series.path}）: status={resp.status}")
    return resp.json()


def parse_ecb(payload: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """SDMX-JSON を ``(サフィックス, 期間, 値)`` の列に展開する。

    サフィックスは系列次元の値 ID を ``.`` 連結したもの。系列が 1 本だけなら空
    （code_base 自体がキー全体を含むため重複させない）。
    """
    datasets = payload.get("dataSets", [])
    if not datasets:
        return []
    structure = payload.get("structure", {}).get("dimensions", {})
    obs_values = (structure.get("observation") or [{}])[0].get("values", [])
    series_dims = structure.get("series", [])
    series_map = datasets[0].get("series", {})
    multi = len(series_map) > 1

    out: list[tuple[str, str, Any]] = []
    for series_key, series_val in series_map.items():
        suffix = ""
        if multi:
            parts = []
            for dim, raw_idx in zip(series_dims, series_key.split(":"), strict=False):
                values = dim.get("values", [])
                idx = int(raw_idx)
                if idx < len(values):
                    parts.append(str(values[idx].get("id", raw_idx)))
            suffix = ".".join(parts)
        for obs_idx, obs in series_val.get("observations", {}).items():
            i = int(obs_idx)
            period = obs_values[i].get("id") if i < len(obs_values) else None
            value = obs[0] if isinstance(obs, list) and obs else None
            if period is not None:
                out.append((suffix, period, value))
    return out


def fetch_boe(fetcher: Fetcher, series: IntlSeries) -> bytes:
    """BOE IADB から CSV を取得する（全期間・系列コード指定）。"""
    resp = fetcher.fetch(
        _BOE_BASE,
        params={
            "csv.x": "yes",
            "Datefrom": "01/Jan/1990",
            "Dateto": "now",
            "SeriesCodes": series.path,
            "CSVF": "TN",       # 縦持ち（DATE, 系列値）
            "UsingCodes": "Y",
        },
    )
    if not resp.ok:
        raise RuntimeError(f"BOE 取得失敗（{series.path}）: status={resp.status}")
    return resp.body


def parse_boe(csv_bytes: bytes) -> list[tuple[str, str, Any]]:
    """IADB CSV（``DATE, <code>`` 縦持ち）を展開する。日付は ``01 Jan 2020`` 形式。"""
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r]
    out: list[tuple[str, str, Any]] = []
    for row in rows[1:]:  # 先頭はヘッダ（DATE, 系列コード）
        if len(row) < 2:
            continue
        try:
            day = datetime.strptime(row[0].strip(), "%d %b %Y")
        except ValueError:
            continue
        out.append(("", day.date().isoformat(), row[1].strip() or None))
    return out


def fetch_imf(fetcher: Fetcher, series: IntlSeries) -> dict[str, Any]:
    """IMF SDMX_JSON（CompactData）を取得する。"""
    dataset, _, key = series.path.partition("/")
    resp = fetcher.fetch(f"{_IMF_BASE}/{dataset}/{key}")
    if not resp.ok:
        raise RuntimeError(f"IMF 取得失敗（{series.path}）: status={resp.status}")
    return resp.json()


def _as_list(value: Any) -> list[Any]:
    """IMF JSON は要素 1 件のとき配列でなく単一オブジェクトを返すため正規化する。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_imf(payload: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """CompactData を展開する。系列が複数あるときは次元属性値をサフィックスにする。"""
    series_list = _as_list(
        payload.get("CompactData", {}).get("DataSet", {}).get("Series")
    )
    multi = len(series_list) > 1
    out: list[tuple[str, str, Any]] = []
    for series in series_list:
        suffix = ""
        if multi:
            dims = [
                str(v)
                for k, v in sorted(series.items())
                if k.startswith("@") and k != "@UNIT_MULT"
            ]
            suffix = ".".join(dims)
        for obs in _as_list(series.get("Obs")):
            period = obs.get("@TIME_PERIOD")
            value = obs.get("@OBS_VALUE")
            if period is not None:
                out.append((suffix, period, value))
    return out


# ────────────────────────────────────────────────────────────────────────────
# 書込（プロバイダ共通）
# ────────────────────────────────────────────────────────────────────────────
def ingest_points(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    points: list[tuple[str, str, Any]],
    *,
    code_base: str,
    raw_payload: bytes | dict[str, Any] | list[Any],
    evidence_kind: str,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """``(サフィックス, 期間, 値)`` の列を ``market.indicators`` へ取り込む。

    非数値・解釈不能な期間は飛ばす。``{'written', 'total'}``。
    """
    as_of = as_of or datetime.now(UTC)
    evidence_id, _ = base.save_raw(
        conn, store, kind=evidence_kind, payload=raw_payload, source=SOURCE_NAME
    )
    written = 0
    for suffix, period, raw_value in points:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        ts = parse_period(period)
        if ts is None:
            continue
        code = f"{code_base}:{suffix}" if suffix else code_base
        if base.write_indicator(
            conn, run, series_code=code, ts=ts, value=value, as_of=as_of
        ):
            written += 1
            record(
                conn, run,
                [("indicators", base.indicator_ref(code, ts))],
                [("evidence", evidence_id)],
            )
    return {"written": written, "total": len(points)}


def ingest_series(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    series: IntlSeries,
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """1 系列を取得・解析・書込する（プロバイダでディスパッチ）。"""
    if series.provider == "ecb":
        payload = fetch_ecb(fetcher, series)
        points = parse_ecb(payload)
        kind = "ecb_sdmx"
        raw: bytes | dict[str, Any] = payload
    elif series.provider == "boe":
        raw_bytes = fetch_boe(fetcher, series)
        points = parse_boe(raw_bytes)
        kind = "boe_iadb_csv"
        raw = raw_bytes
    elif series.provider == "imf":
        payload = fetch_imf(fetcher, series)
        points = parse_imf(payload)
        kind = "imf_sdmx"
        raw = payload
    else:  # pragma: no cover - load_series で検査済み
        raise ValueError(f"未知のプロバイダ: {series.provider}")
    return ingest_points(
        conn, run, store, points,
        code_base=series.code_base, raw_payload=raw,
        evidence_kind=kind, as_of=as_of,
    )


def ingest_all(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    series: list[IntlSeries],
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """複数系列を取り込む。1 系列の失敗は握って他を継続する。"""
    as_of = as_of or datetime.now(UTC)
    written = total = errors = 0
    for s in series:
        try:
            r = ingest_series(conn, run, store, fetcher, s, as_of=as_of)
            written += r["written"]
            total += r["total"]
        except Exception:  # noqa: BLE001 - 1 系列障害で全体を止めない
            errors += 1
    return {"written": written, "total": total, "series": len(series), "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="海外中銀・国際機関 統計取込")
    parser.add_argument("--config", default=str(_CONFIG_PATH))
    parser.add_argument("--series", action="append", help="特定 path のみ（複数可）")
    args = parser.parse_args(argv)

    series = load_series(args.config)
    if args.series:
        wanted = set(args.series)
        series = [s for s in series if s.path in wanted]

    store = base.default_store()
    fetcher = base.default_fetcher()
    conn = connect(autocommit=True)
    try:
        params = {"series": [f"{s.provider}:{s.path}" for s in series]}
        with run_ctx("ingest.intl_banks", params, conn=conn) as r:
            result = ingest_all(conn, r, store, fetcher, series)
        print(f"intl_banks: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
