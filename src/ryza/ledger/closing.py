"""日次締め(設計書 §5 のシーケンス)。

run_daily_close:
  1. 未記帳の約定を検出して記帳(冪等: 記帳済み fill はスキップ)
  2. 全ポジションを終値で評価替え(price_snapshot を evidence 化)
  3. アクルーアル(当面は手数料のみ。金利は TODO)
  4. NAV 算出 → nav_snapshots に provisional で保存
  5. recon の照合結果が全件 matched なら confirmed に更新、不一致なら provisional のまま

reclose_recent(独立審査 重要-2):
  締めが走った**後**に同じ日付で立った仕訳は当日のスナップショットに入らない。日次の
  締めはそのため当日に加えて直近 N 営業日の NAV を再計算し、値が変わった日だけ
  上書きする(``_upsert_nav`` は冪等)。詳細は関数 docstring。

nav_snapshots のリネージ(不変原則3):
  ``detail`` は jsonb であり、``detail.producer`` に producer_job / run_id /
  code_version / as_of / input_refs(仕訳の水位)を書く。既存列で足りるためスキーマ
  変更(保護領域)は不要。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

from ryza.ledger import _util, posting, recon, statements

# 帳簿 -> trade.order_intents.track の対応
_BOOK_TRACK = {"DEMO_FUND": "demo", "LIVE_FUND": "live"}

# nav_snapshots.detail.producer.job に記録するジョブ名(リネージの追跡単位)。
_JOB_DAILY_CLOSE = "ledger.closing.run_daily_close"
_JOB_RECLOSE = "ledger.closing.reclose_recent"

# price_source は callable(instrument_id)->price、または dict{instrument_id: price}
PriceSource = Callable[[int], Any] | dict[int, Any]


def _price_of(price_source: PriceSource, instrument_id: int) -> Any:
    if callable(price_source):
        return price_source(instrument_id)
    return price_source[instrument_id]


def _recorded_fill_ids(conn: psycopg.Connection, book_id: str) -> set[int]:
    """既に記帳済みの trade fill_id の集合(broker_fill 証憑の payload から抽出)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.payload_ref
            FROM ledger.journal_entries je
            JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
            WHERE je.book_id = %s AND e.kind = 'broker_fill'
            """,
            (book_id,),
        )
        recorded: set[int] = set()
        for (text,) in cur.fetchall():
            try:
                fid = json.loads(text).get("fill_id")
            except (ValueError, TypeError):
                continue
            if fid is not None:
                recorded.add(int(fid))
    return recorded


def _record_unrecorded_fills(
    conn: psycopg.Connection, book_id: str, date: _date, run_id: int
) -> list[int]:
    """trade.fills のうち未記帳のものを検出して記帳する。冪等。記帳した entry_id を返す。

    fill -> order -> intent の連鎖で track(=帳簿)と instrument/side を解決する。
    OPS 帳簿や、track 対応の無い帳簿では何もしない。
    """
    track = _BOOK_TRACK.get(book_id)
    if track is None:
        return []

    recorded = _recorded_fill_ids(conn, book_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.fill_id, oi.instrument_id, oi.side, f.qty, f.price, f.fee,
                   f.filled_at::date
            FROM trade.fills f
            JOIN trade.orders o ON o.order_id = f.order_id
            JOIN trade.order_intents oi ON oi.intent_id = o.intent_id
            WHERE oi.track = %s
            ORDER BY f.fill_id
            """,
            (track,),
        )
        pending = cur.fetchall()

    entry_ids: list[int] = []
    for fill_id, instrument_id, side, qty, price, fee, filled_date in pending:
        if fill_id in recorded:
            continue
        norm_side = "buy" if side in ("buy", "long") else "sell"
        entry_ids.append(
            posting.post_fill(
                conn,
                book_id=book_id,
                instrument_id=instrument_id,
                side=norm_side,
                qty=qty,
                price=price,
                fee=fee or 0,
                entry_date=filled_date or date,
                run_id=run_id,
                fill_id=fill_id,
                source="trade.fills",
                posted_by="ledger.closing",
            )
        )
    return entry_ids


