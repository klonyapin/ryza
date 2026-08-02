"""Discord チャンネルの ensure と解決記録(§1 改訂・2026-08-03)。

4つの論理チャンネル(press|approval|ops|dev)を指定カテゴリ配下に配置する。Bot が起動時に
カテゴリの子チャンネルを走査し、論理名に対応する表示名(報道|承認|運営|dev)があれば**再利用**、
無ければ**自動作成**(ensure)する。解決した ``logical → channel_id`` は ``ops.discord_channels``
に記録し、以後の配送はこの表を引く(手動リネームにも起動ごとの再 ensure で追従)。

Discord API(チャンネル作成)には依存しない。ここでは「どの論理チャンネルを再利用/新規作成すべきか」
の**純粋な計画(plan_ensure)**と DB への記録・解決のみを扱い、実作成は ``main`` が担う。
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ryza.bot import CHANNEL_NAMES


@dataclass(frozen=True)
class ChannelPlan:
    """1論理チャンネルの ensure 計画。"""

    logical: str
    channel_name: str
    action: str            # 'reuse' | 'create'
    channel_id: str | None  # reuse のとき既存 ID、create のとき None


def plan_ensure(existing_by_name: dict[str, str]) -> list[ChannelPlan]:
    """カテゴリ内の既存チャンネル(表示名→ID)から各論理チャンネルの計画を立てる。

    ``existing_by_name`` は対象カテゴリ配下のテキストチャンネルのみを渡すこと
    (別カテゴリの同名チャンネルを誤って再利用しないため)。
    """
    plans: list[ChannelPlan] = []
    for logical, name in CHANNEL_NAMES.items():
        if name in existing_by_name:
            plans.append(ChannelPlan(logical, name, "reuse", existing_by_name[name]))
        else:
            plans.append(ChannelPlan(logical, name, "create", None))
    return plans


def record_channel(
    conn: psycopg.Connection,
    logical: str,
    channel_name: str,
    channel_id: str,
    category_id: str,
) -> None:
    """論理チャンネルの解決結果を ``ops.discord_channels`` に upsert する(呼び出し側が commit)。"""
    if logical not in CHANNEL_NAMES:
        raise ValueError(f"未知の論理チャンネル: {logical}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.discord_channels
                (logical, channel_name, channel_id, category_id, resolved_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (logical) DO UPDATE
            SET channel_name = EXCLUDED.channel_name,
                channel_id = EXCLUDED.channel_id,
                category_id = EXCLUDED.category_id,
                resolved_at = now()
            """,
            (logical, channel_name, str(channel_id), str(category_id)),
        )


def resolve(conn: psycopg.Connection, logical: str) -> str | None:
    """論理チャンネルの現在の Discord チャンネル ID。未解決なら None。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel_id FROM ops.discord_channels WHERE logical = %s", (logical,)
        )
        row = cur.fetchone()
        return row[0] if row else None
