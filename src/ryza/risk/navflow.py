"""navflow — NAV 系列と外部フローの突合(唯一の定義。独立審査 T-018 重要-5 の是正)。

**外部フローは「その日以降の最初のスナップショット日」に帰属させ、当日仕訳(EOP)と
区間内仕訳(BOP)に分けて返す。**従来は ``ledger.journal_entries.entry_date`` と
``ledger.nav_snapshots.snap_date`` の**完全一致**で結合していたため、休日など締めが
走らない日に付いた出資・払戻が ``net_flow`` に載らず、次のスナップショットまでの
リターンが外部フローの分だけ誤っていた(実測: 1/2 ¥100万 → 1/3(土)+¥50万出資・
snapshot なし → 1/5 ¥150万 で「+50%」— 実際の運用損益は 0%)。この誤リターンは
帳簿リターン系列を入力とする EWMA 実現ボラに波及する(ES95 は保有銘柄のリターン
系列から測るため ``book_returns`` を参照せず、影響しない — 独立審査 重要-3)。

**BOP/EOP を分ける理由**(独立審査 重要-1)。帰属先を決めるだけでは足りない。
``ryza.risk.engine.book_returns`` は
``r_t = (nav_t − flow_eop_t) / (nav_{t−1} + flow_bop_t) − 1`` で測る。これは
「区間の途中で入った資金はその区間の運用元本になっている」という事実に対応する。
フローを一律に期末扱い(分子から引くだけ)にすると区間リターンが
``(1 + flow/nav_{t−1})`` 倍に増幅され、V₀=100万・期中+50万・市場+5% のとき +7.5%
(真値 +5.0%)を報告する。資本形成期は ``flow/nav`` が 1 のオーダーになるため、
これは誤 ``vol_exceeded``(新規建てブロック)を出しうる大きさである。そこで当日仕訳を
``flow_eop``、前の測定日より後・当日より前の仕訳(= ロールフォワードされた分)を
``flow_bop`` として分けて返す。フローが無い日は従来式に退化する。

厳密には日中の入金時刻まで見なければ真値にはならないが、日次の帳簿にその情報は無い。
BOP 仮定は「区間の入金は区間の頭で入った」という保守側(分母を大きく = リターンを
小さく見せる)の丸めである。

系列最終日より後のフロー(まだスナップショットが無い)は**捨てず**に
``NavFlowData.pending`` として返す。呼び出し側(リスク日次レポート・ダッシュボード)は
これを注記として出す — 黙って落とすと「NAV が跳ねたのに net_flow が 0」の日が
次の締めまで見えなくなる。

配置: 会計テーブルの読み出しだが、規約そのもの(どの点に寄せるか・分母か分子か)は
リターン測定の定義であり ``risk.engine`` の TWR と一体で意味を持つため risk 配下に置く。
``ryza.risk.daily`` と ``dashboard/queries.py`` は**この 1 箇所を共有する**(定義の
二重化が重要-5 を生んだ — tests/dashboard/test_queries.py が両者の一致を固定)。
読み出しは 1 クエリで、NAV 点と pending が同一スナップショットから出ることを保証する
(独立審査 中-6 — 2 クエリだと両者がずれた時点を映しうる)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

#: 未反映フローを urgent にする材料性しきい値(NAV 比)。通知規約であり IPS 値ではない。
PENDING_MATERIALITY_NAV = Decimal("0.005")

#: NAV 点と外部フローを 1 クエリで読む。フローは拠出資本勘定(``category='equity'``
#: かつ ``account_id <> 'retained'``)への仕訳の日次純額で、損益の振替(retained)は
#: 外部フローではないため除外する。各フロー日は「その日以降の最初の snap_date」へ
#: 帰属させ、同日仕訳を flow_eop、それ以前(= 前の snap_date より後)を flow_bop へ
#: 振り分ける。``kind='pending'`` の行 = 系列最終日より後のフロー(未反映)。
#:
#: **形(2026-08-04 の書き換え — reminders navflow-equity-flow-query-rewrite)**。意味は
#: 変えていない。旧形は ``journal_lines`` を ``accounts`` に結合して
#: ``category='equity'`` に絞っていたため、プランナは結合前に「どの account_id が equity か」を
#: 知れず、``journal_lines`` 側の行数を平均選択度で見積もっていた(実測: 真値 13 行に対し
#: 見積り 6,107 行)。結果としてどんな索引を置いてもハッシュ結合+逐次走査になり、索引では
#: 直らないことが確定している(``migrations/0027_query_indexes.sql`` の「入れなかったもの」(B))。
#: 新形は対象科目を ``eq`` CTE で**先に確定**させ、``CROSS JOIN LATERAL`` で科目ごとに
#: ``journal_lines`` を引く。``ledger.accounts`` の主キーは ``(book_id, account_id)`` なので
#: 旧形の結合は 1:1 であり、科目ごとの LATERAL に分解しても行の重複も脱落も起きない
#: (``jl.book_id = %(book)s`` を等値で固定しているため、旧形の結合条件
#: ``a.book_id = jl.book_id`` は ``a.book_id = %(book)s`` と同値)。
#: **``OFFSET 0`` は必須の最適化フェンスである** — 外すとプランナが LATERAL を引き上げ、
#: 旧形と同じ「``journal_lines`` 全走査 + ハッシュ結合」に戻る(実測で確認済み)。
#: **科目リストはハードコードしない** — ``accounts`` が正であり、二重管理は帳簿の定義が
#: コード側にずれる典型経路である。新旧の結果一致は
#: ``tests/risk/test_navflow_query_rewrite.py`` が対照テストで固定している。
#:
#: 実測(0027 と同じ合成データ・中央値7回): 規模A は本 SQL 全体で 9.1 → 5.3 ms、
#: flow CTE 単体で実行 11.0 → 3.8 ms・共有バッファ 979 → 341(狙いどおり全走査が索引走査に
#: 変わった)。**規模B(明細 628,004 行)は 49.7 → 49.7 ms で横ばい**であり、共有バッファは
#: 9,637 → 3,286 に減ったものの支配項が ``journal_entries`` とのハッシュ結合へ移っただけである。
#: これは単一クエリの上限で、原因は同じ見積り誤差(``l.account_id = eq.account_id`` の右辺が
#: 実行時まで不明なので ``journal_lines`` 側が 209,335 行 = 真値 2 行と見積もられる)。
#: ネストループを強制する形(二段 LATERAL)は I/O は最適(共有バッファ 13)だが、同じ誤見積りが
#: 総コストを 1.5M に膨らませて JIT を起動するため実測は逆に遅い(規模B 57.9 ms /
#: ``jit=off`` なら 0.68 ms)。根治は科目リストを**クエリパラメータで渡す**ことだが
#: (実測 規模B 0.66 ms)、読み出しが 2 文になり下の「1 クエリ」性質(独立審査 中-6)に
#: 触れるため設計判断として ``ops/reminders.yaml`` の
#: ``navflow-equity-account-parameterization`` に分離した。
NAV_FLOW_SQL = """
WITH eq AS MATERIALIZED (
    SELECT a.account_id
    FROM ledger.accounts a
    WHERE a.book_id = %(book)s
      AND a.category = 'equity' AND a.account_id <> 'retained'
), flow AS (
    SELECT f.entry_date AS entry_date, sum(f.amount) AS amount
    FROM eq
    CROSS JOIN LATERAL (
        SELECT je.entry_date, l.credit - l.debit AS amount
        FROM ledger.journal_lines l
        JOIN ledger.journal_entries je ON je.entry_id = l.entry_id
        WHERE l.book_id = %(book)s AND l.account_id = eq.account_id
        OFFSET 0
    ) f
    GROUP BY f.entry_date
    HAVING sum(f.amount) <> 0
), attributed AS (
    SELECT f.entry_date, f.amount, (
               SELECT min(s.snap_date) FROM ledger.nav_snapshots s
               WHERE s.book_id = %(book)s AND s.snap_date >= f.entry_date
           ) AS snap_date
    FROM flow f
), per_snap AS (
    SELECT snap_date,
           coalesce(sum(amount) FILTER (WHERE entry_date = snap_date), 0) AS flow_eop,
           coalesce(sum(amount) FILTER (WHERE entry_date < snap_date), 0) AS flow_bop
    FROM attributed WHERE snap_date IS NOT NULL GROUP BY snap_date
)
SELECT 'snapshot' AS kind, s.snap_date AS day, s.nav, s.status,
       coalesce(p.flow_eop, 0) AS flow_eop, coalesce(p.flow_bop, 0) AS flow_bop,
       coalesce((s.detail ->> 'recon_invalidated')::boolean, false) AS recon_invalidated,
       (s.detail ->> 'recon_invalidated_by_run')::bigint AS recon_invalidated_by_run
