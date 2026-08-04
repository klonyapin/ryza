"""preprocess.fundamentals — J-Quants 財務サマリの構造化数値化(T-029)。

`docs.documents`(source_name='J-Quants', meta.kind='financial_statement')に取り込まれた
J-Quants `/v2/fins/summary` の生 JSON(証憑 `ledger.evidence` kind='jquants_statement')を、
決定論・point-in-time・冪等に `market.indicators` へ昇格する。米国側(EDGAR companyfacts →
`market.indicators`)と対称な流儀:

- **series_code**: ``JQUANTS:{symbol}:{field}:{period_kind}:{basis}``
  - ``symbol``: 5 桁コード → 4 桁+``.T`` に正規化(``jquants._normalize_symbol``)
  - ``field``: 正規化名(``config/jquants_fields.yaml`` で定義。実績と予想は別 field)
  - ``period_kind``: 当期区分(1Q/2Q/3Q/4Q/5Q/FY — payload ``CurPerType`` から導出)。
    会社予想は「予想対象期」の区分(現行予想=FY / 翌期予想=FY_NEXT)
  - ``basis``: 連結/単体(payload ``DocType`` の中央要素から導出)
- **ts**: 当期または予想対象期の**期末日**(payload ``CurPerEn`` / ``CurFYEn`` / ``NxtFYEn``)
- **as_of**: **開示日時**(``DiscDate`` + ``DiscTime``、TZ=JST → UTC)。**開示時点以外を
  as_of にしてはならない** — point-in-time 原則(不変原則4)。決算は対象期と開示時点が
  大きくずれるため、この分離が look-ahead 混入を防ぐ

**冪等**: ``docs.documents.meta->>'fundamentals_version'`` を処理済みマーカーにする
(ルール改訂時は ``FUNDAMENTALS_VERSION`` を上げれば全件再処理)。書込側は
``base.write_indicator`` の revision 対応 upsert に乗る(同一 (series_code, ts) の別 value
は revision++ の追記で改定される — 訂正開示の既存規約)。

**fail-closed**: config に無いフィールドは書かない。config にあるが payload に欠測・
非数値のものはその項目だけ skip(エラーにしない)。skip 件数は返り値に集計して出す
(静かに欠けさせない — T-029 §1-3)。

**リネージ**: 昇格した indicator ごとに ``record(conn, run, [("indicators", ref)],
[("documents", doc_id)])`` を張る。producer_job / run_id / as_of は Run が刻む。

**非 LLM**: 数値の解釈・補完・推定を一切しない(不変原則1・7)。

実行: ``python -m ryza.preprocess.fundamentals [--limit N] [--backfill]``
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.jquants import SOURCE_NAME as JQUANTS_SOURCE_NAME
from ryza.ingest.jquants import _normalize_symbol
from ryza.provenance import EvidenceStore, Run, record, start_run

# 昇格ルール束のバージョン。config/コード改訂時に上げると全件再処理対象になる
# (docs.documents.meta->>'fundamentals_version' との一致判定)。
FUNDAMENTALS_VERSION = "1"

_PREFIX = "JQUANTS:"

# config/jquants_fields.yaml はリポジトリルート直下の config/ に置く。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "jquants_fields.yaml"

# 証憑の付き回し先(記録時に付けた kind)。ingest.jquants.ingest_statements 参照。
_EVIDENCE_KIND = "jquants_statement"


# ─── フィールドマッピング(config)─────────────────────────────────────────
@dataclass(frozen=True)
class FieldMap:
    """config/jquants_fields.yaml の 1 行:「実フィールド名 → 正規化名」。

    ``bucket`` は "actuals" / "forecasts_current" / "forecasts_next" のいずれか。
    バケットにより ts(期末)・period_kind の導出経路が変わる。
    """

    source: str          # payload のキー(例: "Sales", "FEPS")
    normalized: str      # series_code の field 要素(例: "NetSales", "FcstEarningsPerShare")
    bucket: str          # "actuals" / "forecasts_current" / "forecasts_next"


def load_field_maps(path: str | Path = _CONFIG_PATH) -> list[FieldMap]:
    """config/jquants_fields.yaml を読み FieldMap のリストを返す。

    bucket ごとに ts と period_kind の導出規則が変わるため bucket を保持したまま返す。
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[FieldMap] = []
    for bucket in ("actuals", "forecasts_current", "forecasts_next"):
        for entry in data.get(bucket, []) or []:
            out.append(
                FieldMap(
                    source=entry["source"],
                    normalized=entry["normalized"],
                    bucket=bucket,
                )
            )
    return out


