"""daily — リスクエンジンの日次サイクル(T-015。保護領域 — 定款第5条)。

帳簿ごとに: NAV 系列(``ledger.nav_snapshots``)とフロー(出資勘定)を読み、
現在ポジション(``trading.positions``)を時価評価し、``engine.evaluate`` で
測定 → ``risk.limits_state`` を更新(dd_hard は OR ラッチ)→ 日次リスクレポートを
``#運営``(ops)outbox へ 1 通 enqueue する。フラグが立っていれば urgent。

NAV 系列の出所(指示書の設計判断): ``ledger.nav_snapshots``(T-002/0005)が既存の
ためこれを正とし、新テーブルは作らない。日次の会計締め(``ledger.closing``)が
snap_date ごとに upsert する系列をそのまま使う(provisional も測定に使う —
「測れる最新値で測る」。締めが走らない日は系列に穴が空くが、リターンは隣接
スナップショット間で計算されるため測定は継続する)。
※ T-016 統合(設計リード裁定 2026-08-03): 執行層の締め(``execution/close.py``)が
同じ NAV を ``risk.nav_daily``(執行照合の status を重ねた risk 用ビュー)にも
書くが、本モジュールの読み出しは引き続き nav_snapshots(正)のまま。

外部フロー調整: 出資・払戻は ``ledger.accounts.category='equity'`` かつ
``account_id <> 'retained'``(拠出資本勘定)への仕訳から日次合算する。損益の
振替(retained)はフローに含めない。**フローの帰属日は「その日以降の最初の
snap_date」で、当日仕訳(EOP)と区間内仕訳(BOP)に分ける**(``ryza.risk.navflow``
— 休日に付いたフローの取りこぼしと、期末仮定によるリターン増幅の是正。独立審査
T-018 重要-5・重要-1)。まだスナップショットの無いフローは ``NavSeries.pending_flows``
として返し、レポートに独立フィールドで注記する(黙って落とさない)。

CLI: ``python -m ryza.risk.daily``(冪等 — 同日再実行は limits_state を同値上書きし、
イベント台帳とレポートが 1 件ずつ増えるのみ)。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from ryza.bot import COLOR_FLASH, COLOR_NORMAL, DISCLAIMER
from ryza.bot.outbox import enqueue
from ryza.db.conn import connect
from ryza.ips import IPSConfig
from ryza.provenance import Run, start_run
from ryza.risk import engine
from ryza.risk.classify import classify_current_instruments
from ryza.risk.navflow import (
    NavFlowPoint,
    PendingFlow,
    load_nav_flow_data,
    pending_flows_note,
    recon_invalidated_days,
    recon_invalidated_note,
    urgent_pending,
)
from ryza.risk.state import upsert_limits_state

_JST = ZoneInfo("Asia/Tokyo")

# 銘柄リターン系列の遡り日数(暦日)。ES のヒストリカル法「直近1年の日次リターン」
# (指示書)を営業日で確保するための読出し窓。判定値ではなく読出し規約。
_RETURN_LOOKBACK_DAYS = 400


# ── DB 読出し ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class NavSeries:
    """NAV 系列(フロー調整済み)と、まだスナップショットの無い未反映フロー。

    ``pending_flows`` を返り値に持たせているのは、呼び出し側が未反映フローの存在を
    **検知できる形**にするため(独立審査 T-018 重要-5 の裁定「黙って落とさない」)。
    """

    points: list[engine.NavPoint]
    pending_flows: tuple[PendingFlow, ...] = ()
    #: navflow の生の点。照合無効フラグ(``recon_invalidated``)とそれを立てた run を
    #: 呼び出し側が読むために保持する — 同じ 1 クエリの結果で、追加の読み出しはしない。
    flow_points: tuple[NavFlowPoint, ...] = ()

    def __bool__(self) -> bool:  # 「系列があるか」の判定は点の有無で行う
        return bool(self.points)


def load_nav_series(conn: psycopg.Connection, book_id: str) -> NavSeries:
    """帳簿の NAV 系列(日付昇順)+外部フロー(拠出資本勘定の日次純額)。

    フローは ``ryza.risk.navflow`` の規約で「その日以降の最初の snap_date」へ寄せ、
    当日仕訳を ``flow_eop``・区間内仕訳を ``flow_bop`` に分ける(重要-5・重要-1)。
    系列最終日より後のフローは点にできないため ``pending_flows`` として別に返す。
    """
    data = load_nav_flow_data(conn, book_id)
    return NavSeries(
        points=[
            engine.NavPoint(
                day=p.day, nav=p.nav, flow_eop=p.flow_eop, flow_bop=p.flow_bop
            )
            for p in data.points
        ],
        pending_flows=data.pending,
        flow_points=tuple(data.points),
    )


def load_positions(
    conn: psycopg.Connection, book_id: str, *, as_of: datetime
) -> tuple[list[engine.RiskPosition], list[str]]:
    """帳簿の現在ポジション(全ポッド合算・銘柄単位)を時価評価する。

    時価は ``market.bars``(1d)の **as_of 以前の**最新終値×現行銘柄の乗数
    (point-in-time — 不変原則4。過去日付での再実行に未来バーを混入させない)。
    時価の無い銘柄は評価から除外し notes に明記する(fail-safe: 落とさず測れる
    範囲で測り、欠測は隠さない。発注時の欠測はゲート側が fail-closed で block する)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.instrument_id, p.asset_class, sum(p.qty)
            FROM trading.positions p
            WHERE p.book_id = %s
            GROUP BY p.instrument_id, p.asset_class
            HAVING sum(p.qty) <> 0
            """,
            (book_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return [], []
        ids = [r[0] for r in rows]
        cur.execute(
            """
            SELECT DISTINCT ON (instrument_id) instrument_id, close
            FROM market.bars
            WHERE instrument_id = ANY(%s) AND timeframe = '1d' AND close IS NOT NULL
              AND ts <= %s
            ORDER BY instrument_id, ts DESC, as_of DESC
            """,
            (ids, as_of),
        )
        prices = {r[0]: Decimal(r[1]) for r in cur.fetchall()}
        cur.execute(
            """
            SELECT DISTINCT ON (instrument_id) instrument_id, multiplier
            FROM market.instruments
            WHERE instrument_id = ANY(%s)
            ORDER BY instrument_id, valid_from DESC
            """,
            (ids,),
        )
        multipliers = {r[0]: Decimal(r[1]) for r in cur.fetchall()}

    positions: list[engine.RiskPosition] = []
    notes: list[str] = []
    for instrument_id, asset_class, qty in rows:
        price = prices.get(instrument_id)
        if price is None:
            notes.append(f"時価欠落のため評価除外: instrument {instrument_id}")
            continue
        value = Decimal(qty) * price * multipliers.get(instrument_id, Decimal(1))
        positions.append(
            engine.RiskPosition(
                instrument_id=instrument_id, asset_class=asset_class, value=value
            )
        )
    return positions, notes


def load_instrument_returns(
    conn: psycopg.Connection, instrument_ids: list[int], *, as_of: datetime
) -> dict[int, dict[Any, float]]:
    """保有銘柄の日次リターン系列(直近1年 — ES ヒストリカル法の入力)。"""
    if not instrument_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT instrument_id, ts, close FROM (
                SELECT DISTINCT ON (instrument_id, ts)
                       instrument_id, ts, close
                FROM market.bars
                WHERE instrument_id = ANY(%s) AND timeframe = '1d'
                  AND close IS NOT NULL
                  AND ts >= %s - make_interval(days => %s)
                  AND ts <= %s
                ORDER BY instrument_id, ts, as_of DESC
            ) b ORDER BY instrument_id, ts
            """,
            (instrument_ids, as_of, _RETURN_LOOKBACK_DAYS, as_of),
        )
        rows = cur.fetchall()
    closes: dict[int, list[tuple[Any, Decimal]]] = {}
    for instrument_id, ts, close in rows:
        closes.setdefault(instrument_id, []).append(
            (ts.astimezone(_JST).date(), Decimal(close))
        )
    returns: dict[int, dict[Any, float]] = {}
    for instrument_id, series in closes.items():
        rets: dict[Any, float] = {}
        for (_, prev), (day, cur_close) in zip(series, series[1:], strict=False):
            if prev > 0:
                rets[day] = float((cur_close - prev) / prev)
        returns[instrument_id] = rets
    return returns


