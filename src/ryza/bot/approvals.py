"""承認 UI(§5「承認 UI」)。

提案(PR・戦略昇格・ブレーカー復帰・予算)を ``#承認`` にボタン付き embed で投稿し、
承認/却下/質問の押下を ``governance.decisions`` に記録する。**押下者がオーナー ID か検証**し、
非オーナーの操作は拒否する。1提案=1決定(``proposal_ref`` の UNIQUE)で二度押しを弾く。

discord.py には依存しない。ボタン View は ``main`` が組み立て、押下時に本モジュールの
``record_decision`` を呼ぶ。ここでは embed 組立・オーナー検証・DB 記録のみを扱う。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from ryza.bot import COLOR_APPROVAL, DISCLAIMER

# 決定の種別(governance.decisions.decision)と提案種別(kind）
DECISIONS = ("approve", "reject", "question")
KINDS = ("pr", "strategy_promotion", "breaker_resume", "budget", "other")


class NotOwnerError(PermissionError):
    """オーナー以外が承認操作を試みた。"""


@dataclass(frozen=True)
class Decision:
    """記録された決定。"""

    id: int
    proposal_ref: str
    kind: str
    decision: str
    decided_by: str


def is_owner(user_id: str, owner_ids: Iterable[str]) -> bool:
    """``user_id`` がオーナー集合に含まれるか。ID は文字列比較(Discord snowflake）。"""
    return str(user_id) in {str(o) for o in owner_ids}


def build_approval_embed(
    proposal_ref: str,
    title: str,
    body: str,
    kind: str = "other",
) -> dict:
    """承認 embed(緑・免責フッター）を組み立てる。

    ``proposal_ref`` はフッターに埋め、押下時にどの提案かを View から復元できるようにする。
    """
    if kind not in KINDS:
        raise ValueError(f"未知の提案種別: {kind}")
    return {
        "title": title,
        "description": body,
        "color": COLOR_APPROVAL,
        "fields": [
            {"name": "種別", "value": kind, "inline": True},
            {"name": "提案参照", "value": proposal_ref, "inline": True},
        ],
        "footer": {"text": f"{DISCLAIMER} / proposal:{proposal_ref}"},
    }


def record_decision(
    conn: psycopg.Connection,
    proposal_ref: str,
    decision: str,
    decided_by: str,
    owner_ids: Iterable[str],
    *,
    kind: str = "other",
    note: str | None = None,
    channel_msg_id: str | None = None,
) -> Decision:
    """押下者のオーナー検証後、``governance.decisions`` に決定を記録する。

    - 非オーナーは ``NotOwnerError``(記録しない)
    - 未知の decision / kind は ``ValueError``
    - 既に同 ``proposal_ref`` の決定があれば ``psycopg.errors.UniqueViolation``(二度押し防止)

    呼び出し側でトランザクションを制御する(本関数は commit しない)。
    """
    if decision not in DECISIONS:
        raise ValueError(f"未知の決定: {decision}")
    if kind not in KINDS:
        raise ValueError(f"未知の提案種別: {kind}")
    if not is_owner(decided_by, owner_ids):
        raise NotOwnerError(f"非オーナーの承認操作を拒否: user={decided_by}")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.decisions
                (proposal_ref, kind, decision, decided_by, note, channel_msg_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (proposal_ref, kind, decision, str(decided_by), note, channel_msg_id),
        )
        decision_id = cur.fetchone()[0]
    return Decision(
        id=decision_id,
        proposal_ref=proposal_ref,
        kind=kind,
        decision=decision,
        decided_by=str(decided_by),
    )
