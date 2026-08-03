"""classify — 銘柄マスタ由来の決定論分類(T-015。保護領域 — 定款第5条)。

ゲート(T-014)の ``OrderProposal`` が要求する分類(``universe_tags``・
``instrument_flags``・``is_single_name``・``product``・``unit_size``)を
``market.instruments``(SCD2)から決定論ルールで導出し、
``market.instrument_classification``(0015)に保存する。LLM 不関与。

**point-in-time(不変原則4・E6 — 独立役員審査 T-017 C-4 の是正)**: 分類は
``market.instrument_classification_history``(0026・追記オンリー)にも同一
トランザクションで追記する。**判断に使う分類は常に履歴表から引く**(C-17):
0015 の表は表示・運用確認のための現在値キャッシュであり、判断経路の入力にはしない。

読出しは **bitemporal**(C-16): ``as_of <= 判断時点`` かつ ``created_at`` が判断時点の
当日(JST)中までに記録された行だけを見る。片方(as_of)だけで絞ると、今日 1 行
追記するだけで過去のリプレイ結果を変えられてしまう — 追記オンリーは「追記による
改変」を止めないため、時間軸の制約でしか塞げない。

``classification_pit_status`` は履歴の記録開始時点(``min(created_at)`` — 表自身の
データ。改竄検知のない ``meta.schema_migrations`` には依存しない・C-19)を境界に、
カバー外の as_of へ「E6 未達」の但し書きを返す(移行前の期間について達成を主張しない)。

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
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

RULES_VERSION = "rules:v1"

_JST = ZoneInfo("Asia/Tokyo")

# カバー外の as_of に付ける但し書き(リプレイ・バックテスト結果に必ず添える)。
E6_UNCOVERED_NOTE = (
    "E6(point-in-time ユニバース)未達: 銘柄分類の追記オンリー履歴は {since} 以降のみ。"
    "この as_of は記録開始より前のため、当時のユニバースを再現できていない"
    "(独立役員審査 T-017 C-4)"
)
E6_NO_HISTORY_NOTE = (
    "E6(point-in-time ユニバース)未達: 銘柄分類の履歴に記録が1件も無い。"
    "ユニバースは空であり、当時の分類は再現できない"
)

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


def _row_to_classification(row: tuple[Any, ...]) -> Classification:
    """``(universe_tags, instrument_flags, is_single_name, product, unit_size)`` → 分類。"""
    return Classification(
        universe_tags=tuple(row[0]),
        instrument_flags=tuple(row[1]),
        is_single_name=row[2],
        product=row[3],
        unit_size=None if row[4] is None else Decimal(row[4]),
    )


def recorded_before(as_of: datetime) -> datetime:
    """as_of の当日(JST)の終端 = bitemporal 読出しで許す記録時刻の上限(排他)。

    「その判断時点で**既に記録されていた**分類だけを見る」ための第2の時間軸(審査 C-16)。
    日次ジョブは1日1回・同一営業日の中で as_of を進めながら走るため、境界を時刻ではなく
    JST 日の終端に置く(同日中に記録された分類はその日の判断で使えたと見なす)。
    """
    next_day = as_of.astimezone(_JST).date() + timedelta(days=1)
    return datetime.combine(next_day, time(0, 0), tzinfo=_JST)


def load_classification(
    conn: psycopg.Connection, instrument_id: int
) -> Classification | None:
    """現在値キャッシュを読む。**判断経路では使わない**(表示・運用確認用 — 審査 C-17)。

    **None = 未取得**(ゲートはタグ空で fail-closed block)。point-in-time の正は
    ``load_classification_at``(履歴表)である。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT universe_tags, instrument_flags, is_single_name, product, unit_size
            FROM market.instrument_classification WHERE instrument_id = %s
            """,
            (instrument_id,),
        )
        row = cur.fetchone()
    return None if row is None else _row_to_classification(row)


def load_classification_at(
    conn: psycopg.Connection, instrument_id: int, *, as_of: datetime
) -> Classification | None:
    """**as_of 時点で知られていた最新の**分類を履歴表から読む(リプレイの正)。

    bitemporal(審査 C-16): ``as_of <= 判断時点`` かつ ``created_at < 判断時点の当日終端``。
    後者が無いと、今日バックデート追記した 1 行が過去のリプレイ結果を変えてしまう。
    行が無ければ None(その時点では未分類 — ゲートは fail-closed で block する)。
    同一 as_of の訂正は後に書かれた行(history_id 降順)が勝つ。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT universe_tags, instrument_flags, is_single_name, product, unit_size
            FROM market.instrument_classification_history
            WHERE instrument_id = %s AND as_of <= %s AND created_at < %s
            ORDER BY as_of DESC, history_id DESC LIMIT 1
            """,
            (instrument_id, as_of, recorded_before(as_of)),
        )
        row = cur.fetchone()
    return None if row is None else _row_to_classification(row)


def history_coverage_since(conn: psycopg.Connection) -> datetime | None:
    """分類履歴が**記録され始めた時点**(履歴表の ``min(created_at)``)。

    これより前の as_of については、当時の分類がそもそも記録されていない(0026 の
    バックフィルは移行時点の現在値を写しただけで、移行前の改訂は物理的に存在しない)。
    したがって E6 を主張できる下限がこの時点になる。履歴が空なら None。

    根拠を**履歴表自身のデータ**に置くのは、``meta.schema_migrations`` が追記オンリー
    保護を持たず ``UPDATE`` 1 文で E6 の達成主張を偽装できるため(審査 C-19)。本表は
    UPDATE/DELETE/TRUNCATE をトリガで拒み、``created_at`` は INSERT トリガが ``now()`` に
    固定するので、古い記録時刻を持つ行を後から作ることもできない。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT min(created_at) FROM market.instrument_classification_history")
        row = cur.fetchone()
    return None if row is None else row[0]


