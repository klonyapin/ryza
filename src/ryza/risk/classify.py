"""classify — 銘柄マスタ由来の決定論分類(T-015。保護領域 — 定款第5条)。

ゲート(T-014)の ``OrderProposal`` が要求する分類(``universe_tags``・
``instrument_flags``・``is_single_name``・``product``・``unit_size``・``asset_class``)を
``market.instruments``(SCD2)から決定論ルールで導出し、
``market.instrument_classification``(0015)に保存する。LLM 不関与。

**資産クラス(IPS §8.1)の正は本モジュール**(0028 で列を追加): 以前は
``src/ryza/fm/base.py`` が読出しのたびに銘柄マスタから導出しており、分類の正が2箇所に
分かれていた。列に持たせることで、資産クラスの変更も他の分類と同じく決定論ジョブ・
追記オンリー履歴・承認手続を通る(reminder fm-asset-class-taxonomy-column)。

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

**curated ユニバース** ``apply_curated_universe``: 流動性系タグ(``liquid_equity``)は
母集団データが要るためルールでは付けられない。``config/universe/*.yaml`` に選定基準と
根拠つきで列挙された銘柄にだけタグを足す(config に無い銘柄はタグなし = fail-closed)。
付与は ``upsert_classification`` 経由で行うため、履歴(0026)にも同じ経路で残る。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import yaml

RULES_VERSION = "rules:v1"

_JST = ZoneInfo("Asia/Tokyo")

# IPS §8.1 資産クラス・タクソノミー(config/ips.yaml asset_class_taxonomy.classes と
# migrations/0028 の CHECK 制約と**同一集合**であること。三者の一致は
# tests/risk/test_classify.py が固定する — 規約ではなくテストで決着させる)。
IPS_ASSET_CLASSES = frozenset(
    {
        "equity_jp", "equity_us", "equity_other", "bond", "fx",
        "crypto", "commodity_futures", "rates", "cash",
    }
)

# curated ユニバース定義の既定ディレクトリ(リポジトリルート/config/universe)。
_ROOT = Path(__file__).resolve().parents[3]
CURATED_UNIVERSE_DIR = _ROOT / "config" / "universe"

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
    """1銘柄の決定論分類(``market.instrument_classification`` の行)。

    ``asset_class`` は IPS §8.1 のタクソノミー(0028 で追加)。**None = 分類不能**であり、
    読出し側(``fm.base.load_universe``)は候補から落とす(fail-closed)。既定を None に
    しているのは、既存の curated 供給経路が資産クラスを指定しないまま通ってしまうより、
    ユニバースから落ちて気付く方が安全なため。
    """

    universe_tags: tuple[str, ...]
    instrument_flags: tuple[str, ...]
    is_single_name: bool | None
    product: str
    unit_size: Decimal | None
    asset_class: str | None = None


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
                asset_class="equity_jp",
            )
        if venue in _US_VENUES:
            return Classification(
                universe_tags=("us_equity_cash",),
                instrument_flags=(),
                is_single_name=True,
                product="listed_equity_cash",
                unit_size=None,
                asset_class="equity_us",
            )
        return None
    if asset_class == "fx":
        return Classification(
            universe_tags=("fx",),
            instrument_flags=(),
            is_single_name=False,
            product="exchange_fx",
            unit_size=None,
            asset_class="fx",
        )
    # etf / future / option / crypto / bond: ルールでは主張しない(モジュール docstring)。
    return None


# 分類の全内容を読み書きする共通の列順(現在値表・履歴表で同一)。
_CLASSIFICATION_COLUMNS = (
    "universe_tags, instrument_flags, is_single_name, product, unit_size, asset_class"
)


def _row_to_classification(row: tuple[Any, ...]) -> Classification:
    """``_CLASSIFICATION_COLUMNS`` の順に並んだ行 → 分類。"""
    return Classification(
        universe_tags=tuple(row[0]),
        instrument_flags=tuple(row[1]),
        is_single_name=row[2],
        product=row[3],
        unit_size=None if row[4] is None else Decimal(row[4]),
        asset_class=row[5],
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
            f"SELECT {_CLASSIFICATION_COLUMNS} "  # noqa: S608 - 定数の列リスト(入力由来ではない)
            "FROM market.instrument_classification WHERE instrument_id = %s",
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
            f"SELECT {_CLASSIFICATION_COLUMNS} "  # noqa: S608 - 定数の列リスト(入力由来ではない)
            "FROM market.instrument_classification_history "
            "WHERE instrument_id = %s AND as_of <= %s AND created_at < %s "
            "ORDER BY as_of DESC, history_id DESC LIMIT 1",
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
            f"SELECT {_CLASSIFICATION_COLUMNS}, source "  # noqa: S608 - 定数の列リスト
            "FROM market.instrument_classification_history "
            "WHERE instrument_id = %s AND as_of <= %s "
            "ORDER BY as_of DESC, history_id DESC LIMIT 1",
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
    if c.asset_class is not None and c.asset_class not in IPS_ASSET_CLASSES:
        # DB の CHECK(0028)と二重の防御。curated 入力の typo を書込時に落とす。
        raise ValueError(
            f"asset_class {c.asset_class!r} は IPS §8.1 タクソノミーに無い "
            f"(語彙: {sorted(IPS_ASSET_CLASSES)})"
        )
    stamp = as_of or datetime.now(UTC)
    payload = (
        list(c.universe_tags), list(c.instrument_flags), c.is_single_name,
        c.product, c.unit_size, c.asset_class, source,
    )
    previous = _effective_history_payload(conn, instrument_id, stamp)
    changed = previous is None or tuple(previous) != payload
    values = (
        instrument_id,
        list(c.universe_tags),
        list(c.instrument_flags),
        c.is_single_name,
        c.product,
        c.unit_size,
        c.asset_class,
        source,
        stamp,
        run_id,
    )
    with conn.cursor() as cur:
        if changed:
            cur.execute(
                """
                INSERT INTO market.instrument_classification_history
                    (instrument_id, universe_tags, instrument_flags, is_single_name,
                     product, unit_size, asset_class, source, as_of, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                values,
            )
        cur.execute(
            """
            INSERT INTO market.instrument_classification
                (instrument_id, universe_tags, instrument_flags, is_single_name,
                 product, unit_size, asset_class, source, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (instrument_id) DO UPDATE SET
                universe_tags = EXCLUDED.universe_tags,
                instrument_flags = EXCLUDED.instrument_flags,
                is_single_name = EXCLUDED.is_single_name,
                product = EXCLUDED.product,
                unit_size = EXCLUDED.unit_size,
                asset_class = EXCLUDED.asset_class,
                source = EXCLUDED.source,
                as_of = EXCLUDED.as_of,
                run_id = EXCLUDED.run_id
            """,
            values,
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


# ── curated ユニバース(流動性系タグの供給 — reminder fm-jim-universe-...)────────
@dataclass(frozen=True)
class CuratedEntry:
    """curated ユニバースの1銘柄。``rationale``(選定根拠)は必須。"""

    symbol: str
    tags: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class CuratedUniverse:
    """``config/universe/<name>.yaml`` 1ファイル分。

    ``manages_tags`` は**このファイルが正であるタグ**の集合。config から外れた銘柄の
    タグを剥がす(revoke)判定に使う — 「config に無い銘柄はタグなし」を維持するには
    付与だけでなく撤回も config 駆動でなければならない。エントリ側のタグ集合ではなく
    独立の宣言にしているのは、最後の1銘柄を削除したときにも撤回が働くようにするため。
    """

    name: str
    version: str
    criterion: str
    manages_tags: frozenset[str]
    entries: tuple[CuratedEntry, ...]
    approved_at: str
    approved_by: str

    @property
    def source(self) -> str:
        """``upsert_classification`` に渡す出所(監査で config の版まで辿れる形)。"""
        return f"curated:{self.name}:v{self.version}"


class CuratedUniverseError(ValueError):
    """curated ユニバース定義が不正(必須項目の欠落・重複・語彙外タグ)。"""


def load_curated_universe(path: Path | str) -> CuratedUniverse:
    """curated ユニバース定義を読む。**根拠の無い銘柄は読み込み時に拒否する**。

    「誰かが銘柄コードを1行足した」だけでユニバースが広がる状態を作らないため、
    ``rationale``(なぜ基準を満たすか)を必須にする。基準そのもの(``criterion``)も
    ファイル単位で必須 — 基準を書かずに列挙されたリストは監査できない。

    **未承認(``approved_at`` が null)のファイルは拒否する**。curated タグの付与は
    「その FM が売買してよい母集団を広げる」操作であり、決定論ルールが安全側に倒して
    付けなかったタグを人手で足す唯一の口である。承認記録の無いリストが実行経路に
    入れるなら、fail-closed(タグを緩めて埋めない)は運用規約でしかなくなる。
    """
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    for key in ("name", "version", "criterion", "manages_tags", "entries"):
        if not raw.get(key):
            raise CuratedUniverseError(f"{p.name}: {key} は必須")
    if not raw.get("approved_at") or not raw.get("approved_by"):
        raise CuratedUniverseError(
            f"{p.name}: 未承認(approved_at / approved_by が空)。curated タグは FM の"
            "売買母集団を広げるため、承認記録の無いリストは反映しない"
        )
    manages = frozenset(str(t) for t in raw["manages_tags"])
    entries: list[CuratedEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(raw["entries"]):
        if not isinstance(item, dict):
            raise CuratedUniverseError(f"{p.name}: entries[{i}] がオブジェクトでない")
        symbol = str(item.get("symbol") or "").strip()
        rationale = str(item.get("rationale") or "").strip()
        tags = tuple(str(t) for t in (item.get("tags") or ()))
        if not symbol:
            raise CuratedUniverseError(f"{p.name}: entries[{i}] に symbol が無い")
        if symbol in seen:
            raise CuratedUniverseError(f"{p.name}: symbol {symbol!r} が重複している")
        if not rationale:
            raise CuratedUniverseError(
                f"{p.name}: {symbol} に rationale(選定根拠)が無い — "
                "基準を満たす理由を書かずに銘柄を足せない"
            )
        if not tags:
            raise CuratedUniverseError(f"{p.name}: {symbol} に tags が無い")
        unmanaged = set(tags) - manages
        if unmanaged:
            raise CuratedUniverseError(
                f"{p.name}: {symbol} のタグ {sorted(unmanaged)} は manages_tags の外"
                "(撤回できないタグを付与しない)"
            )
        seen.add(symbol)
        entries.append(CuratedEntry(symbol=symbol, tags=tags, rationale=rationale))
    return CuratedUniverse(
        name=str(raw["name"]),
        version=str(raw["version"]),
        criterion=str(raw["criterion"]),
        manages_tags=manages,
        entries=tuple(entries),
        approved_at=str(raw["approved_at"]),
        approved_by=str(raw["approved_by"]),
    )


def _resolve_symbols(
    conn: psycopg.Connection, symbols: list[str]
) -> dict[str, tuple[int, str, str]]:
    """symbol → (instrument_id, asset_class, venue)。現行版(valid_to IS NULL)のみ。"""
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (i.symbol)
                   i.symbol, i.instrument_id, i.asset_class, i.venue
            FROM market.instruments i
            WHERE i.symbol = ANY(%s) AND i.valid_to IS NULL
            ORDER BY i.symbol, i.valid_from DESC
            """,
            (symbols,),
        )
        return {r[0]: (int(r[1]), r[2], r[3]) for r in cur.fetchall()}


def _tagged_instruments(
    conn: psycopg.Connection, tags: list[str]
) -> list[tuple[int, str]]:
    """現在値表で当該タグを持つ (instrument_id, symbol)(撤回対象の探索)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (c.instrument_id) c.instrument_id, i.symbol
            FROM market.instrument_classification c
            JOIN market.instruments i ON i.instrument_id = c.instrument_id
            WHERE c.universe_tags && %s AND i.valid_to IS NULL
            ORDER BY c.instrument_id, i.valid_from DESC
            """,
            (tags,),
        )
        return [(int(r[0]), r[1]) for r in cur.fetchall()]


def _base_classification(
    conn: psycopg.Connection, instrument_id: int, asset_class: str, venue: str, symbol: str
) -> Classification | None:
    """タグを載せる土台。既存の現在値行があればそれ、無ければルール分類。

    既存行を優先するのは、以前の curated 供給が入れた ``instrument_flags``
    (レバ ETF 判定など)をルール分類で上書きして**消してしまわない**ため。
    どちらも無ければ None(= タグだけの行は作らない — 商品・単元が無い分類は
    ゲートで block されるだけで、fail-closed の意味も薄れる)。
    """
    existing = load_classification(conn, instrument_id)
    if existing is not None:
        return existing
    return classify_instrument(symbol=symbol, asset_class=asset_class, venue=venue)


def apply_curated_universe(
    conn: psycopg.Connection,
    universe: CuratedUniverse,
    *,
    run_id: int,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """curated ユニバースを分類へ反映する(付与と撤回の両方 — config が正)。

    付与は ``upsert_classification`` 経由のため、**追記オンリー履歴(0026)にも同じ
    トランザクションで残る**。したがって「いつからその銘柄が liquid_equity だったか」は
    point-in-time で再現でき、過去のリプレイに今日のタグが漏れない(不変原則4)。

    撤回(``revoked``)は、``manages_tags`` を現在持っているのに config に載っていない
    銘柄からタグを外す操作。付与だけを config 駆動にすると、config から1行消しても
    ユニバースが狭まらず「config が正」が嘘になる。

    返り値は ``{"granted", "unchanged", "revoked", "unresolved", "unclassifiable",
    "source"}``。``unresolved``(銘柄マスタに無い symbol)は**エラーにしない**が件数と
    シンボルを返す — 取込前の銘柄を先に curate できる一方、綴り間違いを黙って
    飲み込まないようにするため呼び出し側に露出させる。
    """
    stamp = as_of or datetime.now(UTC)
    by_symbol = {e.symbol: e for e in universe.entries}
    resolved = _resolve_symbols(conn, sorted(by_symbol))
    result: dict[str, Any] = {
        "granted": 0,
        "unchanged": 0,
        "revoked": 0,
        "unresolved": [],
        "unclassifiable": [],
        "source": universe.source,
    }

    for symbol in sorted(by_symbol):
        entry = by_symbol[symbol]
        found = resolved.get(symbol)
        if found is None:
            result["unresolved"].append(symbol)
            continue
        instrument_id, asset_class, venue = found
        base = _base_classification(conn, instrument_id, asset_class, venue, symbol)
        if base is None:
            result["unclassifiable"].append(symbol)
            continue
        merged_tags = tuple(base.universe_tags) + tuple(
            t for t in entry.tags if t not in base.universe_tags
        )
        changed = merged_tags != tuple(base.universe_tags)
        upsert_classification(
            conn,
            instrument_id,
            replace(base, universe_tags=merged_tags),
            run_id=run_id,
            source=universe.source,
            as_of=stamp,
        )
        result["granted" if changed else "unchanged"] += 1

    for instrument_id, symbol in _tagged_instruments(conn, sorted(universe.manages_tags)):
        wanted = set(by_symbol[symbol].tags) if symbol in by_symbol else set()
        current = load_classification(conn, instrument_id)
        if current is None:
            continue
        stripped = tuple(
            t for t in current.universe_tags
            if t not in universe.manages_tags or t in wanted
        )
        if stripped == tuple(current.universe_tags):
            continue
        upsert_classification(
            conn,
            instrument_id,
            replace(current, universe_tags=stripped),
            run_id=run_id,
            source=universe.source,
            as_of=stamp,
        )
        result["revoked"] += 1
    return result


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 実行パス
    """CLI: curated ユニバースの反映(決定論 — LLM 不使用)。

    ``uv run python -m ryza.risk.classify --curated-universe config/universe/jim-curated.yaml``
    """
    parser = argparse.ArgumentParser(description="銘柄分類(curated ユニバースの反映)")
    parser.add_argument(
        "--curated-universe",
        required=True,
        help="curated ユニバース定義 YAML(config/universe/*.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="反映せず読み込み検証だけ行う"
    )
    args = parser.parse_args(argv)

    from ryza.db.conn import connect
    from ryza.provenance import start_run

    try:
        universe = load_curated_universe(args.curated_universe)
    except (CuratedUniverseError, OSError) as exc:
        print(f"curated ユニバースを読めません: {exc}", file=sys.stderr)
        return 1
    print(
        f"{universe.source}: {len(universe.entries)} 銘柄 / 基準: {universe.criterion}",
        file=sys.stderr,
    )
    if args.dry_run:
        return 0

    run = start_run("risk.classify.curated", {"universe": universe.name})
    conn = connect()
    try:
        with conn.transaction():
            result = apply_curated_universe(conn, universe, run_id=run.run_id)
        run.finish("success")
    except Exception:
        run.finish("failed")
        raise
    finally:
        conn.close()
    print(f"反映しました: {result}", file=sys.stderr)
    return 0


__all__ = [
    "CURATED_UNIVERSE_DIR",
    "E6_NO_HISTORY_NOTE",
    "E6_UNCOVERED_NOTE",
    "IPS_ASSET_CLASSES",
    "RULES_VERSION",
    "Classification",
    "CuratedEntry",
    "CuratedUniverse",
    "CuratedUniverseError",
    "apply_curated_universe",
    "classification_pit_status",
    "classify_current_instruments",
    "classify_instrument",
    "history_coverage_since",
    "load_classification",
    "load_classification_at",
    "load_curated_universe",
    "main",
    "recorded_before",
    "upsert_classification",
]


if __name__ == "__main__":  # pragma: no cover - CLI 実行パス
    raise SystemExit(main())
