"""state — ``risk.limits_state`` の更新と dd_hard の委員会解除(T-015。保護領域)。

エンジン更新(``upsert_limits_state``)は dd_hard を **OR ラッチ**で書く: 一度立った
dd_hard は測定値が下がっても消えない(IPS §3.2「復帰は委員会の明示操作のみ」)。
ラッチは SQL(``ON CONFLICT`` の OR)と 0015 のトリガ(true→false 禁止)の二重底。

解除(``release_dd_hard``)は委員会の明示操作専用。トランザクション局所 GUC
``ryza.dd_hard_release`` を解除キーとして立ててから UPDATE し、actor・reason 必須の
イベントを台帳(``risk.limits_state_events``)に残す。**自動では決して呼ばれない**
(リポジトリ内で呼ぶのは committee 操作経路とテストのみ)。
"""

from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from ryza.risk.engine import RiskState

# 0015 の risk.guard_limits_state と対の解除キー GUC 名。
_RELEASE_GUC = "ryza.dd_hard_release"


def _record_event(
    conn: psycopg.Connection,
    *,
    book_id: str,
    event: str,
    flags: tuple[bool, bool, bool, bool],
    metrics: dict,
    actor: str,
    reason: str | None,
    as_of: datetime,
    run_id: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk.limits_state_events
                (book_id, event, dd_soft, dd_hard, vol_exceeded, es_exceeded,
                 metrics, actor, reason, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (book_id, event, *flags, Jsonb(metrics), actor, reason, as_of, run_id),
        )


def state_metrics(state: RiskState, extra: dict | None = None) -> dict:
    """イベント台帳・レポートに残す測定値スナップショット(監査再現性)。

    ``extra`` は測定の**前提**に関する追加キー(例: 測定窓に含まれた照合無効日の数)。
    フラグがどんな系列の上で立ったかを後から再現できるようにするためのもので
    (不変原則3)、判定そのものには使わない。
    """
    return {
        "as_of_day": state.as_of_day.isoformat(),
        "nav": str(state.nav),
        "peak_nav": str(state.peak_nav),
        "drawdown": str(state.drawdown),
        "n_returns": state.n_returns,
        "sufficient": state.sufficient,
        "ewma_vol_annual": state.ewma_vol_annual,
        "es95_historical": state.es95.historical,
        "es95_parametric": state.es95.parametric,
        "es95_adopted": state.es95.adopted,
        "es95_n_obs": state.es95.n_obs,
        "measured_dd_hard": state.dd_hard,
        "notes": list(state.notes),
        **(extra or {}),
    }


def upsert_limits_state(
    conn: psycopg.Connection,
    book_id: str,
    state: RiskState,
    *,
    as_of: datetime,
    run_id: int,
    actor: str = "risk.daily",
    extra_metrics: dict | None = None,
) -> dict[str, bool]:
    """エンジン測定値で ``risk.limits_state`` を更新し、実効フラグを返す(冪等)。

    dd_hard は OR ラッチ(既存 true は保持 — 解除は ``release_dd_hard`` のみ)。
    同日再実行は同じ行を上書きし、イベント台帳に追記が1件増えるだけ(状態は不変)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk.limits_state
                (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (book_id) DO UPDATE SET
                dd_soft      = EXCLUDED.dd_soft,
                dd_hard      = risk.limits_state.dd_hard OR EXCLUDED.dd_hard,
                vol_exceeded = EXCLUDED.vol_exceeded,
                es_exceeded  = EXCLUDED.es_exceeded,
                as_of        = EXCLUDED.as_of,
                run_id       = EXCLUDED.run_id
            RETURNING dd_soft, dd_hard, vol_exceeded, es_exceeded
            """,
            (
                book_id,
                state.dd_soft,
                state.dd_hard,
                state.vol_exceeded,
                state.es_exceeded,
                as_of,
                run_id,
            ),
        )
        row = cur.fetchone()
    flags = (row[0], row[1], row[2], row[3])
    _record_event(
        conn,
        book_id=book_id,
        event="engine_update",
        flags=flags,
        metrics=state_metrics(state, extra_metrics),
        actor=actor,
        reason=None,
        as_of=as_of,
        run_id=run_id,
    )
    return {
        "dd_soft": flags[0],
        "dd_hard": flags[1],
        "vol_exceeded": flags[2],
        "es_exceeded": flags[3],
    }


def release_dd_hard(
    conn: psycopg.Connection,
    book_id: str,
    *,
    actor: str,
    reason: str,
    run_id: int,
    as_of: datetime | None = None,
) -> bool:
    """dd_hard を解除する(委員会の明示操作専用 — IPS §3.2 復帰条項)。

    actor・reason は必須。解除キー GUC を立てて 0015 のトリガを通し、台帳に
    ``dd_hard_release`` イベントを追記する。dd_hard が立っていなければ False。
    **このリポジトリのどのジョブもこれを自動では呼ばない。**
    """
    if not actor.strip() or not reason.strip():
        raise ValueError("dd_hard の解除には actor と reason が必須(IPS §3.2 復帰条項)")
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        now = cur.fetchone()[0]
        as_of = as_of or now
        # 解除キー(トランザクション局所)。トリガ risk.guard_limits_state が照合する。
        cur.execute("SELECT set_config(%s, %s, true)", (_RELEASE_GUC, book_id))
        try:
            cur.execute(
                """
                UPDATE risk.limits_state
                SET dd_hard = false, as_of = %s, run_id = %s
                WHERE book_id = %s AND dd_hard
                RETURNING dd_soft, vol_exceeded, es_exceeded
                """,
                (as_of, run_id, book_id),
            )
            row = cur.fetchone()
        finally:
            # 同一トランザクション内の後続 UPDATE に解除キーが漏れないよう即時撤去。
            cur.execute("SELECT set_config(%s, '', true)", (_RELEASE_GUC,))
    if row is None:
        return False
    _record_event(
        conn,
        book_id=book_id,
        event="dd_hard_release",
        flags=(row[0], False, row[1], row[2]),
        metrics={},
        actor=actor,
        reason=reason,
        as_of=as_of,
        run_id=run_id,
    )
    return True


__all__ = ["release_dd_hard", "state_metrics", "upsert_limits_state"]
