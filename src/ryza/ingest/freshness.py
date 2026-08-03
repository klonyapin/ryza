"""ingest.freshness — ソース別 鮮度 SLA 検査。

各取込ソースに鮮度 SLA（最終取込からの許容経過時間）を定義し、超過（または一度も
取込が無い）を検知したら ``press.outbox``（#運営 = channel 'ops'）へ警告 embed を投入する。
監査 A-14（ソース停止検知）の常時部分（設計 20-research §2）。

**独立性**: Discord Bot（``ryza.bot``）には依存せず、``press.outbox`` へ直接 INSERT する
（配送は Bot 側ポーラーが sent_at 冪等で行う）。

各ソースの「最終取込時点」は as_of の最大値で測る:
- documents 系（TDnet / EDINET / ニュース各社）… ``docs.documents.as_of``（source_name 別）
- bars 系（J-Quants 日足）… ``market.bars.as_of``（source 別）
- indicators 系（FRED）… ``market.indicators.as_of``（series_code 接頭辞別）

実行: ``python -m ryza.ingest.freshness``
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from ryza.db.conn import connect
from ryza.provenance import Run
from ryza.provenance import run as run_ctx

_OPS_CHANNEL = "ops"          # #運営（bot.CHANNELS の論理名。依存回避のため文字列直書き）
_COLOR_WARN = 0xC24E3A        # 赤（速報色に準拠）


@dataclass(frozen=True)
class FreshnessSLA:
    """1 ソースの鮮度 SLA 定義。

    ``kind``: documents|bars|indicators のいずれか。``key`` の解釈は kind による
    （documents=source_name、bars=source、indicators=series_code の LIKE パターン）。
    """

    label: str
    kind: str
    key: str
    max_age: timedelta


# 既定 SLA。頻度の高いソースほど短い（TDnet は 5 分ポーリング → 30 分で警告）。
DEFAULT_SLAS: list[FreshnessSLA] = [
    FreshnessSLA("TDnet 適時開示", "documents", "TDnet", timedelta(minutes=30)),
    FreshnessSLA("日銀", "documents", "日銀", timedelta(minutes=60)),
    FreshnessSLA("財務省", "documents", "財務省", timedelta(hours=6)),
    FreshnessSLA("金融庁", "documents", "金融庁", timedelta(hours=6)),
    FreshnessSLA("FRB", "documents", "FRB", timedelta(hours=6)),
    FreshnessSLA("EDINET 開示", "documents", "EDINET", timedelta(hours=26)),
    FreshnessSLA("J-Quants 日足", "bars", "jquants", timedelta(hours=26)),
    FreshnessSLA("FRED マクロ統計", "indicators", "FRED:%", timedelta(hours=26)),
    # ── T-012 一括拡張分 ────────────────────────────────────────────────────
    # RSS（日次巡回想定。更新頻度が低い機関は緩め）。
    FreshnessSLA("総務省統計局", "documents", "総務省統計局", timedelta(days=7)),
    FreshnessSLA("米労働統計局BLS", "documents", "米労働統計局BLS", timedelta(days=3)),
    FreshnessSLA("米経済分析局BEA", "documents", "米経済分析局BEA", timedelta(days=7)),
    FreshnessSLA("ECB プレス", "documents", "ECB", timedelta(days=3)),
    FreshnessSLA("BOE ニュース", "documents", "BOE", timedelta(days=3)),
    FreshnessSLA("IMF プレス", "documents", "IMF", timedelta(days=7)),
    # API 系。EDGAR は日次バッチ、e-Stat は月次統計中心、中銀系列は日次〜月次混在。
    FreshnessSLA("SEC EDGAR 開示", "documents", "EDGAR", timedelta(hours=50)),
    FreshnessSLA("EDGAR XBRL", "indicators", "EDGAR:%", timedelta(days=35)),
    FreshnessSLA("e-Stat 統計", "indicators", "ESTAT:%", timedelta(days=40)),
    FreshnessSLA("ECB 統計", "indicators", "ECB:%", timedelta(days=7)),
    FreshnessSLA("BOE 統計", "indicators", "BOE:%", timedelta(days=7)),
    FreshnessSLA("IMF 統計", "indicators", "IMF:%", timedelta(days=40)),
]


@dataclass(frozen=True)
class Breach:
    """SLA 違反 1 件。"""

    sla: FreshnessSLA
    last_as_of: datetime | None    # None=一度も取込が無い
    age: timedelta | None
    reason: str                    # 'no_data' | 'stale'


def _latest_as_of(
    conn: psycopg.Connection, sla: FreshnessSLA
) -> datetime | None:
    """ソースの最終取込時点（as_of の最大値）を返す。"""
    if sla.kind == "documents":
        sql = "SELECT max(as_of) FROM docs.documents WHERE source_name = %s"
        param: tuple = (sla.key,)
    elif sla.kind == "bars":
        sql = "SELECT max(as_of) FROM market.bars WHERE source = %s"
        param = (sla.key,)
    elif sla.kind == "indicators":
        sql = "SELECT max(as_of) FROM market.indicators WHERE series_code LIKE %s"
        param = (sla.key,)
    else:
        raise ValueError(f"未知の SLA kind: {sla.kind}")
    with conn.cursor() as cur:
        cur.execute(sql, param)
        row = cur.fetchone()
    return row[0] if row else None


def check_freshness(
    conn: psycopg.Connection,
    *,
    slas: list[FreshnessSLA] | None = None,
    now: datetime | None = None,
) -> list[Breach]:
    """全 SLA を検査し、違反（no_data / stale）のリストを返す。"""
    slas = slas if slas is not None else DEFAULT_SLAS
    now = now or datetime.now(UTC)
    breaches: list[Breach] = []
    for sla in slas:
        last = _latest_as_of(conn, sla)
        if last is None:
            breaches.append(Breach(sla=sla, last_as_of=None, age=None, reason="no_data"))
            continue
        age = now - last
        if age > sla.max_age:
            breaches.append(
                Breach(sla=sla, last_as_of=last, age=age, reason="stale")
            )
    return breaches


def _breach_embed(breach: Breach) -> dict:
    """違反 1 件を #運営 向け embed（dict）に整形する。"""
    sla = breach.sla
    if breach.reason == "no_data":
        detail = "一度も取込されていない（未起動 or ソース停止の疑い）"
    else:
        hours = breach.age.total_seconds() / 3600 if breach.age else 0
        detail = f"最終取込から {hours:.1f} 時間経過（SLA {sla.max_age}）"
    return {
        "title": f"⚠️ 鮮度 SLA 違反: {sla.label}",
        "description": detail,
        "color": _COLOR_WARN,
        "fields": [
            {"name": "ソース種別", "value": sla.kind, "inline": True},
            {"name": "理由", "value": breach.reason, "inline": True},
            {
                "name": "最終取込",
                "value": breach.last_as_of.isoformat() if breach.last_as_of else "なし",
                "inline": False,
            },
        ],
    }


def enqueue_alerts(
    conn: psycopg.Connection,
    run: Run,
    breaches: list[Breach],
    *,
    channel: str = _OPS_CHANNEL,
) -> int:
    """違反を ``press.outbox`` へ警告として投入する。投入件数を返す。"""
    count = 0
    for breach in breaches:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
                VALUES (%s, %s, %s, %s)
                """,
                (channel, Jsonb(_breach_embed(breach)), True, run.run_id),
            )
        count += 1
    return count


def run_check(
    conn: psycopg.Connection,
    run: Run,
    *,
    slas: list[FreshnessSLA] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """検査 → 違反を #運営 へ投入するまでを一括で行う。``{'breaches', 'enqueued'}``。"""
    breaches = check_freshness(conn, slas=slas, now=now)
    enqueued = enqueue_alerts(conn, run, breaches)
    return {"breaches": len(breaches), "enqueued": enqueued}


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="鮮度 SLA 検査").parse_args(argv)
    conn = connect(autocommit=True)
    try:
        with run_ctx("ingest.freshness", conn=conn) as r:
            result = run_check(conn, r)
        print(f"freshness: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
