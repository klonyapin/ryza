"""base — FM 共通の入力読出しと「提案 → 記録 → ゲート → 注文」の唯一の経路(T-017)。

FM(Ben・Jim)は **Intent(採否)** だけを作る。本モジュールが、

1. 現在ポジション・参照価格・NAV・現金を **point-in-time**(as_of 以前)で読み
2. ``sizing`` の決定論コードで数量を決め(確信度は入らない — 不変原則1)
3. ``theses.record_thesis`` で論拠・反証条件・証憑を記録し(thesis_id)
4. ``gate.orders.gate_and_record`` へ注文案を投げる(唯一の発注経路 — 00 §9)

という順で処理する。block された案も ``fm_theses`` に残り、orders.thesis_id 経由で
判定結果を辿れる(次回プロンプトの学習材料 — 指示書6)。

設計上の判断:

- **資産クラスの導出** ``ips_asset_class``: ゲートが要求する IPS §8.1 タクソノミー
  (equity_jp 等)は ``market.instrument_classification``(0015)に列が無いため、
  銘柄マスタの ``asset_class``×``venue`` から決定論で導く。分類できない銘柄は候補から
  落とす(fail-closed。ゲートに投げても G-F で block される)。分類列そのものを
  0015 に足すのは保護領域スキーマの変更で T-017 の範囲外 — 引き継ぎ事項とする
- **NAV・現金は会計から読む**: NAV は ``ledger.nav_snapshots`` の as_of 以前の最新、
  現金は ``cash`` 勘定の残高。値が無ければ ``None`` のままゲートへ渡し、ゲートが
  fail-closed で block する(FM 側で「たぶん大丈夫」を作らない)
- **クローズを先に処理する**: invalidation 成立の解消でスロットを空けてから新規建てを
  評価する(同一実行内で決定論の順序 — 銘柄 ID 昇順)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from ryza.fm import sizing
from ryza.fm.theses import LONG_ONLY_DIRECTIONS, record_thesis
from ryza.gate.compliance import GateResult, OrderProposal, PositionState
from ryza.gate.orders import gate_and_record
from ryza.ips import IPSConfig, Mandate, load_and_validate
from ryza.provenance import Run
from ryza.risk.classify import (
    Classification,
    classification_pit_status,
    recorded_before,
)

_JST = ZoneInfo("Asia/Tokyo")

# 銘柄マスタ(market.instruments)の asset_class×venue → IPS §8.1 タクソノミー。
# 決定論の対応表。ここに無い組み合わせは分類不能として候補から落とす(fail-closed)。
_JP_EQUITY_VENUES = frozenset({"TSE"})
_US_EQUITY_VENUES = frozenset({"NYSE", "NASDAQ"})


def ips_asset_class(asset_class: str, venue: str) -> str | None:
    """銘柄マスタの分類 → IPS §8.1 資産クラス。分類できなければ None。"""
    if asset_class == "equity":
        if venue in _JP_EQUITY_VENUES:
            return "equity_jp"
        if venue in _US_EQUITY_VENUES:
            return "equity_us"
        return None
    if asset_class == "fx":
        return "fx"
    return None


@dataclass(frozen=True)
class Candidate:
    """ユニバース内の1銘柄(銘柄マスタ+決定論分類の合成)。"""

    instrument_id: int
    symbol: str
    asset_class: str  # IPS §8.1 タクソノミー
    classification: Classification


@dataclass(frozen=True)
class UniverseRead:
    """ユニバース読出しの結果と、**実際に使った読出し経路**(審査 C-20)。

    経路名を結果に同梱するのは、実行サマリの表示のために条件を再導出すると、既定の
    READ COMMITTED では並行 commit を挟んで実際の読出しと食い違い得るため。
    """

    candidates: list[Candidate]
    source: str


@dataclass(frozen=True)
class Intent:
    """FM が出した「採否」。**数量・金額は持たない**(サイズは決定論コードが決める)。"""

    fm: str
    instrument_id: int
    direction: str  # buy | close(第一陣は long-only)
    thesis_md: str
    evidence_refs: list[dict[str, Any]]
    invalidation_md: str
    rule_id: str | None = None  # 決定論シグナル由来(Jim)
    model: str | None = None  # LLM 由来(Ben)


@dataclass
class SubmitResult:
    """1回の提案投入の結果要約。"""

    proposed: int = 0
    passed: int = 0
    blocked: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposed": self.proposed,
            "passed": self.passed,
            "blocked": self.blocked,
            "skipped": len(self.skipped),
            "skip_reasons": [s["reason"] for s in self.skipped],
            "orders": self.orders,
        }


# ── 入力読出し(すべて point-in-time)──────────────────────────────────────────
# ユニバースの読出しは**常に**追記オンリー履歴(0026)から行う(審査 C-17)。
# 現在値キャッシュ(0015)を「当日は等価だから」と併用する設計は破綻する: 現在値行の
# as_of は上書き更新で巻き戻り得るため、等価性を現在値表自身から判定できない。
# 走査は1日1回で DISTINCT ON 1 段の差は性能上の意味を持たず、経路を1本にする方が
# 「判断に使った分類」の説明可能性で勝る。
#
# 時間軸は2つ(bitemporal — 審査 C-16):
#   as_of        <= 判断時点         … その分類が有効になっていたか
#   created_at   <  判断時点の当日終端 … その分類がその時点で**記録されていた**か
# 後者が無いと、今日 1 行追記するだけで過去のリプレイ結果が変わる。
#
# **タグ照合は「as_of 時点で最新の行」を選んだ後に効かせる**: 先に絞ると、タグが後から
# 付いた銘柄の古い行が拾われ、分類の変更が過去に漏れる(look-ahead — 審査 C-4)。
UNIVERSE_SOURCE = "history"

_UNIVERSE_HISTORY_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (instrument_id)
               instrument_id, universe_tags, instrument_flags, is_single_name,
               product, unit_size
        FROM market.instrument_classification_history
        WHERE as_of <= %(as_of)s AND created_at < %(recorded_before)s
        ORDER BY instrument_id, as_of DESC, history_id DESC
    )
    SELECT DISTINCT ON (i.instrument_id)
           i.instrument_id, i.symbol, i.asset_class, i.venue,
           l.universe_tags, l.instrument_flags, l.is_single_name,
           l.product, l.unit_size
    FROM latest l
    JOIN market.instruments i ON i.instrument_id = l.instrument_id
    WHERE l.universe_tags && %(tags)s
      AND i.valid_from <= %(as_of)s
      AND (i.valid_to IS NULL OR i.valid_to > %(as_of)s)
    ORDER BY i.instrument_id, i.valid_from DESC
    LIMIT %(limit)s
"""


