"""ingest.edgar — SEC EDGAR（米企業開示・13F・XBRL companyfacts）。

T-012 一括拡張バッチ（設計 20-research §2）。対象 CIK は ``config/edgar.yaml``
（``active: true`` のみ）。2 系統を取り込む:

- **submissions**（``https://data.sec.gov/submissions/CIK##########.json``）
  … 直近提出書類のメタデータ → ``docs.documents``（source_type='filing',
  source_name='EDGAR'）。10-K/10-Q/8-K に加え **13F-HR（機関投資家保有報告）も
  同一経路**で入る（保有明細 XML の構造化パースは情報分析班の設計課題として別起票。
  ここではメタデータ+証憑の取込まで）。冪等キーは accessionNumber
- **companyfacts**（``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json``）
  … XBRL 財務ファクト → ``market.indicators``。系列は
  ``EDGAR:{CIK10}:{タクソノミ}:{タグ}:{単位}``。**SEC が正規化した frame 付き
  ファクトのみ**採用する（同一期末に FY/四半期の duration が重複するため。frame は
  SEC 側で期間正規化・重複排除済み）。``as_of`` は各ファクトの filed（提出日）＝
  その値を知り得た時点（point-in-time 原則）

## SEC アクセス規約（https://www.sec.gov/os/accessing-edgar-data）

- **リクエストは最大 10 req/s**。本ジョブは 1 CIK あたり 2 リクエストの日次バッチで
  上限に達しない（並列化する場合はスロットリング必須）
- **User-Agent に連絡先の申告が必須**（"Sample Company Name AdminContact@example.com"
  形式）。環境変数 ``RYZA_EDGAR_CONTACT`` で設定（未設定時はリポジトリ URL）
- 認証不要（API キーなし）

HTTP は ``Fetcher`` 越し（テストはモック）。

実行: ``python -m ryza.ingest.edgar [--config PATH] [--cik CIK ...] [--no-facts]``
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

_API_SUBMISSIONS = "https://data.sec.gov/submissions"
_API_COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts"
SOURCE_NAME = "EDGAR"
_PREFIX = "EDGAR:"

# config/edgar.yaml はリポジトリルート直下。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "edgar.yaml"

# companyfacts から indicators へ落とす既定タグ（us-gaap）。全タグは数百あるため
# 主要財務指標に絞る（追加は config ではなくコード側で管理: 系列定義の変更を diff に残す）。
DEFAULT_FACT_TAGS = (
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "NetIncomeLoss",
    "EarningsPerShareDiluted",
)


def _contact() -> str:
    """SEC 申告用の連絡先（env ``RYZA_EDGAR_CONTACT``。未設定時はリポジトリ URL）。"""
    return os.environ.get("RYZA_EDGAR_CONTACT", "https://github.com/sukifura/ryza")


def _headers() -> dict[str, str]:
    # SEC 規約: User-Agent での連絡先申告が必須（モジュール docstring 参照）。
    return {"User-Agent": f"ryza-ingest/1.0 ({_contact()})"}


def cik10(cik: str | int) -> str:
    """CIK を 10 桁ゼロ詰め文字列に正規化する（EDGAR の URL・系列コード規約）。"""
    return f"{int(str(cik).strip().lstrip('CIK') or 0):010d}"


@dataclass(frozen=True)
class Company:
    """取込対象 1 社（または 13F 提出者）。"""

    cik: str            # 10 桁ゼロ詰め
    name: str = ""
    facts: bool = True  # companyfacts も取るか（13F 専業提出者は False）
    active: bool = True


def load_companies(path: str | Path = _CONFIG_PATH) -> list[Company]:
    """``edgar.yaml`` を読み ``active: true`` の対象のみ返す。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[Company] = []
    for entry in data.get("companies", []):
        c = Company(
            cik=cik10(entry["cik"]),
            name=entry.get("name", ""),
            facts=entry.get("facts", True),
            active=entry.get("active", True),
        )
        if c.active:
            out.append(c)
    return out


# ────────────────────────────────────────────────────────────────────────────
# submissions → docs.documents
# ────────────────────────────────────────────────────────────────────────────
def fetch_submissions(fetcher: Fetcher, cik: str | int) -> dict[str, Any]:
    """提出書類一覧（submissions JSON）を取得する。"""
    c = cik10(cik)
    resp = fetcher.fetch(f"{_API_SUBMISSIONS}/CIK{c}.json", headers=_headers())
    if not resp.ok:
        raise RuntimeError(f"EDGAR submissions 失敗（CIK{c}）: status={resp.status}")
    return resp.json()


def _filing_url(cik: str, accession: str, primary_doc: str | None) -> str | None:
    """Archives の原文 URL を組み立てる（primary_doc 不明時は None）。"""
    if not primary_doc:
        return None
    acc = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{primary_doc}"


