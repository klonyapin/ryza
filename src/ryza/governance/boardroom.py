"""boardroom — 役員室チャットのロジック層(05-governance §5・Issue #9)。

ダッシュボードの「役員室」タブ(``dashboard/app.py``)から分離した、UI に依存しない
関数群。役職を選んでチャットし、会話を議事録(``governance.minutes``・meeting
='office_chat')に保存し、「決議」を ``governance.minute_resolutions`` にマークし、
セッションの主要な主張・懸念を LLM 要約で ``governance.stances`` に蓄積する。

責務の分担:

- 着任プロンプトの組み立ては ``personas.assume_role``(本モジュールは受け取るだけ)
- LLM 呼び出しは ``StructuredLLM``(構造化出力・検証・リトライ・コスト記録)経由のみ。
  部門タグは ``governance``・task_type は ``boardroom``(呼び出し側が ``dept_tag`` を設定)
- DB 書込は本モジュールの関数に閉じる(テスト対象)。Streamlit UI 自体はテスト対象外

**不変原則1(LLM は判断材料を作る側)**: 役員室の LLM 出力は対話・議事録・stances に
しかならない。本モジュールは発注・設定変更・フラグ操作など「何かを自動執行する」経路を
一切持たず、発効する決定は代表が明示的にマークした決議のみ(05 §4)。決議マーク自体も
記録であって執行ではない(執行は別途、承認フロー・決定論コードの管轄)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple

import psycopg

from ryza.governance.personas import record_stance
from ryza.research.llm import StructuredLLM

# 役員室のタスク種別(コスト台帳のタグ。部門は dept_tag='governance')。
TASK_TYPE = "boardroom"

# 役員室でチャットできる役職(05 §5: CIO/独立役員。監査も対話窓口として許す)と表示名。
BOARDROOM_ROLES: dict[str, str] = {
    "cio": "CIO",
    "independent_officer": "独立役員",
    "audit": "監査",
}

_SPEAKER_LABELS = {"representative": "代表", **BOARDROOM_ROLES}


# ── 出力スキーマ(schemas.py の流儀: 狭い語彙のみで自前 validate に適合)────────
# チャット応答。自由文だが StructuredLLM の共通口(構造化出力+コスト記録)に乗せる
# ため {"reply": <Markdown>} の 1 フィールドに包む。
REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["reply"],
    "additionalProperties": True,
    "properties": {
        "reply": {"type": "string"},
    },
}

# セッション終了時の主張・懸念の要約(05 §4: stances への蓄積)。kind は
# governance.stances の CHECK(0013)に合わせる。retraction は要約では作らせない
# (撤回は明示操作 — personas.record_stance の retracts 経由のみ)。
STANCE_DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["stances"],
    "additionalProperties": True,
    "properties": {
        "stances": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "summary"],
                "additionalProperties": True,
                "properties": {
                    "kind": {"type": "string", "enum": ["claim", "concern", "dissent"]},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class ChatTurn:
    """役員室チャットの 1 発言。speaker は 'representative' か役職キー。"""

    speaker: str
    text: str


class SavedMinute(NamedTuple):
    """議事録保存の結果(minute_id と保存した本文 — stances 要約の入力に使い回す)。"""

    minute_id: int
    body_md: str


def _label(speaker: str) -> str:
    return _SPEAKER_LABELS.get(speaker, speaker)


# ── 会話の Markdown 化(議事録本文)───────────────────────────────────────────
def transcript_markdown(
    role: str, turns: Sequence[ChatTurn], *, held_at: datetime
) -> str:
    """会話全文を議事録本文(Markdown)へ決定論的に整形する。

    05 §4「要約でなく全文を残す」。見出しの「役員室チャット」が本文の種別表示
    (body_kind 相当 — テーブル側の種別は meeting='office_chat' が担う)。
    """
    lines = [
        f"# 役員室チャット({_label(role)})",
        "",
        f"- 開催: {held_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC",
        f"- 出席: 代表、{_label(role)}",
        "",
    ]
    for turn in turns:
        lines.append(f"**{_label(turn.speaker)}**: {turn.text}")
        lines.append("")
    return "\n".join(lines)


def _conversation_block(turns: Sequence[ChatTurn]) -> str:
    return "\n\n".join(f"{_label(t.speaker)}: {t.text}" for t in turns)


# ── チャット応答 ───────────────────────────────────────────────────────────────
_CHAT_DIRECTIVE = (
    "\n\n---\n"
    "# 役員室チャット(05-governance §5)\n"
    "ここはダッシュボードの役員室。相手は代表(ユーザー)であり、あなたは上記の役職として"
    "応答する。\n"
    "- 応答は reply フィールドに Markdown で書く(見出し・箇条書き可)\n"
    "- 議論規約(追従の禁止): 代表の提案・判断には妥当性の評価(根拠付き)・リスクや反例・"
    "より良い代替案を返す。全面同意する場合は「反対すべき点を探して見つからなかった」と"
    "明示する\n"
    "- この対話は判断材料であり、何も自動執行されない。発効する決定は代表が「決議」として"
    "明示的にマークしたもののみ(05 §4)\n"
)


def chat_reply(
    llm: StructuredLLM,
    *,
    onboarding_prompt: str,
    turns: Sequence[ChatTurn],
    model: str,
    model_tier: str,
) -> str:
    """会話履歴+代表の新しい発言に対する役職の応答を得る。

    system = 着任プロンプト(``personas.assume_role``)+役員室指示。会話履歴は
    プロバイダ契約(system+user の 2 引数)に合わせて user 側に直列化する。
    ``turns`` の末尾は代表の新しい発言でなければならない。
    """
    if not turns or turns[-1].speaker != "representative":
        raise ValueError("turns の末尾は代表(representative)の発言でなければならない")
    history, latest = turns[:-1], turns[-1]
    parts = []
    if history:
        parts.append("# これまでの対話\n\n" + _conversation_block(history))
    parts.append(f"# 代表の新しい発言\n\n{latest.text}")
    result = llm.complete(
        system=onboarding_prompt + _CHAT_DIRECTIVE,
        user="\n\n".join(parts),
        schema=REPLY_SCHEMA,
        task_type=TASK_TYPE,
        model_tier=model_tier,
        model=model,
    )
    return str(result.content["reply"])


# ── 議事録保存・決議マーク ─────────────────────────────────────────────────────
def save_office_chat_minute(
    conn: psycopg.Connection,
    *,
    role: str,
    turns: Sequence[ChatTurn],
    run_id: int,
    held_at: datetime | None = None,
) -> SavedMinute:
    """会話全文を ``governance.minutes``(meeting='office_chat')へ保存する。

    出席は代表+当該役職。テーブルは追記オンリー(0013)なので保存 = 確定。
    """
    if not turns:
        raise ValueError("空の会話は議事録として保存できない")
    held_at = held_at or datetime.now(UTC)
    body_md = transcript_markdown(role, turns, held_at=held_at)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.minutes (meeting, held_at, attendees, body_md, run_id)
            VALUES ('office_chat', %s, %s, %s, %s)
            RETURNING minute_id
            """,
            (held_at, ["representative", role], body_md, run_id),
        )
        return SavedMinute(minute_id=cur.fetchone()[0], body_md=body_md)