def is_replay(as_of: datetime) -> bool:
    """判断時点が**過去日**か(JST 日付で判定)。当日・未来は通常運転として扱う。"""
    return as_of.astimezone(_JST).date() < datetime.now(tz=_JST).date()


def load_universe(
    conn: psycopg.Connection, mandate: Mandate, *, as_of: datetime, limit: int = 500
) -> UniverseRead:
    """マンデートのユニバースに属する銘柄(その時点の決定論分類つき)を返す。

    分類の**行がある銘柄のみ**が対象(行なし=未分類=ゲートが fail-closed で block
    する — T-015 の設計)。読出しは常に追記オンリー履歴から bitemporal に行う
    (経路の選択は無い — 審査 C-17。理由は ``_UNIVERSE_HISTORY_SQL`` 上のコメント)。

    返り値は候補と**実際に使った読出し経路**の組(``UniverseRead``)。経路名を実行時に
    持ち回るのは、サマリ表示のために条件を再導出すると並行 commit 下で実際の読出しと
    食い違うため(審査 C-20)。履歴がその as_of をカバーしているかは
    ``universe_pit_status`` が別途報告する。
    """
    with conn.cursor() as cur:
        cur.execute(
            _UNIVERSE_HISTORY_SQL,
            {
                "tags": list(mandate.universe),
                "as_of": as_of,
                "recorded_before": recorded_before(as_of),
                "limit": limit,
            },
        )
        rows = cur.fetchall()
    candidates: list[Candidate] = []
    for iid, symbol, asset_class, venue, tags, flags, single, product, unit in rows:
        ips_class = ips_asset_class(asset_class, venue)
        if ips_class is None:
            continue  # 資産クラス分類不能 → 候補にしない(fail-closed)
        candidates.append(
            Candidate(
                instrument_id=int(iid),
                symbol=symbol,
                asset_class=ips_class,
                classification=Classification(
                    universe_tags=tuple(tags),
                    instrument_flags=tuple(flags),
                    is_single_name=single,
                    product=product,
                    unit_size=None if unit is None else Decimal(unit),
                ),
            )
        )
    return UniverseRead(candidates=candidates, source=UNIVERSE_SOURCE)