def ingest_submissions(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    payload: dict[str, Any],
    *,
    cik: str | int,
    forms: set[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """submissions レスポンスを ``docs.documents`` へ冪等取込する。

    EDGAR の recent は**列指向**（accessionNumber / form / filingDate … が並行配列）。
    ``forms`` 指定時はその様式のみ（例 ``{"13F-HR"}``）。冪等キーは accessionNumber。
    各文書の証憑は当該 filing 1 件分のメタデータ dict。``{'written', 'total'}``。
    """
    as_of = as_of or datetime.now(UTC)
    c = cik10(cik)
    entity = payload.get("name", "")
    recent = payload.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])

    def col(name: str, i: int) -> Any:
        values = recent.get(name, [])
        return values[i] if i < len(values) else None

    written = 0
    total = 0
    for i, accession in enumerate(accessions):
        form = col("form", i)
        if forms is not None and form not in forms:
            continue
        total += 1
        filing_date = col("filingDate", i)
        primary_doc = col("primaryDocument", i)
        published_at = None
        if filing_date:
            try:
                published_at = datetime.fromisoformat(filing_date).replace(tzinfo=UTC)
            except ValueError:
                published_at = None
        filing_meta = {
            "cik": c,
            "entityName": entity,
            "accessionNumber": accession,
            "form": form,
            "filingDate": filing_date,
            "primaryDocument": primary_doc,
            "primaryDocDescription": col("primaryDocDescription", i),
        }
        res = base.upsert_document(
            conn, run, store,
            source_type="filing", source_name=SOURCE_NAME,
            title=f"{entity} {form} ({filing_date})",
            body=col("primaryDocDescription", i),
            url=_filing_url(c, accession, primary_doc), lang="en",
            published_at=published_at, as_of=as_of,
            meta=filing_meta,
            raw_payload=filing_meta, evidence_kind="edgar_submission",
            hash_source=f"{SOURCE_NAME}:{accession}",
        )
        if res.created:
            written += 1
    return {"written": written, "total": total}


# ────────────────────────────────────────────────────────────────────────────
# companyfacts → market.indicators
# ────────────────────────────────────────────────────────────────────────────
def fetch_company_facts(fetcher: Fetcher, cik: str | int) -> dict[str, Any]:
    """XBRL companyfacts を取得する。"""
    c = cik10(cik)
    resp = fetcher.fetch(f"{_API_COMPANYFACTS}/CIK{c}.json", headers=_headers())
    if not resp.ok:
        raise RuntimeError(f"EDGAR companyfacts 失敗（CIK{c}）: status={resp.status}")
    return resp.json()


def ingest_company_facts(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    payload: dict[str, Any],
    *,
    cik: str | int,
    tags: tuple[str, ...] | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """companyfacts を ``market.indicators`` へ取り込む（frame 付きファクトのみ）。

    系列は ``EDGAR:{CIK10}:{タクソノミ}:{タグ}:{単位}``、``ts`` は期末（end）。
    ``as_of`` は各ファクトの filed（提出日）を優先し、無ければ引数の as_of。
    各書込点は生レスポンス（証憑）へのリネージ辺を張る。``{'written', 'total'}``。
    """
    fallback_as_of = as_of or datetime.now(UTC)
    c = cik10(cik)
    tags = tags if tags is not None else DEFAULT_FACT_TAGS

    evidence_id, _ = base.save_raw(
        conn, store, kind="edgar_companyfacts", payload=payload, source=SOURCE_NAME
    )

    written = 0
    total = 0
    for taxonomy, tag_map in payload.get("facts", {}).items():
        for tag, fact in tag_map.items():
            if tag not in tags:
                continue
            for unit, observations in fact.get("units", {}).items():
                series_code = f"{_PREFIX}{c}:{taxonomy}:{tag}:{unit}"
                for obs in observations:
                    # frame 無し（重複期間あり）の生ファクトは採用しない（docstring 参照）。
                    if "frame" not in obs:
                        continue
                    total += 1
                    try:
                        value = float(obs["val"])
                        ts = datetime.fromisoformat(obs["end"]).replace(tzinfo=UTC)
                    except (KeyError, TypeError, ValueError):
                        continue
                    point_as_of = fallback_as_of
                    filed = obs.get("filed")
                    if filed:
                        try:
                            point_as_of = datetime.fromisoformat(filed).replace(
                                tzinfo=UTC
                            )
                        except ValueError:
                            point_as_of = fallback_as_of
                    if base.write_indicator(
                        conn, run,
                        series_code=series_code, ts=ts,
                        value=value, as_of=point_as_of,
                    ):
                        written += 1
                        record(
                            conn, run,
                            [("indicators", base.indicator_ref(series_code, ts))],
                            [("evidence", evidence_id)],
                        )
    return {"written": written, "total": total}


# ────────────────────────────────────────────────────────────────────────────
# オーケストレーション + エントリポイント
# ────────────────────────────────────────────────────────────────────────────
def ingest_all(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    companies: list[Company],
    *,
    with_facts: bool = True,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """複数社を取り込む。1 社の失敗は握って他を継続する。"""
    as_of = as_of or datetime.now(UTC)
    docs_written = facts_written = errors = 0
    for company in companies:
        try:
            payload = fetch_submissions(fetcher, company.cik)
            r = ingest_submissions(conn, run, store, payload, cik=company.cik, as_of=as_of)
            docs_written += r["written"]
            if with_facts and company.facts:
                facts = fetch_company_facts(fetcher, company.cik)
                rf = ingest_company_facts(
                    conn, run, store, facts, cik=company.cik, as_of=as_of
                )
                facts_written += rf["written"]
        except Exception:  # noqa: BLE001 - 1 社障害で全体を止めない
            errors += 1
    return {
        "documents": docs_written,
        "indicators": facts_written,
        "companies": len(companies),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SEC EDGAR 取込")
    parser.add_argument("--config", default=str(_CONFIG_PATH))
    parser.add_argument("--cik", action="append", help="特定 CIK のみ（複数可）")
    parser.add_argument("--no-facts", action="store_true", help="companyfacts を省略")
    args = parser.parse_args(argv)

    companies = load_companies(args.config)
    if args.cik:
        wanted = {cik10(c) for c in args.cik}
        companies = [c for c in companies if c.cik in wanted]

    store = base.default_store()
    fetcher = base.default_fetcher()
    conn = connect(autocommit=True)
    try:
        params = {"ciks": [c.cik for c in companies]}
        with run_ctx("ingest.edgar", params, conn=conn) as r:
            result = ingest_all(
                conn, r, store, fetcher, companies, with_facts=not args.no_facts
            )
        print(f"edgar: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