def run_daily_close(
    conn: psycopg.Connection,
    *,
    book_id: str,
    date: _date,
    price_source: PriceSource,
    run_id: int,
    broker_snapshot: dict[str, Any] | None = None,
    broker: str = "sim",
    on_break: recon.BreakCallback | None = None,
) -> dict[str, Any]:
    """日次締めを実行し、要約 dict を返す。

    戻り値: {nav, status, marked, fills_recorded, recon}
    """
    bt = _util.book_type(conn, book_id)

    # 1. 未記帳の約定を検出して記帳(冪等)
    fills_recorded = _record_unrecorded_fills(conn, book_id, date, run_id)

    # 2. 全ポジションを終値で評価替え(ファンド帳簿のみ)
    marked: list[int] = []
    positions_detail: dict[str, Any] = {}
    if bt == "fund":
        for iid in _util.held_instruments(conn, book_id):
            qty, _cost = _util.replay_position(conn, book_id, iid)
            if qty == 0:
                continue
            price = _util.to_decimal(_price_of(price_source, iid))
            entry_id = posting.post_mark_to_market(
                conn,
                book_id=book_id,
                instrument_id=iid,
                price=price,
                entry_date=date,
                run_id=run_id,
                posted_by="ledger.closing",
            )
            if entry_id is not None:
                marked.append(entry_id)
            positions_detail[str(iid)] = {
                "qty": str(qty),
                "price": str(price),
                "market_value": str(qty * price),
            }

    # 3. アクルーアル: 当面は手数料のみ(約定時に計上済み)。
    #    TODO: 金利(信用取引の支払利息 interest_expense / 貸株料など)の日次アクルーアル。

    # 4. NAV 算出(= 資産 − 負債)→ nav_snapshots に provisional で保存
    totals = statements.book_totals(conn, book_id, date)
    nav = totals["nav"]
    detail = {
        "assets": str(totals["assets"]),
        "liabilities": str(totals["liabilities"]),
        "net_income": str(totals["net_income"]),
        "positions": positions_detail,
        "priced_at": date.isoformat(),
    }
    _upsert_nav(conn, book_id, date, nav, "provisional", detail, run_id)

    # 5. ブローカー照合。全件 matched なら confirmed に更新。
    recon_result = None
    status = "provisional"
    if broker_snapshot is not None:
        recon_result = recon.reconcile(
            conn,
            book_id=book_id,
            date=date,
            broker_snapshot=broker_snapshot,
            run_id=run_id,
            broker=broker,
            on_break=on_break,
        )
        if recon_result.all_matched:
            _upsert_nav(conn, book_id, date, nav, "confirmed", detail, run_id)
            status = "confirmed"

    return {
        "nav": nav,
        "status": status,
        "marked": marked,
        "fills_recorded": fills_recorded,
        "recon": recon_result,
    }


def _entries_watermark(conn: psycopg.Connection, book_id: str, as_of: _date) -> int | None:
    """NAV の入力になった仕訳の水位(``entry_date <= as_of`` の最大 entry_id)。

    リネージの input_refs(不変原則3)。同じ日付でも「どこまでの仕訳を見た値か」が
    残るため、締め後に立った仕訳による NAV の変化を後から説明できる。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(entry_id) FROM ledger.journal_entries "
            "WHERE book_id = %s AND entry_date <= %s",
            (book_id, as_of),
        )
        row = cur.fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _producer(
    conn: psycopg.Connection, book_id: str, as_of: _date, run_id: int, job: str
) -> dict[str, Any]:
    """生成物のリネージ(不変原則3: producer_job / code_version / input_refs / as_of)。

    ``code_version`` は ``meta.runs`` が唯一の記録元なので run_id から引く(締め側で
    git を叩き直すと 2 つの真実ができる)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT code_version FROM meta.runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return {
        "job": job,
        "run_id": int(run_id),
        "code_version": row[0] if row else None,
        "as_of": as_of.isoformat(),
        "input_refs": {"ledger.journal_entries.max_entry_id": _entries_watermark(
            conn, book_id, as_of
        )},
        "written_at": datetime.now(UTC).isoformat(),
    }