def universe_pit_status(
    conn: psycopg.Connection, *, as_of: datetime, source: str
) -> dict[str, Any]:
    """ユニバースの point-in-time 保証(E6)の充足状況。**実行サマリに必ず載せる**。

    ``covered=True`` は「この as_of の分類が追記オンリー履歴で再現されている」の意で、
    このときだけ E6 の但し書きが外れる(審査 C-4 の裁定の解除条件)。履歴の記録開始
    (``min(created_at)``)より前の as_of は ``covered=False`` のままで、``note`` に
    未達の理由が入る — 移行前の期間について達成を主張しない。

    ``source`` は**呼び出し側が実際の読出しから受け取った経路**を渡す(再導出しない
    — 審査 C-20)。
    """
    status = classification_pit_status(conn, as_of=as_of)
    return {
        "replay": is_replay(as_of),
        "source": source,
        "e6_covered": status["covered"],
        "history_since": status["since"],
        "note": status["note"],
    }


def load_positions(conn: psycopg.Connection, book_id: str) -> tuple[PositionState, ...]:
    """帳簿の全ポジション(全 FM)。ゲートと同じ ``PositionState`` 語彙で返す。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fm, instrument_id, asset_class, qty, avg_cost
            FROM trading.positions WHERE book_id = %s AND qty <> 0
            """,
            (book_id,),
        )
        return tuple(
            PositionState(
                fm=r[0], instrument_id=r[1], asset_class=r[2],
                qty=Decimal(r[3]), avg_cost=Decimal(r[4]),
            )
            for r in cur.fetchall()
        )


def load_pending_orders(
    conn: psycopg.Connection, book_id: str, fm: str
) -> tuple[dict[int, Decimal], set[int]]:
    """当該 FM の**未約定の通過注文**(passed/submitted)を (建て, 決済) に分けて返す。

    ポジションは約定してはじめて動くため、通過済みで未約定の注文はどこにも現れない。
    同じ FM が同じ日に二度走れば、同じ銘柄へ二度スロットを割り当て(建て)、あるいは
    同じ建玉を二度売る(決済)ことができてしまう — 審査 C-1 の穴の実行またぎ版。

    返り値は ``({建て注文の銘柄 → 数量}, {決済注文が出ている銘柄})``。前者はスロットを
    占有し、後者は追加のクローズ提案を止める(いずれも fail-closed 側)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT instrument_id, side, sum(qty) FROM trading.orders
            WHERE book_id = %s AND fm = %s AND status IN ('passed', 'submitted')
            GROUP BY instrument_id, side
            """,
            (book_id, fm),
        )
        rows = cur.fetchall()
    entries: dict[int, Decimal] = {}
    closing: set[int] = set()
    for instrument_id, side, qty in rows:
        if side in ("buy", "cover"):
            entries[int(instrument_id)] = entries.get(int(instrument_id), Decimal(0)) + Decimal(qty)
        else:
            closing.add(int(instrument_id))
    return entries, closing