def mark_resolution(
    conn: psycopg.Connection,
    *,
    minute_id: int,
    title: str,
    resolution_md: str,
    proposal_ref: str | None = None,
) -> int:
    """議事録の 1 項目を「決議」としてマークする(発効する決定はこれのみ — 05 §4)。

    決議ボタンは代表のみ押せる建前(05 §5)。ダッシュボードはローカル専用で公開
    ホスティングを持たない(dashboard/app.py 冒頭)ため、**操作者=代表とみなす**。
    resolved_by='representative' は 0013 の CHECK でも DB 側から強制される。
    seq は同一議事録内の連番(既存最大+1)。UNIQUE(minute_id, seq) が二重マークを弾く。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(seq), 0) + 1 FROM governance.minute_resolutions"
            " WHERE minute_id = %s",
            (minute_id,),
        )
        seq = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO governance.minute_resolutions
                (minute_id, seq, title, resolution_md, proposal_ref, resolved_by)
            VALUES (%s, %s, %s, %s, %s, 'representative')
            RETURNING resolution_id
            """,
            (minute_id, seq, title, resolution_md, proposal_ref),
        )
        return cur.fetchone()[0]


def fetch_resolutions(
    conn: psycopg.Connection, minute_id: int
) -> list[dict[str, Any]]:
    """当該議事録の決議一覧(seq 順)。UI の確認表示用。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT resolution_id, seq, title, resolution_md, proposal_ref, created_at
            FROM governance.minute_resolutions
            WHERE minute_id = %s
            ORDER BY seq
            """,
            (minute_id,),
        )
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


# ── 主張・懸念の蓄積(セッション終了時)────────────────────────────────────────
_DIGEST_SYSTEM = (
    "あなたは Ryza の議事録係。役員室チャットの全文から、指定された役職**本人**の"
    "主要な主張・懸念を抽出して要約する(05-governance §4: 次回着任時に引き継がれる"
    "永続記憶になる)。\n"
    "- kind の判定: claim=主張(積極的な意見・提案)/ concern=懸念(リスク指摘)/ "
    "dissent=反対意見・少数意見\n"
    "- summary は 1 件 120 字以内。文脈なしで単独で読める一文にする\n"
    "- 代表の発言は対象外。引き継ぐ価値のある内容がなければ stances は空配列でよい\n"
)


def digest_stances(
    llm: StructuredLLM,
    *,
    role: str,
    transcript_md: str,
    model: str,
    model_tier: str,
) -> list[dict[str, str]]:
    """会話全文から当該役職の主張・懸念の要約リストを LLM で生成する。

    返り値は ``[{"kind": claim|concern|dissent, "summary": ...}, ...]``(空も可)。
    kind の妥当性は ``STANCE_DIGEST_SCHEMA`` の enum で検証済み(不適合は
    ``StructuredLLM`` がリトライ → 最終的に ``SchemaError``)。
    """
    result = llm.complete(
        system=_DIGEST_SYSTEM,
        user=f"役職: {_label(role)}({role})\n\n{transcript_md}",
        schema=STANCE_DIGEST_SCHEMA,
        task_type=TASK_TYPE,
        model_tier=model_tier,
        model=model,
    )
    return [
        {"kind": str(s["kind"]), "summary": str(s["summary"])}
        for s in result.content["stances"]
    ]


def record_chat_stances(
    conn: psycopg.Connection,
    *,
    role: str,
    stances: Sequence[dict[str, str]],
    minute_id: int,
    run_id: int,
) -> list[int]:
    """要約済みの主張・懸念を ``governance.stances`` へ追記する(出所 = 当該議事録)。"""
    return [
        record_stance(
            conn,
            role=role,
            kind=s["kind"],
            summary=s["summary"],
            run_id=run_id,
            minute_id=minute_id,
        )
        for s in stances
    ]


__all__ = [
    "BOARDROOM_ROLES",
    "REPLY_SCHEMA",
    "STANCE_DIGEST_SCHEMA",
    "TASK_TYPE",
    "ChatTurn",
    "SavedMinute",
    "chat_reply",
    "digest_stances",
    "fetch_resolutions",
    "mark_resolution",
    "record_chat_stances",
    "save_office_chat_minute",
    "transcript_markdown",
]