FROM ledger.nav_snapshots s
LEFT JOIN per_snap p ON p.snap_date = s.snap_date
WHERE s.book_id = %(book)s
UNION ALL
SELECT 'pending', a.entry_date, NULL, NULL, a.amount, 0, false, NULL
FROM attributed a WHERE a.snap_date IS NULL
ORDER BY 1, 2
"""


@dataclass(frozen=True)
class NavFlowPoint:
    """スナップショット 1 点と、そこに帰属した外部フロー(BOP/EOP 分離)。"""

    day: date
    nav: Decimal
    status: str
    flow_eop: Decimal = Decimal(0)  # 当日仕訳(分子から引く)
    flow_bop: Decimal = Decimal(0)  # 前の測定日より後・当日より前(分母に足す)
    #: 再締めの訂正でその日の照合結論が無効化されたか(``ledger.closing`` が立てる)。
    #: True の日は ``status='confirmed'`` でも「照合済み NAV」として扱ってはならない。
    recon_invalidated: bool = False
    #: そのフラグを立てた/更新した再締めの run_id。通知の鮮度判定に使う(新-2)。
    recon_invalidated_by_run: int | None = None

    @property
    def net_flow(self) -> Decimal:
        """帰属フロー純額(表示用)。測定は BOP/EOP を分けて使う。"""
        return self.flow_eop + self.flow_bop


@dataclass(frozen=True)
class PendingFlow:
    """スナップショットがまだ無い日の外部フロー(次の締めで系列に載る)。"""

    entry_date: date
    amount: Decimal  # 出資 +・払戻 −(JPY)


@dataclass(frozen=True)
class NavFlowData:
    """NAV 点(フロー帰属済み)と未反映フロー。1 クエリの結果を 2 つに分けたもの。"""

    points: list[NavFlowPoint] = field(default_factory=list)
    pending: tuple[PendingFlow, ...] = ()


def load_nav_flow_data(conn: psycopg.Connection, book_id: str) -> NavFlowData:
    """帳簿の NAV 点(日付昇順・フロー帰属済み)と未反映フローを 1 クエリで読む。"""
    with conn.cursor() as cur:
        cur.execute(NAV_FLOW_SQL, {"book": book_id})
        rows: list[tuple[Any, ...]] = cur.fetchall()
    points: list[NavFlowPoint] = []
    pending: list[PendingFlow] = []
    for (
        kind, day, nav, status, flow_eop, flow_bop, recon_invalidated, recon_run
    ) in rows:
        if kind == "snapshot":
            points.append(
                NavFlowPoint(
                    day=day,
                    nav=Decimal(nav),
                    status=status,
                    flow_eop=Decimal(flow_eop),
                    flow_bop=Decimal(flow_bop),
                    recon_invalidated=bool(recon_invalidated),
                    recon_invalidated_by_run=(
                        None if recon_run is None else int(recon_run)
                    ),
                )
            )
        else:  # pending: 金額は flow_eop 列に載せてある(UNION の列合わせ)
            pending.append(PendingFlow(entry_date=day, amount=Decimal(flow_eop)))
    return NavFlowData(points=points, pending=tuple(pending))


def pending_flows_note(pending: Sequence[PendingFlow]) -> str | None:
    """未反映フローの注記文(無ければ None)。文言はレポートと UI で共有する。"""
    if not pending:
        return None
    items = " / ".join(
        f"{p.entry_date} {'出資' if p.amount > 0 else '払戻'} ¥{abs(p.amount):,.0f}"
        for p in pending
    )
    return (
        f"NAV スナップショット未生成の外部フロー {len(pending)} 件: "
        f"{items} — 次の会計締めまでリターン測定に反映されない"
    )


def recon_invalidated_days(
    points: Sequence[NavFlowPoint], *, by_run: int | None = None
) -> list[date]:
    """照合結論が無効化された日(日付昇順)。

    再締めが**建玉を動かす遅延仕訳**を取り込んだ日は、その日の建玉が締め時点の照合
    対象と違う。``status='confirmed'`` の見た目に反して照合は無効である(独立審査
    再-2 に対する設計リード裁定)。拠出資本の入出金だけが遅れた日は建玉を動かさない
    ためここには現れない。

    ``by_run`` を渡すと**その run の再締めで立った/更新された日**に絞る。フラグ自体は
    証憑として不可逆だが、通知まで不可逆にすると「一度でも遅延約定が起きたら日次
    レポートが永久に urgent」になる(中-5 で是正済みの欠陥の再発 — 独立審査 新-2)。
    鮮度で絞るのは通知側の責務であり、記録は消さない。
    """
    return [
        p.day
        for p in points
        if p.recon_invalidated
        and (by_run is None or p.recon_invalidated_by_run == by_run)
    ]


def recon_invalidated_note(days: Sequence[date], *, fresh: bool) -> str | None:
    """照合無効日の注記文(無ければ None)。文言はレポートと UI で共有する。

    ``fresh=True``(当該再締めで立った/更新された日)は日付を名指しして urgent の理由に
    する。``fresh=False``(過去に立った既知のフラグ)は件数のみ — 既知の事実を毎日
    同じ強度で鳴らさない。
    """
    if not days:
        return None
    if not fresh:
        return (
            f"照合結論が無効化された日 {len(days)} 件(既知 — 直近 {days[-1]})。"
            "当該日の status は照合済みを意味しない"
        )
    shown = " / ".join(str(d) for d in days[-8:])
    return (
        f"再締めで照合結論が新たに無効化された日 {len(days)} 件: {shown}"
        " — 建玉を動かす遅延仕訳を取り込んだため、その日の status は照合済みを"
        "意味しない(建玉明細も無効化済み)"
    )


def urgent_pending(
    pending: Sequence[PendingFlow], nav: Decimal | None, *, as_of_day: date
) -> tuple[bool, str | None]:
    """未反映フローを urgent で上げるか(決定論ルール — 独立審査 中-5)。

    pending は次の締めで自然に解消するのが正常系であり、当日中の未反映まで毎日
    urgent にすると「毎日赤」で通知の意味が失われる(IBCS の色規約とも衝突)。
    そこで**材料性のあるものだけ**を上げる:

    1. **締めを 1 回跨いだ**: ``entry_date < as_of_day``。仕訳日当日の締めはまだ
       走っていない可能性があるが、翌測定日でも未反映なら締めが飛んでいる
    2. **NAV 比 0.5% 以上**: 当日中でも規模が材料的なら上げる。NAV が測れない
       (系列なし)場合は判断材料が無いため「材料性あり」と扱う(fail-safe)

    しきい値 0.5% は IPS の判定値ではなく通知の材料性基準(このモジュールの規約)。
    """
    if not pending:
        return False, None
    stale = [p for p in pending if p.entry_date < as_of_day]
    if stale:
        return True, f"締めを跨いだ未反映フロー {len(stale)} 件(仕訳日 < 測定日)"
    if nav is None or nav <= 0:
        return True, "NAV 未測定のため材料性を判定できない(fail-safe)"
    material = [p for p in pending if abs(p.amount) / nav >= PENDING_MATERIALITY_NAV]
    if material:
        return True, (
            f"未反映フローが NAV 比 {float(PENDING_MATERIALITY_NAV):.1%} 以上"
            f"({len(material)} 件)"
        )
    return False, None


__all__ = [
    "NAV_FLOW_SQL",
    "PENDING_MATERIALITY_NAV",
    "NavFlowData",
    "NavFlowPoint",
    "PendingFlow",
    "load_nav_flow_data",
    "pending_flows_note",
    "recon_invalidated_days",
    "recon_invalidated_note",
    "urgent_pending",
]
