"""Kill Switch(§5「Kill Switch」)。

``/kill``(オーナーのみ）で ``ops.flags`` の ``kill_switch`` を立て、全発注経路が参照する。
復帰は ``/resume`` +確認ボタンの2段階(確認は View 側、本モジュールは ``release`` で遷移を実行)。

現在値は ``ops.flags``、遷移履歴は追記オンリーの ``ops.flag_events``(監査証跡)に残す。
discord.py には依存しない。オーナー検証は ``approvals.is_owner`` を再利用する。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from ryza.bot import KILL_SWITCH
from ryza.bot.approvals import NotOwnerError, is_owner


@dataclass(frozen=True)
class FlagState:
    """フラグの現在状態。"""

    name: str
    enabled: bool
    reason: str | None
    updated_by: str


def get_flag(conn: psycopg.Connection, name: str = KILL_SWITCH) -> bool:
    """フラグの現在値。未設定(行なし)は False とみなす。"""
    with conn.cursor() as cur:
        cur.execute("SELECT enabled FROM ops.flags WHERE name = %s", (name,))
        row = cur.fetchone()
        return bool(row[0]) if row else False


def is_engaged(conn: psycopg.Connection) -> bool:
    """Kill Switch が有効か。発注経路はこれを参照して停止する。"""
    return get_flag(conn, KILL_SWITCH)


def _set_flag(
    conn: psycopg.Connection,
    name: str,
    enabled: bool,
    actor: str,
    reason: str | None,
) -> FlagState:
    """フラグを upsert し、遷移を ``ops.flag_events`` に追記する(呼び出し側が commit）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.flags (name, enabled, reason, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (name) DO UPDATE
            SET enabled = EXCLUDED.enabled,
                reason = EXCLUDED.reason,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (name, enabled, reason, str(actor)),
        )
        cur.execute(
            """
            INSERT INTO ops.flag_events (name, enabled, reason, actor)
            VALUES (%s, %s, %s, %s)
            """,
            (name, enabled, reason, str(actor)),
        )
    return FlagState(name=name, enabled=enabled, reason=reason, updated_by=str(actor))


def engage(
    conn: psycopg.Connection,
    actor: str,
    owner_ids: Iterable[str],
    *,
    reason: str | None = None,
) -> FlagState:
    """Kill Switch を有効化(``/kill``)。オーナーのみ。安全側なので即時に立てる。"""
    if not is_owner(actor, owner_ids):
        raise NotOwnerError(f"非オーナーの /kill を拒否: user={actor}")
    return _set_flag(conn, KILL_SWITCH, True, actor, reason)


def release(
    conn: psycopg.Connection,
    actor: str,
    owner_ids: Iterable[str],
    *,
    confirmed: bool = False,
    reason: str | None = None,
) -> FlagState:
    """Kill Switch を解除(``/resume``)。オーナーのみ+確認必須(2段階の2段目)。

    ``confirmed`` が False の場合は遷移させず ``PermissionError`` を送出する
    (確認ボタンを押していない誤操作の防止)。復帰は事故を招きうるため kill と非対称。
    """
    if not is_owner(actor, owner_ids):
        raise NotOwnerError(f"非オーナーの /resume を拒否: user={actor}")
    if not confirmed:
        raise PermissionError("/resume は確認ボタンによる2段階確認が必要")
    return _set_flag(conn, KILL_SWITCH, False, actor, reason)