def load_prices(
    conn: psycopg.Connection,
    instrument_ids: list[int],
    *,
    as_of: datetime,
    timeframe: str = "1d",
) -> dict[int, Decimal]:
    """as_of 以前の最新終値(point-in-time — バーの ts も as_of も判断時点以前)。"""
    if not instrument_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (instrument_id) instrument_id, close
            FROM market.bars
            WHERE instrument_id = ANY(%s) AND timeframe = %s AND close IS NOT NULL
              AND ts <= %s AND as_of <= %s
            ORDER BY instrument_id, ts DESC, as_of DESC
            """,
            (list(instrument_ids), timeframe, as_of, as_of),
        )
        return {int(r[0]): Decimal(r[1]) for r in cur.fetchall()}


def load_nav_and_cash(
    conn: psycopg.Connection, book_id: str, *, as_of: datetime
) -> tuple[Decimal | None, Decimal | None]:
    """判断時点(JST 日付)以前の最新 NAV と現金残高。無ければ None(ゲートが block)。"""
    day = as_of.astimezone(_JST).date()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT nav FROM ledger.nav_snapshots
            WHERE book_id = %s AND snap_date <= %s
            ORDER BY snap_date DESC LIMIT 1
            """,
            (book_id, day),
        )
        row = cur.fetchone()
        nav = None if row is None else Decimal(row[0])
        cur.execute(
            """
            SELECT sum(jl.debit - jl.credit)
            FROM ledger.journal_lines jl
            JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
            WHERE jl.book_id = %s AND jl.account_id = 'cash' AND je.entry_date <= %s
            """,
            (book_id, day),
        )
        row = cur.fetchone()
        cash = None if row is None or row[0] is None else Decimal(row[0])
    return nav, cash


# ── 提案 → 記録 → ゲート ──────────────────────────────────────────────────────
def _build_proposal(
    *,
    book_id: str,
    fm: str,
    candidate: Candidate,
    side: str,
    qty: Decimal,
    price: Decimal,
    thesis_id: int,
) -> OrderProposal:
    """注文案を組む。分類値は**銘柄マスタ由来の決定論分類のみ**(LLM 出力は入れない)。

    ``signal_ids`` には thesis_id を入れる — Jim のマンデート
    ``discretionary_trades_outside_signals``(シグナル外売買の禁止・独立レビュー C-13)は
    「全注文に signal_id の紐付けを必須とするデータ契約」で機械判定される。FM 提案の
    signal は fm_theses の行そのものである。
    """
    c = candidate.classification
    return OrderProposal(
        book_id=book_id,
        fm=fm,
        instrument_id=candidate.instrument_id,
        side=side,
        qty=qty,
        order_type="market",
        ref_price=price,
        product=c.product,
        asset_class=candidate.asset_class,
        universe_tags=c.universe_tags,
        instrument_flags=c.instrument_flags,
        is_single_name=c.is_single_name,
        is_margin=False,
        unit_size=c.unit_size,
        signal_ids=(thesis_id,),
    )


def dedupe_intents(intents: list[Intent]) -> tuple[list[Intent], list[dict[str, Any]]]:
    """同一 instrument_id の Intent を先頭のみ残す(決定論・審査 C-1)。

    重複を許すと 1 銘柄に複数スロットが割り当たり、個々の注文はゲート G-3(ポッド内
    集中度)を通りながら合計で上限を破れる — G-3 は同一実行内の pending 注文を
    約定後想定に加算しないため。生成側(LLM・シグナル)の重複が実効的な集中度を決める
    のは不変原則1 の趣旨に反するので、ゲートに投げる前にここで潰す。

    返り値は ``(採用した Intent, 落とした理由の記録)``。
    """
    seen: set[int] = set()
    kept: list[Intent] = []
    dropped: list[dict[str, Any]] = []
    for intent in intents:
        if intent.instrument_id in seen:
            dropped.append(
                {
                    "instrument_id": intent.instrument_id,
                    "reason": "同一銘柄の重複提案(先頭のみ採用 — 集中度の二重割り当て防止)",
                }
            )
            continue
        seen.add(intent.instrument_id)
        kept.append(intent)
    return kept, dropped


