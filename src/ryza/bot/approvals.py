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
# frozen_exception_trade は Kill Switch 凍結中の例外的取引(1件=1決定・IPS v1.3 §5)
DECISIONS = ("approve", "reject", "question")
KINDS = ("pr", "strategy_promotion", "breaker_resume", "budget", "frozen_exception_trade", "other")


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


def parse_proposal(embed: dict) -> tuple[str, str] | None:
    """embed から ``(proposal_ref, kind)`` を復元する。承認 embed でなければ None。

    配送側(main)が ``#承認`` 向けメッセージにボタン View を付けるかの判定に使う。
    ``build_approval_embed`` が埋めたフッターの ``proposal:<ref>`` と「種別」フィールドを読む。
    """
    footer_text = (embed.get("footer") or {}).get("text", "")
    marker = "proposal:"
    idx = footer_text.rfind(marker)
    if idx < 0:
        return None
    ref = footer_text[idx + len(marker):].strip()
    if not ref:
        return None
    kind = "other"
    for f in embed.get("fields", []):
        if f.get("name") == "種別" and f.get("value") in KINDS:
            kind = f["value"]
            break
    return ref, kind


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
    reviewed_sha: str | None = None,
    review_ref: str | None = None,
) -> Decision:
    """押下者のオーナー検証後、``governance.decisions`` に決定を記録する。

    - 非オーナーは ``NotOwnerError``(記録しない)
    - 未知の decision / kind は ``ValueError``
    - 既に同 ``proposal_ref`` の決定があれば ``psycopg.errors.UniqueViolation``(二度押し防止)

    ``reviewed_sha`` / ``review_ref``(0029)は**明示承認にも書ける**。3専決事項(定款第3条:
    定款改正・実弾マネー・Kill Switch 復帰)は必ずこの経路を通るため、ここに列が無いと
    **最重要の決定種別が構造的に監査 A-18-8(審査対象 SHA の突合)の射程外**になる
    (独立役員審査 2026-08-04 SHA-4)。既定 ``None`` なのは、押下による承認が必ずしも
    コミットを対象としない(予算・戦略昇格など)ためで、値があるときだけ突合対象になる。

    呼び出し側でトランザクションを制御する(本関数は commit しない)。
    """
    from ryza.governance.decisions import normalize_reviewed_sha

    if decision not in DECISIONS:
        raise ValueError(f"未知の決定: {decision}")
    if kind not in KINDS:
        raise ValueError(f"未知の提案種別: {kind}")
    if not is_owner(decided_by, owner_ids):
        raise NotOwnerError(f"非オーナーの承認操作を拒否: user={decided_by}")
    # 様式検証は writer に一本化する(deemed 経路と同じ規則 = 突合が経路で変わらない)。
    reviewed_sha = normalize_reviewed_sha(reviewed_sha)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.decisions
                (proposal_ref, kind, decision, decided_by, note, channel_msg_id,
                 reviewed_sha, review_ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                proposal_ref, kind, decision, str(decided_by), note, channel_msg_id,
                reviewed_sha, review_ref,
            ),
        )
        decision_id = cur.fetchone()[0]
    return Decision(
        id=decision_id,
        proposal_ref=proposal_ref,
        kind=kind,
        decision=decision,
        decided_by=str(decided_by),
    )