def _load_cash(conn: psycopg.Connection, book_id: str) -> Decimal | None:
    """現金残高(cash 勘定の借方残)。ガードレール消費率レポート用。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sum(debit - credit) FROM ledger.journal_lines
            WHERE book_id = %s AND account_id = 'cash'
            """,
            (book_id,),
        )
        row = cur.fetchone()
    return None if row is None or row[0] is None else Decimal(row[0])


# ── レポート ──────────────────────────────────────────────────────────────────
def _pct(v: float | Decimal | None, digits: int = 2) -> str:
    return "—" if v is None else f"{float(v) * 100:.{digits}f}%"


def build_risk_embed(
    book_id: str,
    state: engine.RiskState,
    effective: dict[str, bool],
    usage: dict[str, dict],
    ips: IPSConfig,
    *,
    as_of: datetime,
    pending_note: str | None = None,
    recon_note: str | None = None,
) -> dict[str, Any]:
    """日次リスクレポートの embed(00 §9「リスクレポート」— #運営 へ 1 通)。

    未反映フローの注記(``pending_note``)と照合無効の注記(``recon_note``)は
    **独立フィールド**にする。注記欄は 1024 字で切り詰めるため、注記が多い日に
    「urgent だけ立って理由が消える」ことが起きうる(独立審査 重要-4)。測定に入って
    いない入出金と、照合が無効になった日の存在は切ってはならない。
    """
    hl = ips.hard_limits
    flagged = [name for name, on in effective.items() if on]
    fields: list[dict[str, Any]] = [
        {
            "name": "DD(設定来ピーク比)",
            "value": (
                f"{_pct(state.drawdown)}(NAV ¥{state.nav:,.0f} / ピーク ¥{state.peak_nav:,.0f})"
                f" — soft {_pct(hl.dd_soft_limit, 0)} / hard {_pct(hl.dd_hard_limit, 0)}"
            ),
            "inline": False,
        },
        {
            "name": "実現ボラ(EWMA 年率)",
            "value": f"{_pct(state.ewma_vol_annual)} — 上限 {_pct(hl.realized_vol_limit, 0)}",
            "inline": True,
        },
        {
            "name": "日次 ES95(NAV 比)",
            "value": (
                f"{_pct(state.es95.adopted)}(hist {_pct(state.es95.historical)} / "
                f"param {_pct(state.es95.parametric)})— 上限 {_pct(hl.daily_es95_nav_max, 0)}"
            ),
            "inline": True,
        },
    ]
    if usage:
        lines = [
            f"集中度 {_pct(usage['issuer_concentration']['value'])}"
            f"/{_pct(usage['issuer_concentration']['limit'], 0)}",
            f"資産クラス({usage['single_asset_class_gross'].get('class', '-')}) "
            f"{_pct(usage['single_asset_class_gross']['value'])}"
            f"/{_pct(usage['single_asset_class_gross']['limit'], 0)}",
            f"レバ {usage['gross_leverage']['value']:.2f}x/{usage['gross_leverage']['limit']:.1f}x",
            f"現金 {_pct(usage['cash_floor']['value'])}"
            f"(下限 {_pct(usage['cash_floor']['limit'], 0)})",
        ]
        fields.append(
            {"name": "ガードレール消費", "value": "\n".join(lines), "inline": False}
        )
    fields.append(
        {
            "name": "執行フラグ(risk.limits_state)",
            "value": ("⛔ " + ", ".join(flagged)) if flagged else "✅ なし",
            "inline": False,
        }
    )
    if pending_note:
        fields.append(
            {"name": "未反映フロー", "value": f"【要確認】{pending_note}"[:1024], "inline": False}
        )
    if recon_note:
        fields.append(
            {"name": "照合無効", "value": f"【要確認】{recon_note}"[:1024], "inline": False}
        )
    if state.notes:
        fields.append(
            {"name": "注記", "value": "\n".join(state.notes)[:1024], "inline": False}
        )
    return {
        "title": f"リスクレポート {book_id} {as_of.astimezone(_JST).strftime('%Y-%m-%d')}",
        "description": (
            f"IPS v{ips.version} §3.2 ハードリミットの日次測定"
            f"(データ {state.n_returns} 営業日 / 測定 as_of {state.as_of_day})。"
        ),
        "color": COLOR_FLASH if (flagged or recon_note) else COLOR_NORMAL,
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


# ── 日次サイクル本体 ──────────────────────────────────────────────────────────
def run_risk_daily(
    conn: psycopg.Connection,
    run: Run,
    *,
    ips: IPSConfig | None = None,
    as_of: datetime | None = None,
    channel_ops: str = "ops",
) -> dict[str, Any]:
    """リスクエンジンの日次サイクルを 1 回実行する(帳簿は ``ips.books`` 全件)。

    NAV 系列がまだ無い帳簿は limits_state を**作らない**(未測定を「リスク OK」と
    主張しない — T-014 判断11 と同じ姿勢。ゲートは行欠落を fail-closed で block)。
    レポートにはその旨を明記する。

    スナップショット未生成の外部フローがある帳簿は、レポートに独立フィールドで必ず
    注記する。urgent にするのは材料性のあるもの(締めを跨いだ/NAV 比 0.5% 以上)に
    限る — ``navflow.urgent_pending``(重要-4・中-5 の裁定)。
    """
    ips = ips or IPSConfig.load()
    as_of = as_of or datetime.now(UTC)
    detail: dict[str, Any] = {
        "classification": classify_current_instruments(conn, run_id=run.run_id)
    }
    as_of_day = as_of.astimezone(_JST).date()
    for book_id in ips.books:
        loaded = load_nav_series(conn, book_id)
        series = loaded.points
        # 未反映フロー(スナップショット未生成)は測定に入らない。黙って落とさず注記する。
        pending_note = pending_flows_note(loaded.pending_flows)
        pending_urgent, pending_reason = urgent_pending(
            loaded.pending_flows,
            series[-1].nav if series else None,
            as_of_day=as_of_day,
        )
        if pending_note and pending_reason:
            pending_note = f"{pending_note}。urgent 理由: {pending_reason}"
        if not series:
            embed = {
                "title": (
                    f"リスクレポート {book_id} {as_of.astimezone(_JST).strftime('%Y-%m-%d')}"
                ),
                "description": (
                    "NAV 系列なし(会計締め未実行)。リスク状態は未測定のため "
                    "risk.limits_state は作成しない(fail-closed — 発注はゲートが block)。"
                    + (f"\n【要確認】{pending_note}" if pending_note else "")
                ),
                "color": COLOR_FLASH,
                "fields": [],
                "footer": {"text": DISCLAIMER},
            }
            oid = enqueue(conn, channel_ops, embed, run.run_id, urgent=True)
            detail[book_id] = {
                "status": "no_nav",
                "pending_flows": len(loaded.pending_flows),
                "report_outbox_id": oid,
            }
            continue
        positions, notes = load_positions(conn, book_id, as_of=as_of)
        returns = load_instrument_returns(
            conn, [p.instrument_id for p in positions], as_of=as_of
        )
        # 照合無効(独立審査 再-2)。フラグ自体は不可逆だが、urgent にするのは**当該
        # 再締めで新たに立った/更新された日**だけ(新-2 — 中-5 と同じ材料性の規律)。
        # 既知分は notes に件数だけ残し、毎日 urgent にはしない。
        fresh_recon = recon_invalidated_days(loaded.flow_points, by_run=run.run_id)
        all_recon = recon_invalidated_days(loaded.flow_points)
        recon_note = recon_invalidated_note(fresh_recon or all_recon, fresh=bool(fresh_recon))
        if recon_note:
            notes.insert(0, ("【要確認】" if fresh_recon else "") + recon_note)
        if pending_note:
            # 先頭に挿す(重要-4): notes は 1024 字で切られるため、測定に入っていない
            # 入出金の存在が末尾で消えないようにする。embed 側は独立フィールドで二重化。
            notes.insert(0, f"【要確認】{pending_note}")
        state = engine.evaluate(series, positions, returns, ips, extra_notes=notes)
        # 測定窓に照合無効日が何日入ったかを metrics に残す(不変原則3 — 独立審査の
        # 要求)。汚染系列で vol_exceeded が立った事実を事後監査で再現できるようにする。
        # 窓は EWMA 実現ボラの営業日数 +1 点(その区間のリターンを作る NAV 点の数)。
        window = ips.hard_limits.realized_vol_ewma_days + 1
        window_days = {p.day for p in loaded.flow_points[-window:]}
        effective = upsert_limits_state(
            conn, book_id, state, as_of=as_of, run_id=run.run_id,
            extra_metrics={
                "recon_invalidated_days_in_window": len(
                    window_days.intersection(all_recon)
                ),
                "recon_invalidated_days_total": len(all_recon),
            },
        )
        usage = engine.guardrail_usage(
            positions, state.nav, _load_cash(conn, book_id), ips
        )
        embed = build_risk_embed(
            book_id, state, effective, usage, ips, as_of=as_of,
            pending_note=pending_note,
            recon_note=recon_note if fresh_recon else None,
        )
        # urgent: フラグ到達に加え、保有ありの ES 測定空白(判定保留)と、材料性のある
        # 未反映フロー(navflow.urgent_pending)、そして**当該再締めで新たに**照合結論が
        # 無効化された日(breaks 相当 — 独立審査 再-2 の裁定)も要確認として上げる。
        oid = enqueue(
            conn,
            channel_ops,
            embed,
            run.run_id,
            urgent=(
                any(effective.values())
                or state.es95.deferred
                or pending_urgent
                or bool(fresh_recon)
            ),
        )
        detail[book_id] = {
            "status": "measured",
            "pending_flows": len(loaded.pending_flows),
            "pending_urgent": pending_urgent,
            "recon_invalidated_new": len(fresh_recon),
            "recon_invalidated_total": len(all_recon),
            "drawdown": str(state.drawdown),
            "ewma_vol": state.ewma_vol_annual,
            "es95": state.es95.adopted,
            "flags": effective,
            "n_returns": state.n_returns,
            "report_outbox_id": oid,
        }
    return detail


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 実行パス
    """CLI: ``uv run python -m ryza.risk.daily``(決定論 — LLM・実ネットワーク不使用)。"""
    parser = argparse.ArgumentParser(description="Ryza リスクエンジン日次サイクル(T-015)")
    parser.parse_args(argv)
    run = start_run("risk.daily", {})
    conn = connect()
    try:
        with conn.transaction():
            detail = run_risk_daily(conn, run)
        run.finish("success")
    except Exception:
        run.finish("failed")
        raise
    finally:
        conn.close()
    for book, res in detail.items():
        print(f"{book}: {res}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