def submit_intents(
    conn: psycopg.Connection,
    run: Run,
    intents: list[Intent],
    *,
    mandate: Mandate,
    max_slots: int,
    candidates: dict[int, Candidate],
    producer: str,
    book_id: str,
    as_of: datetime,
    ips: IPSConfig | None = None,
    mandates: dict[str, Mandate] | None = None,
    trade_date: date | None = None,
) -> SubmitResult:
    """Intent(採否)を数量つきの注文案に変換し、記録 → ゲートへ通す。

    処理順は**完全に決定論**(独立役員審査 2026-08-03 C-1/C-8):

    1. **重複排除**: 同一 instrument_id の Intent は先頭のみ採用する。重複を許すと
       1銘柄に複数スロットが割り当てられ、各注文が個別にはゲート G-3(ポッド内集中度)
       を通りながら合計で上限を破れる(G-3 は pending 注文を post-trade に加算しない)。
       LLM の出力の重複が実効集中度を決めてはならない — 不変原則1
    2. **順序の固定**: クローズ → 新規建ての順、各群は **instrument_id 昇順**。
       LLM の出力順(= 生成側の選好)がスロットの配分優先度を決めない
    3. **実行内 held の更新**: 通過した新規建ての銘柄は即座に保有扱いにし、通過した
       クローズはスロットを空ける。同一実行内で同じ銘柄に二度スロットを割り当てない
       (1 の二重防御)。未約定の通過注文(``load_pending_orders``)も同様に扱い、
       同じ日の二度目の実行で二重に建てる/二重に売ることを防ぐ

    スロットが尽きた分・数量 0・価格欠落・保有なしのクローズ・語彙外 direction は
    ``skipped`` に理由つきで残す(黙って落とさない)。
    """
    if ips is None or mandates is None:
        loaded_ips, loaded_mandates = load_and_validate()
        ips = ips or loaded_ips
        mandates = mandates or loaded_mandates
    trade_date = trade_date or as_of.astimezone(_JST).date()
    result = SubmitResult()

    positions = load_positions(conn, book_id)
    # plan は1スロットの金額(仮想資本 ÷ スロット数)の正。空きスロット数は下で
    # 「保有 + 未約定の通過注文」から数え直す(約定前の枠も占有として数える)。
    plan = sizing.slot_plan(mandate, max_slots=max_slots, positions=positions)
    held = sizing.held_positions(positions, mandate.fm)
    pending_entries, closing = load_pending_orders(conn, book_id, mandate.fm)
    for instrument_id, qty in pending_entries.items():
        held.setdefault(instrument_id, qty)
    nav, cash = load_nav_and_cash(conn, book_id, as_of=as_of)

    unique, duplicates = dedupe_intents(intents)
    result.skipped.extend(duplicates)

    needed = {p.instrument_id for p in positions} | {i.instrument_id for i in unique}
    prices = load_prices(conn, sorted(needed), as_of=as_of)

    ordered = sorted(
        (i for i in unique if i.direction in LONG_ONLY_DIRECTIONS),
        key=lambda i: (0 if i.direction == "close" else 1, i.instrument_id),
    )
    for intent in unique:
        if intent.direction not in LONG_ONLY_DIRECTIONS:
            # 語彙外・第一陣が扱わない direction は黙って落とさず理由を残す(審査 C-7)。
            result.skipped.append(
                {
                    "instrument_id": intent.instrument_id,
                    "reason": f"未対応の direction={intent.direction!r}(第一陣は buy/close)",
                }
            )
    free_slots = max(max_slots - len(held), 0)

    for intent in ordered:
        candidate = candidates.get(intent.instrument_id)
        if candidate is None:
            result.skipped.append(
                {"instrument_id": intent.instrument_id, "reason": "ユニバース外(分類なし)"}
            )
            continue
        price = prices.get(intent.instrument_id)
        if price is None or price <= 0:
            result.skipped.append(
                {"instrument_id": intent.instrument_id, "reason": "参照価格が無い(as_of 以前)"}
            )
            continue

        if intent.direction == "close":
            qty = sizing.close_qty(positions, mandate.fm, intent.instrument_id)
            if qty <= 0:
                result.skipped.append(
                    {"instrument_id": intent.instrument_id, "reason": "保有なし(クローズ不要)"}
                )
                continue
            if intent.instrument_id in closing:
                result.skipped.append(
                    {
                        "instrument_id": intent.instrument_id,
                        "reason": "決済注文が未約定(二重売り防止)",
                    }
                )
                continue
            if held.get(intent.instrument_id, Decimal(0)) < 0:
                # 第一陣は long-only。負のポジションはこの経路で作られない(防御的)。
                result.skipped.append(
                    {"instrument_id": intent.instrument_id, "reason": "ショート建玉(long-only)"}
                )
                continue
            side = "sell"
        else:
            if intent.instrument_id in held:
                result.skipped.append(
                    {"instrument_id": intent.instrument_id, "reason": "保有済み(スロット占有)"}
                )
                continue
            if free_slots <= 0:
                result.skipped.append(
                    {"instrument_id": intent.instrument_id, "reason": "空きスロットなし"}
                )
                continue
            qty = sizing.entry_qty(
                slot_value=plan.slot_value,
                price=price,
                lot_size=candidate.classification.unit_size,
            )
            if qty <= 0:
                result.skipped.append(
                    {"instrument_id": intent.instrument_id, "reason": "1スロットが1単元に満たない"}
                )
                continue
            side = "buy"
            free_slots -= 1

        thesis_id = record_thesis(
            conn,
            fm=mandate.fm,
            book_id=book_id,
            instrument_id=intent.instrument_id,
            direction=intent.direction,
            thesis_md=intent.thesis_md,
            evidence_refs=intent.evidence_refs,
            invalidation_md=intent.invalidation_md,
            producer=producer,
            as_of=as_of,
            run_id=run.run_id,
            rule_id=intent.rule_id,
            model=intent.model,
        )
        proposal = _build_proposal(
            book_id=book_id, fm=mandate.fm, candidate=candidate,
            side=side, qty=qty, price=price, thesis_id=thesis_id,
        )
        order_id, gate_log_id, verdict = gate_and_record(
            conn, proposal, nav=nav, cash=cash, run_id=run.run_id,
            prices=prices, ips=ips, mandates=mandates, trade_date=trade_date,
            thesis_id=thesis_id,
        )
        result.proposed += 1
        if verdict.blocked:
            result.blocked += 1
            free_slots += 1 if side == "buy" else 0  # 通らなかった枠は戻す
        else:
            result.passed += 1
            if side == "buy":
                # 通過した新規建ては即座に保有扱い(実行内の二重割り当て防止 — 審査 C-1)。
                # 約定前でもスロットは消費済みとみなす(fail-closed 側に倒す)。
                held[intent.instrument_id] = qty
            else:
                # 通過したクローズはスロットを空け(新規建てより先に評価される)、
                # 以降の二重売りを止める。
                held.pop(intent.instrument_id, None)
                closing.add(intent.instrument_id)
                free_slots += 1
        result.orders.append(
            _order_summary(intent, thesis_id, order_id, gate_log_id, verdict, side, qty)
        )
    return result


def _order_summary(
    intent: Intent,
    thesis_id: int,
    order_id: int,
    gate_log_id: int,
    verdict: GateResult,
    side: str,
    qty: Decimal,
) -> dict[str, Any]:
    return {
        "instrument_id": intent.instrument_id,
        "direction": intent.direction,
        "side": side,
        "qty": str(qty),
        "thesis_id": thesis_id,
        "order_id": order_id,
        "gate_log_id": gate_log_id,
        "verdict": verdict.verdict,
        "reasons": [r.message for r in verdict.reasons],
    }


__all__ = [
    "UNIVERSE_SOURCE",
    "Candidate",
    "Intent",
    "SubmitResult",
    "UniverseRead",
    "dedupe_intents",
    "ips_asset_class",
    "is_replay",
    "load_nav_and_cash",
    "load_pending_orders",
    "load_positions",
    "load_prices",
    "load_universe",
    "submit_intents",
    "universe_pit_status",
]