def classification_pit_status(
    conn: psycopg.Connection, *, as_of: datetime
) -> dict[str, Any]:
    """as_of の分類が point-in-time で再現できるか(E6 の充足状況)。

    返り値は ``{covered, since, note}``。``covered=False`` の ``note`` は
    リプレイ結果・バックテスト結果に**必ず添える**但し書きである(審査 C-4 の裁定)。
    """
    since = history_coverage_since(conn)
    if since is None:
        return {"covered": False, "since": None, "note": E6_NO_HISTORY_NOTE}
    covered = as_of >= since
    return {
        "covered": covered,
        "since": since.isoformat(),
        "note": None if covered else E6_UNCOVERED_NOTE.format(since=since.isoformat()),
    }


def _effective_history_payload(
    conn: psycopg.Connection, instrument_id: int, as_of: datetime
) -> tuple[Any, ...] | None:
    """**その as_of 時点で有効な**履歴行の内容(圧縮の比較対象 — 審査 C-18)。

    全体で最新の行と比べると、バックデートした訂正(``as_of`` が最新行より古い書込)が
    「内容が最新行と同じ」だけの理由で無音で捨てられ、当該時点の分類が古いまま残る。
    比較対象は「今この as_of を読んだら返る行」でなければならない。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT universe_tags, instrument_flags, is_single_name, product,
                   unit_size, source
            FROM market.instrument_classification_history
            WHERE instrument_id = %s AND as_of <= %s
            ORDER BY as_of DESC, history_id DESC LIMIT 1
            """,
            (instrument_id, as_of),
        )
        return cur.fetchone()


def upsert_classification(
    conn: psycopg.Connection,
    instrument_id: int,
    c: Classification,
    *,
    run_id: int,
    source: str = RULES_VERSION,
    as_of: datetime | None = None,
) -> None:
    """分類を保存する(冪等)。curated 供給もこの口を使う(source="curated")。

    **履歴表(0026)への追記と現在値表の更新は同一トランザクション**で行う。片方だけが
    残ると「現在値はあるが当時の分類が無い」状態になり、リプレイのユニバースが静かに
    欠ける(審査 C-4)。commit は呼び出し側の責務。

    内容(タグ・フラグ・product・unit_size・source)が**その as_of 時点で有効な行**と
    同一なら追記はしない — 日次スイープの再実行で履歴が同内容の行で膨れるのを避ける
    (比較対象を全体の最新行にするとバックデート訂正が無音で消える — 審査 C-18)。
    現在値表の as_of / run_id は毎回更新するため、「いつ最後に確認したか」は現在値表が持つ。

    履歴行の ``created_at`` は DB 側が ``now()`` に固定する(申告できない)。したがって
    バックデートした as_of の書込は**過去のリプレイ結果を変えない** — 読出しが
    bitemporal だからである(審査 C-16)。
    """
    stamp = as_of or datetime.now(UTC)
    payload = (
        list(c.universe_tags), list(c.instrument_flags), c.is_single_name,
        c.product, c.unit_size, source,
    )
    previous = _effective_history_payload(conn, instrument_id, stamp)
    changed = previous is None or tuple(previous) != payload
    with conn.cursor() as cur:
        if changed:
            cur.execute(
                """
                INSERT INTO market.instrument_classification_history
                    (instrument_id, universe_tags, instrument_flags, is_single_name,
                     product, unit_size, source, as_of, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    instrument_id,
                    list(c.universe_tags),
                    list(c.instrument_flags),
                    c.is_single_name,
                    c.product,
                    c.unit_size,
                    source,
                    stamp,
                    run_id,
                ),
            )
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
                stamp,
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
    "E6_NO_HISTORY_NOTE",
    "E6_UNCOVERED_NOTE",
    "RULES_VERSION",
    "Classification",
    "classification_pit_status",
    "classify_current_instruments",
    "classify_instrument",
    "history_coverage_since",
    "load_classification",
    "load_classification_at",
    "recorded_before",
    "upsert_classification",
]
