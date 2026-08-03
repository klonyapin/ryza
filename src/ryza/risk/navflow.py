"""navflow — NAV 系列に外部フローを突合する唯一の定義(独立審査 T-018 重要-5 の是正)。

**外部フローは「その日以降の最初のスナップショット日」に付ける**(ロールフォワード)。
従来は ``ledger.journal_entries.entry_date`` と ``ledger.nav_snapshots.snap_date`` の
**完全一致**で結合していたため、休日など締めが走らない日に付いた出資・払戻が
``net_flow`` に載らず、次のスナップショットまでのリターンが外部フローの分だけ誤って
いた(実測: 1/2 ¥100万 → 1/3(土)+¥50万出資・snapshot なし → 1/5 ¥150万 で
「+50%」— 実際の運用損益は 0%)。この誤リターンは EWMA ボラ・ES95 にも波及する。

ロールフォワードが正しい理由は ``ryza.risk.engine.book_returns`` の意味論から出る:
``r_t = (nav_t − flow_t − nav_{t−1}) / nav_{t−1}`` の ``flow_t`` は「``nav_{t−1}`` の
測定後から ``nav_t`` の測定までに外から入った(出た)金額」である。``nav_t`` は
その入出金を既に含んだ残高なので、区間内のフローは**区間終端の点**に集計しなければ
ならない。系列先頭より前のフローは先頭点に付くが、先頭点にリターンは無く
(``nav_0`` が既にそれを含んだ基準)測定に影響しない。

系列最終日より後のフロー(まだスナップショットが無い)は**捨てず**に
``ExternalFlows.pending`` として返す。呼び出し側(リスク日次レポート・ダッシュボード)は
これを注記として出す — 黙って落とすと「NAV が跳ねたのに net_flow が 0」の日が
次の締めまで見えなくなる。

配置: 会計テーブルの読み出しだが、規約そのもの(どの点にフローを寄せるか)は
リターン測定の定義であり ``risk.engine`` の TWR と一体で意味を持つため risk 配下に置く。
``ryza.risk.daily`` と ``dashboard/queries.py`` は**この 1 箇所を共有する**
(定義の二重化が重要-5 を生んだ — tests/dashboard/test_queries.py が両者の一致を固定)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import psycopg

#: 拠出資本勘定(``category='equity'`` かつ ``account_id <> 'retained'``)への仕訳を
#: 日次合算し、各フロー日をその日以降の最初の ``snap_date`` へ割り当てる。
#: ``snap_date IS NULL`` の行 = 系列最終日より後のフロー(未反映 — pending)。
#: 損益の振替(retained)は外部フローではないため除外する。
EXTERNAL_FLOW_SQL = """
WITH flow AS (
    SELECT je.entry_date AS entry_date, sum(jl.credit - jl.debit) AS amount
    FROM ledger.journal_lines jl
    JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
    JOIN ledger.accounts a
      ON a.book_id = jl.book_id AND a.account_id = jl.account_id
    WHERE jl.book_id = %(book)s
      AND a.category = 'equity' AND a.account_id <> 'retained'
    GROUP BY je.entry_date
)
SELECT (
           SELECT min(s.snap_date) FROM ledger.nav_snapshots s
           WHERE s.book_id = %(book)s AND s.snap_date >= f.entry_date
       ) AS snap_date,
       f.entry_date,
       f.amount
FROM flow f
ORDER BY f.entry_date
"""


@dataclass(frozen=True)
class PendingFlow:
    """スナップショットがまだ無い日の外部フロー(次の締めで系列に載る)。"""

    entry_date: date
    amount: Decimal  # 出資 +・払戻 −(JPY)


@dataclass(frozen=True)
class ExternalFlows:
    """スナップショット日へ寄せた外部フローと、未反映フローの一覧。"""

    by_snap_date: dict[date, Decimal] = field(default_factory=dict)
    pending: tuple[PendingFlow, ...] = ()

    def net_flow(self, day: date) -> Decimal:
        """``day`` のスナップショットに帰属する外部フロー純額(無ければ 0)。"""
        return self.by_snap_date.get(day, Decimal(0))

    def pending_note(self) -> str | None:
        """未反映フローの注記(無ければ None)。リスクレポート・UI で同じ文言を使う。"""
        return pending_flows_note(self.pending)


def pending_flows_note(pending: Sequence[PendingFlow]) -> str | None:
    """未反映フローの注記文(無ければ None)。文言はレポートと UI で共有する。"""
    if not pending:
        return None
    items = " / ".join(
        f"{p.entry_date} {'出資' if p.amount > 0 else '払戻'} ¥{abs(p.amount):,.0f}"
        for p in pending
    )
    return (
        f"【要確認】NAV スナップショット未生成の外部フロー {len(pending)} 件: "
        f"{items} — 次の会計締めまでリターン測定に反映されない"
    )


def load_external_flows(conn: psycopg.Connection, book_id: str) -> ExternalFlows:
    """帳簿の外部フローを読み、スナップショット日へロールフォワードして返す。"""
    with conn.cursor() as cur:
        cur.execute(EXTERNAL_FLOW_SQL, {"book": book_id})
        rows = cur.fetchall()
    by_snap: dict[date, Decimal] = {}
    pending: list[PendingFlow] = []
    for snap_date, entry_date, amount in rows:
        amount = Decimal(amount)
        if snap_date is None:
            if amount != 0:
                pending.append(PendingFlow(entry_date=entry_date, amount=amount))
        else:
            by_snap[snap_date] = by_snap.get(snap_date, Decimal(0)) + amount
    return ExternalFlows(by_snap_date=by_snap, pending=tuple(pending))


__all__ = [
    "EXTERNAL_FLOW_SQL",
    "ExternalFlows",
    "PendingFlow",
    "load_external_flows",
    "pending_flows_note",
]