def _upsert_nav(
    conn: psycopg.Connection,
    book_id: str,
    snap_date: _date,
    nav: Decimal,
    status: str,
    detail: dict[str, Any],
    run_id: int,
    *,
    job: str = _JOB_DAILY_CLOSE,
) -> None:
    """nav_snapshots を upsert する(同日再締めは上書き。provisional→confirmed の更新に対応)。

    ``detail.producer`` に書き手のリネージを載せる — 「いつの締めが作った値か」を
    後から辿れるようにする(不変原則3)。detail は jsonb なので列の追加は不要。
    """
    detail = {**detail, "producer": _producer(conn, book_id, snap_date, run_id, job)}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (book_id, snap_date)
            DO UPDATE SET nav = EXCLUDED.nav, status = EXCLUDED.status, detail = EXCLUDED.detail
            """,
            (book_id, snap_date, nav, status, json.dumps(detail)),
        )


#: 再締めの対象日。**既にスナップショットが確定している直近の日**を新しい順に取る。
#: 「営業日」を祝日カレンダーで定義せず実績で定義する — 締めが走った日 = 帳簿にとっての
#: 営業日であり、外部カレンダーへの依存を持ち込まずに済む。締めが**走らなかった**日は
#: そもそもスナップショットが無く、その日の外部フローは navflow のロールフォワードが
#: 次の点へ寄せる(重要-5 の是正)。つまり再締めが救うべき対象は「確定済みの日」で
#: 必要十分である。
_RECENT_SNAP_DATES_SQL = """
SELECT snap_date FROM ledger.nav_snapshots
WHERE book_id = %s AND snap_date < %s
ORDER BY snap_date DESC
LIMIT %s
"""


def reclose_recent(
    conn: psycopg.Connection,
    *,
    book_id: str,
    through: _date,
    days: int,
    run_id: int,
) -> list[dict[str, Any]]:
    """直近 ``days`` 営業日ぶんの NAV を再計算し、値が変わった日だけ上書きする。

    **何を直すか**(独立審査 重要-2): 締めが走った後に同じ日付で立った仕訳(典型は
    出資・払戻)は、その日のスナップショットに入らない。``risk.navflow`` はその仕訳を
    当日の ``flow_eop`` として NAV から引くため、当日は偽の下振れ、翌日は同額の偽の
    上振れという ±X% の対を生む。翌営業日の締めで同じ日付を再計算すれば仕訳は NAV 側に
    入り、対は消える。

    **MTM を打ち直さない理由**: ここでは ``statements.book_totals`` の再計算と
    スナップショットの上書きだけを行い、``run_daily_close`` は呼ばない。
    ``post_mark_to_market`` は現在の保有数量(``replay_position`` は日付で切らない)を
    使うため、過去日付で呼ぶと**その日には存在しなかった建玉**を過去日付の仕訳として
    書いてしまう。過去日への新規記帳は行わず、既に記帳された仕訳の集計だけをやり直す。

    ``status`` は据え置く。遅れて立った拠出資本の仕訳は執行照合・ポジション照合の
    結論(= status の意味)を変えないため。値が変わった日は戻り値で返すので、
    呼び出し側が通知・監査に載せること(確定値の書き換えは黙って行わない)。

    戻り値: 値が変わった日の ``[{date, nav_before, nav_after, status}, ...]``(日付昇順)。
    """
    if days <= 0:
        return []
    with conn.cursor() as cur:
        cur.execute(_RECENT_SNAP_DATES_SQL, (book_id, through, days))
        snap_dates = [r[0] for r in cur.fetchall()]

    changed: list[dict[str, Any]] = []
    for snap_date in reversed(snap_dates):  # 古い順に処理(監査ログの読み順に合わせる)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nav, status, detail FROM ledger.nav_snapshots "
                "WHERE book_id = %s AND snap_date = %s",
                (book_id, snap_date),
            )
            prev_nav, status, prev_detail = cur.fetchone()
        prev_nav = _util.to_decimal(prev_nav)
        nav = statements.book_totals(conn, book_id, snap_date)["nav"]
        if nav == prev_nav:
            continue

        prev_detail = prev_detail if isinstance(prev_detail, dict) else {}
        detail = {
            **prev_detail,
            "reclose": {
                "nav_before": str(prev_nav),
                "reason": "締め後に同日付で立った仕訳の取り込み(独立審査 重要-2)",
                "previous_producer": prev_detail.get("producer"),
            },
        }
        _upsert_nav(conn, book_id, snap_date, nav, status, detail, run_id, job=_JOB_RECLOSE)
        changed.append(
            {
                "date": snap_date,
                "nav_before": prev_nav,
                "nav_after": nav,
                "status": status,
            }
        )
    return changed


__all__ = ["reclose_recent", "run_daily_close"]