# ─── payload → 期間区分・基準(basis)・期末日の導出 ─────────────────────────
def _period_kind_actual(payload: dict[str, Any]) -> str | None:
    """実績の当期区分。V2 の ``CurPerType`` をそのまま採用する(1Q/2Q/3Q/4Q/5Q/FY)。

    未知値・欠測は None を返して呼び出し側で skip する(fail-closed)。
    """
    v = payload.get("CurPerType")
    if v is None:
        return None
    v = str(v).strip()
    if v in ("1Q", "2Q", "3Q", "4Q", "5Q", "FY"):
        return v
    return None


def _basis_from_doctype(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """DocType から (basis, skip_reason) を返す。

    仕様書 https://jpx-jquants.com/en/spec/fin-summary/typeofdocument に列挙された
    DocType は ``<Period>_<Basis>_<Standard>`` の 3 要素構成。本関数は中央要素
    (連結/単体)を basis として返す。仕様上の派生形の扱い:

    - ``FYFinancialStatements_Consolidated_REIT`` /
      ``..._Consolidated_Foreign`` などの中央要素が ``Consolidated``/``NonConsolidated``
      であるものは通過して昇格される(basis が導出できるため書いてよい)。
    - 中央要素が ``Consolidated``/``NonConsolidated`` 以外(すなわち basis 未定)は
      skip_reason=``no_basis`` で返す。
    - DocType 自体が ``EarnForecastRevision`` / ``DividendForecastRevision`` 等の
      財務諸表本体でない開示(3 要素構成でない)は skip_reason=``not_statement``
      で返す(``no_basis`` と分離することで運用時の診断を分けやすくする)。

    返り値のいずれかは必ず None:
      - (basis, None): 正常導出
      - (None, "not_statement"): 財務諸表本体でない DocType
      - (None, "no_basis"): 財務諸表本体だが basis 未定
    """
    dt = payload.get("DocType")
    if not dt:
        return None, "not_statement"
    parts = str(dt).split("_")
    if len(parts) < 2:
        # 3 要素構成でない DocType(EarnForecastRevision 等)= 財務諸表本体ではない
        return None, "not_statement"
    b = parts[1]
    if b in ("Consolidated", "NonConsolidated"):
        return b, None
    return None, "no_basis"


def _parse_date(value: str | None) -> datetime | None:
    """ISO 日付を UTC 00:00 の timezone-aware datetime に変換する。欠測は None。

    J-Quants の日付フィールド(CurPerEn 等)は "YYYY-MM-DD"。tzinfo を UTC に固定するのは
    ``market.indicators.ts`` が timestamptz のため。時刻自体は期末の暦日を意味するので
    UTC 深夜として扱うのが EDGAR 側(``ingest.edgar._parse``)と対称。
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_as_of(payload: dict[str, Any]) -> datetime | None:
    """開示日時(DiscDate + DiscTime, JST)を UTC の datetime に変換する。

    ``DiscTime`` は "HH:MM:SS" 形式。欠測時は**翌日 00:00 JST(=開示日の JST 終端)**へ
    フォールバックする(保守側 — 開示より**前**に as_of を倒さない。不変原則4)。
    「00:00 JST」フォールバックは最大約 24 時間の look-ahead を許すため採らない
    (実開示は 15:00 JST 以降が典型だが、それを 00:00 JST に丸めると当日の判断で
    「開示前に知っていた」ことになる系列を作り得る)。開示日の JST 終端に倒せば、
    ・実行時点に落として as_of が再取り込みで動く問題は避けられ、
    ・かつ point-in-time の「開示以降にしか使えない」条件を厳格に満たす。
    """
    disc_date = payload.get("DiscDate")
    if not disc_date:
        return None
    from zoneinfo import ZoneInfo
    jst = ZoneInfo("Asia/Tokyo")
    disc_time = payload.get("DiscTime")
    if disc_time:
        try:
            naive = datetime.fromisoformat(f"{disc_date}T{disc_time}")
        except ValueError:
            return None
        return naive.replace(tzinfo=jst).astimezone(UTC)
    # DiscTime 欠測: 翌日 00:00 JST(=開示日の JST 終端)に倒す(look-ahead を防ぐ)。
    try:
        d = datetime.fromisoformat(str(disc_date))
    except ValueError:
        return None
    end_of_day_jst = (d + timedelta(days=1)).replace(tzinfo=jst)
    return end_of_day_jst.astimezone(UTC)


# ─── payload の抽出(bucket ごとに ts と period_kind を変える)────────────────
@dataclass(frozen=True)
class Extraction:
    """1 (field, ts, value) の抽出結果(未書込)。"""

    series_code: str
    ts: datetime
    value: float


def _num(payload: dict[str, Any], key: str) -> float | None:
    """payload の当該キーを float 化する。空文字・None・非数値・非有限は None(→ skip)。

    J-Quants の一部フィールドは "" を返す(該当項目なし)。fail-closed に None 化する。
    ``float("NaN")`` / ``float("Infinity")`` / ``float("1e999")`` などの非有限値は
    ``math.isfinite`` で拒否する(NaN は自己不等のため write_indicator の同値判定を
    すり抜けて revision を無限に進める素地になり、Infinity は numeric 列を汚す)。
    """
    v = payload.get(key)
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _extract(
    payload: dict[str, Any],
    field_maps: list[FieldMap],
    *,
    symbol: str,
) -> tuple[list[Extraction], dict[str, int]]:
    """payload から書込点のリストと skip 集計を作る。

    skip 集計のキー:
      - ``no_period_kind``: 実績で ``CurPerType`` が空/未知
      - ``not_statement``: DocType が財務諸表本体でない(EarnForecastRevision 等)
      - ``no_basis``: 財務諸表本体だが連結/単体(basis)を導出できない
      - ``no_end_date``: 期末日フィールドが空(実績 CurPerEn / 現行予想 CurFYEn / 翌期予想 NxtFYEn)
      - ``no_value``: 対象フィールド自体が欠測 or 非数値
    """
    skip: dict[str, int] = {
        "no_period_kind": 0, "not_statement": 0, "no_basis": 0,
        "no_end_date": 0, "no_value": 0,
    }
    out: list[Extraction] = []

    basis, basis_skip = _basis_from_doctype(payload)
    if basis is None:
        # 全 field が書けないので 1 回だけ加算して返す(bucket ごとの二重計上を避ける)。
        # skip_reason("not_statement" or "no_basis")で運用診断を分ける。
        assert basis_skip is not None
        skip[basis_skip] += 1
        return out, skip

    # 実績: period_kind = CurPerType、ts = CurPerEn
    period_actual = _period_kind_actual(payload)
    ts_actual = _parse_date(payload.get("CurPerEn"))
    # 現行予想: period_kind = "FY"(会計年度末予想)、ts = CurFYEn
    ts_fy_current = _parse_date(payload.get("CurFYEn"))
    # 翌期予想: period_kind = "FY_NEXT"、ts = NxtFYEn
    ts_fy_next = _parse_date(payload.get("NxtFYEn"))

    for fm in field_maps:
        if fm.bucket == "actuals":
            if period_actual is None:
                skip["no_period_kind"] += 1
                continue
            if ts_actual is None:
                skip["no_end_date"] += 1
                continue
            period_kind = period_actual
            ts = ts_actual
        elif fm.bucket == "forecasts_current":
            if ts_fy_current is None:
                skip["no_end_date"] += 1
                continue
            period_kind = "FY"
            ts = ts_fy_current
        elif fm.bucket == "forecasts_next":
            if ts_fy_next is None:
                skip["no_end_date"] += 1
                continue
            period_kind = "FY_NEXT"
            ts = ts_fy_next
        else:  # pragma: no cover - load_field_maps が bucket を固定するので到達しない
            continue

        val = _num(payload, fm.source)
        if val is None:
            skip["no_value"] += 1
            continue
        series_code = f"{_PREFIX}{symbol}:{fm.normalized}:{period_kind}:{basis}"
        out.append(Extraction(series_code=series_code, ts=ts, value=val))
    return out, skip


# ─── 対象文書の探索と証憑 payload の取得 ──────────────────────────────────
@dataclass(frozen=True)
class DocRef:
    """未処理の 1 文書(``docs.documents`` の必要列のみ)。"""

    doc_id: int
    meta: dict[str, Any]


def find_unprocessed(
    conn: psycopg.Connection,
    *,
    version: str = FUNDAMENTALS_VERSION,
    limit: int = 500,
) -> list[DocRef]:
    """現行バージョンで未処理の J-Quants 財務諸表文書を若い順に返す。

    ``meta.fundamentals_version`` が現行と異なる(未処理 = NULL を含む)行が対象。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_id, meta
            FROM docs.documents
            WHERE source_name = %s
              AND meta->>'kind' = 'financial_statement'
              AND meta->>'fundamentals_version' IS DISTINCT FROM %s
            ORDER BY doc_id ASC
            LIMIT %s
            """,
            (JQUANTS_SOURCE_NAME, version, limit),
        )
        rows = cur.fetchall()
    return [DocRef(doc_id=r[0], meta=r[1] or {}) for r in rows]


def _resolve_evidence_id(
    conn: psycopg.Connection, doc_id: int
) -> int | None:
    """文書にひもづく証憑 ID を返す(``meta.lineage_edges`` を辿る)。

    ingest.jquants → ingest.base.upsert_document が
    ``record(conn, run, [("documents", doc_id)], [("evidence", evidence_id)])`` を
    張っているため、``from_kind='documents' AND to_kind='evidence'`` で引ける。
    複数辺がある場合は最小 ID(最初に張られたもの)を採る — 通常 1 対 1。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_id::bigint FROM meta.lineage_edges
            WHERE from_kind = 'documents' AND from_id = %s
              AND to_kind = 'evidence'
            ORDER BY to_id::bigint ASC
            LIMIT 1
            """,
            (str(doc_id),),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def _load_payload(
    conn: psycopg.Connection, store: EvidenceStore, evidence_id: int
) -> dict[str, Any] | None:
    """証憑バイト列を JSON として読む。JSON でない場合は None。"""
    data = store.get(conn, evidence_id)
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# ─── 1 文書の昇格 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DocOutcome:
    """1 文書の昇格結果(テスト・集計用)。"""

    doc_id: int
    written: int
    total: int
    skip: dict[str, int]
    error: str | None = None


def _stamp_processed(
    conn: psycopg.Connection,
    doc_id: int,
    *,
    version: str,
    stats: dict[str, Any],
) -> None:
    """meta に処理済みマーカーを刻む(冪等キー + 集計)。

    ``fundamentals_version`` が「次に処理対象か」を決める。同じ version の再実行は
    ``find_unprocessed`` の SELECT で対象から外れるため、書込点は revision の同値
    判定で必ず 0 になる(base.write_indicator の既存規約)。
    """
    patch = {
        "fundamentals_version": version,
        "fundamentals_processed_at": datetime.now(UTC).isoformat(),
        "fundamentals_stats": stats,
    }
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE docs.documents "
            "SET meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb WHERE doc_id = %s",
            (Jsonb(patch), doc_id),
        )


def promote_document(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    doc: DocRef,
    *,
    field_maps: list[FieldMap],
    version: str = FUNDAMENTALS_VERSION,
) -> DocOutcome:
    """1 文書を market.indicators に昇格する(冪等・リネージ・fail-closed)。

    証憑が引けない/JSON が読めない/銘柄が特定できない等、payload に到達できない
    ケースは書込 0 のまま processed マーカーを刻む(次回以降の再処理対象から外す)。
    実行環境の不整合(証憑が壊れている等)を毎日繰り返しリトライしないためである。
    """
    evidence_id = _resolve_evidence_id(conn, doc.doc_id)
    if evidence_id is None:
        stats = {"written": 0, "total": 0, "error": "no_evidence"}
        _stamp_processed(conn, doc.doc_id, version=version, stats=stats)
        return DocOutcome(
            doc_id=doc.doc_id, written=0, total=0, skip={}, error="no_evidence"
        )
    payload = _load_payload(conn, store, evidence_id)
    if payload is None:
        stats = {"written": 0, "total": 0, "error": "payload_not_json"}
        _stamp_processed(conn, doc.doc_id, version=version, stats=stats)
        return DocOutcome(
            doc_id=doc.doc_id, written=0, total=0, skip={}, error="payload_not_json"
        )
    # symbol は取込時に meta へ入っている。fallback で payload["Code"] も見る
    # (どちらも取れなければ書けない)。
    symbol = doc.meta.get("symbol") or (
        _normalize_symbol(payload["Code"]) if payload.get("Code") else None
    )
    if not symbol:
        stats = {"written": 0, "total": 0, "error": "no_symbol"}
        _stamp_processed(conn, doc.doc_id, version=version, stats=stats)
        return DocOutcome(
            doc_id=doc.doc_id, written=0, total=0, skip={}, error="no_symbol"
        )
    as_of = _parse_as_of(payload)
    if as_of is None:
        stats = {"written": 0, "total": 0, "error": "no_disc_date"}
        _stamp_processed(conn, doc.doc_id, version=version, stats=stats)
        return DocOutcome(
            doc_id=doc.doc_id, written=0, total=0, skip={}, error="no_disc_date"
        )

    extractions, skip = _extract(payload, field_maps, symbol=symbol)
    written = 0
    for ex in extractions:
        if base.write_indicator(
            conn, run,
            series_code=ex.series_code, ts=ex.ts, value=ex.value, as_of=as_of,
        ):
            written += 1
            record(
                conn, run,
                [("indicators", base.indicator_ref(ex.series_code, ex.ts))],
                [("documents", doc.doc_id)],
            )
    stats = {
        "written": written,
        "total": len(extractions),
        "skip": skip,
    }
    _stamp_processed(conn, doc.doc_id, version=version, stats=stats)
    return DocOutcome(
        doc_id=doc.doc_id, written=written, total=len(extractions),
        skip=skip, error=None,
    )


# ─── 一括処理(日次 + バックフィル)────────────────────────────────────────
@dataclass
class RunResult:
    """1 回のジョブ実行結果。"""

    processed: int = 0            # 処理した文書数
    written: int = 0              # 書き込んだ indicator 点数
    total_extractions: int = 0    # 抽出できた候補点数(書込 revision 同値含む)
    skip: dict[str, int] = field(
        default_factory=lambda: {
            "no_period_kind": 0, "not_statement": 0, "no_basis": 0,
            "no_end_date": 0, "no_value": 0,
        }
    )
    errors: dict[str, int] = field(default_factory=dict)  # error kind → 件数


def run_promotion(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    *,
    version: str = FUNDAMENTALS_VERSION,
    limit: int = 500,
    field_maps: list[FieldMap] | None = None,
) -> RunResult:
    """未処理の J-Quants 財務諸表文書を全て昇格する。

    ``limit`` は 1 バッチの SELECT 上限(既定 500)。決算集中日(1000 件超/日)でも
    当日中に処理しきるため、**未処理が尽きるまでループ**する(冪等マーカで再取得は
    安全 — 処理済み文書は ``find_unprocessed`` の SELECT で除外される)。無限ループ
    防止のため、1 バッチで進捗ゼロ(processed 増分なし)なら打ち切る。
    """
    field_maps = field_maps if field_maps is not None else load_field_maps()
    result = RunResult()
    while True:
        docs = find_unprocessed(conn, version=version, limit=limit)
        if not docs:
            break
        batch_processed = 0
        for doc in docs:
            outcome = promote_document(
                conn, run, store, doc, field_maps=field_maps, version=version
            )
            result.processed += 1
            batch_processed += 1
            result.written += outcome.written
            result.total_extractions += outcome.total
            for k, v in outcome.skip.items():
                result.skip[k] = result.skip.get(k, 0) + v
            if outcome.error:
                result.errors[outcome.error] = result.errors.get(outcome.error, 0) + 1
        if batch_processed == 0:
            # 進捗ゼロ(全件が processed マーカーを刻めなかった)= 無限ループ回避で打ち切る
            break
    return result


# ─── CLI エントリポイント ─────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    """CLI: 未処理の J-Quants 財務諸表文書を昇格する。

    ``--backfill`` は上限を大きく取って全件処理する。``--limit`` は 1 回の処理上限
    (省略時 500)。DB 接続は autocommit(冪等な追記書込は逐次確定でよい — jquants.main
    と同じ)。
    """
    parser = argparse.ArgumentParser(description="J-Quants 財務サマリの構造化数値化 (T-029)")
    parser.add_argument(
        "--limit", type=int, default=500,
        help="1 回の処理上限(既定 500)。バックフィル時は --backfill を使う",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="上限を十分大きく取って全件処理する(冪等マーカで再実行は安全)",
    )
    args = parser.parse_args(argv)

    limit = 10**9 if args.backfill else args.limit

    store = base.default_store()
    run = start_run(
        "preprocess.fundamentals",
        {"version": FUNDAMENTALS_VERSION, "limit": limit, "backfill": args.backfill},
    )
    conn = connect(autocommit=True)
    try:
        result = run_promotion(conn, run, store, limit=limit)
        run.finish("success")
    except Exception:
        run.finish("failed")
        raise
    finally:
        conn.close()

    print(
        f"fundamentals: processed={result.processed} written={result.written} "
        f"extractions={result.total_extractions} skip={result.skip} "
        f"errors={result.errors}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
