"""classify — 銘柄マスタ由来の決定論分類(T-015。保護領域 — 定款第5条)。

ゲート(T-014)の ``OrderProposal`` が要求する分類(``universe_tags``・
``instrument_flags``・``is_single_name``・``product``・``unit_size``)を
``market.instruments``(SCD2)から決定論ルールで導出し、
``market.instrument_classification``(0015)に保存する。LLM 不関与。

完全性の設計(T-014 審査条件7の残り半分 — 「空 vs 未取得」の区別):

- **行なし = 未取得**: ``load_classification`` は None を返し、呼び出し側はタグ空の
  提案を組む → ゲート G-2 が fail-closed で block
- **行あり・配列空 = 取得済み該当なし**: フラグなしと確認済みであることの主張

ルールで分類できる範囲は保守的に限定する(fail-closed の一貫化):

- **現物株**(equity): 上場市場からユニバース確定(TSE → jp_equity_cash、
  NYSE/NASDAQ → us_equity_cash)。個別銘柄=True。JP は1単元 100株
  (2018 年の単元統一 — 決定論の事実)。``liquid_equity``・``jp_equity_midcap_cash``
  等の流動性・時価総額系タグは母集団データが要るためルールでは**付けない**
  (タグ不足は狭める方向 = 安全側)
- **FX**(fx): exchange_fx・タグ fx
- **ETF・先物・オプション・暗号資産・債券は None**(ルール分類しない): ETF は
  レバ/インバース該当(IPS §5 禁止フラグ)を銘柄マスタから決定論で否定できず、
  「フラグなし」を主張すると fail-open になるため。先物は指数/金利/商品の別が
  マスタに無い。これらは curated 行(``upsert_classification(source="curated")``・
  承認記録つき運用)でのみ供給する
- **監理・整理銘柄**(supervised_or_delisting_stock)は日次で変わる状態でありマスタに
  無い。restricted_list(ips.yaml TODO)の配線と併せて後続課題(レポートに明記)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import psycopg

RULES_VERSION = "rules:v1"

# 上場市場 → 地域の決定論対応(銘柄マスタ venue 語彙)。
_JP_VENUES = frozenset({"TSE"})
_US_VENUES = frozenset({"NYSE", "NASDAQ"})

# 日本株の1単元(2018-10 の全国取引所の売買単位統一後は一律 100株)。
_JP_UNIT_SIZE = Decimal(100)


@dataclass(frozen=True)
class Classification:
    """1銘柄の決定論分類(``market.instrument_classification`` の行)。"""

    universe_tags: tuple[str, ...]
    instrument_flags: tuple[str, ...]
    is_single_name: bool | None
    product: str
    unit_size: Decimal | None


def classify_instrument(
    *, symbol: str, asset_class: str, venue: str
) -> Classification | None:
    """銘柄マスタの属性から分類を導出する。ルールで確定できなければ None(未分類)。"""
    del symbol  # 現ルールでは未使用(シンボル形式に依存しない — 将来の拡張余地)
    if asset_class == "equity":
        if venue in _JP_VENUES:
            return Classification(
                universe_tags=("jp_equity_cash",),
                instrument_flags=(),
                is_single_name=True,
                product="listed_equity_cash",
                unit_size=_JP_UNIT_SIZE,
            )
        if venue in _US_VENUES:
            return Classification(
                universe_tags=("us_equity_cash",),
                instrument_flags=(),
                is_single_name=True,
                product="listed_equity_cash",
                unit_size=None,
            )
        return None
    if asset_class == "fx":
        return Classification(
            universe_tags=("fx",),
            instrument_flags=(),
            is_single_name=False,
            product="exchange_fx",
            unit_size=None,
        )
    # etf / future / option / crypto / bond: ルールでは主張しない(モジュール docstring)。
    return None


def load_classification(
    conn: psycopg.Connection, instrument_id: int
) -> Classification | None:
    """保存済み分類を読む。**None = 未取得**(ゲートはタグ空で fail-closed block)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT universe_tags, instrument_flags, is_single_name, product, unit_size
            FROM market.instrument_classification WHERE instrument_id = %s
            """,
            (instrument_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Classification(
        universe_tags=tuple(row[0]),
        instrument_flags=tuple(row[1]),
        is_single_name=row[2],
        product=row[3],
        unit_size=None if row[4] is None else Decimal(row[4]),
    )


def upsert_classification(
    conn: psycopg.Connection,
    instrument_id: int,
    c: Classification,
    *,
    run_id: int,
    source: str = RULES_VERSION,
    as_of: datetime | None = None,
) -> None:
    """分類を保存する(冪等)。curated 供給もこの口を使う(source="curated")。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instrument_classification
                (instrument_id, universe_tags, instrument_flags, is_single_name,
                 product, unit_size, source, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (instrument_id) DO UPDATE SET
                universe_tags = EXCLUDED.universe_tags,
                instrument_flags = EXCLUDED.instrument_flags,
                is_single_name = EXCLUDED.is_single_name,
                product = EXCLUDED.product,
                unit_size = EXCLUDED.unit_size,
                source = EXCLUDED.source,
                as_of = EXCLUDED.as_of,
                run_id = EXCLUDED.run_id
            """,
            (
                instrument_id,
                list(c.universe_tags),
                list(c.instrument_flags),
                c.is_single_name,
                c.product,
                c.unit_size,
                source,
                as_of or datetime.now(UTC),
                run_id,
            ),
        )


def classify_current_instruments(conn: psycopg.Connection, *, run_id: int) -> dict[str, int]:
    """現行銘柄(valid_to IS NULL)のうち未分類のものへルール分類を適用する(日次配線)。

    curated 行(source="curated")は上書きしない(未分類行のみ対象)。
    返り値は ``{classified, unclassifiable, already}`` の件数。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (i.instrument_id)
                   i.instrument_id, i.symbol, i.asset_class, i.venue,
                   (c.instrument_id IS NOT NULL) AS has_row
            FROM market.instruments i
            LEFT JOIN market.instrument_classification c USING (instrument_id)
            WHERE i.valid_to IS NULL
            ORDER BY i.instrument_id, i.valid_from DESC
            """
        )
        rows = cur.fetchall()
    counts = {"classified": 0, "unclassifiable": 0, "already": 0}
    for instrument_id, symbol, asset_class, venue, has_row in rows:
        if has_row:
            counts["already"] += 1
            continue
        c = classify_instrument(symbol=symbol, asset_class=asset_class, venue=venue)
        if c is None:
            counts["unclassifiable"] += 1
            continue
        upsert_classification(conn, instrument_id, c, run_id=run_id)
        counts["classified"] += 1
    return counts


__all__ = [
    "RULES_VERSION",
    "Classification",
    "classify_current_instruments",
    "classify_instrument",
    "load_classification",
    "upsert_classification",
]
