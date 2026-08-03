"""boardroom — 役員室(会議)のロジック層(05-governance §5・Issue #9)。

ダッシュボードの「役員室」タブ(``dashboard/app.py``)から分離した、UI に依存しない
関数群。代表が発言すると全役職が固定順で逐次応答する**会議形式**で審議し、会話を
議事録(``governance.minutes``・meeting='office_chat')に保存し、「決議」を
``governance.minute_resolutions`` にマークし、セッションの主要な主張・懸念を LLM 要約で
``governance.stances`` に蓄積する。

**会議形式への変更(2026-08-03 代表指示)**: 役職を1つ選ぶ1対1チャットを廃止し、代表の
1発言に対して CIO → 独立役員 → 監査 の固定順で全役職が応答する。後の発言者には先行者の
発言を含む**当該会議のトランスクリプト全文**を渡す(会議体の審議は相互批判が本質であり、
先行発言を見ずに並列独白させると牽制が働かない — 05 §3 の「CIO の提案を独立役員が批判し、
代表が決める」構造)。

**05 §6-2(独立性の実質確保)との関係 — 共有するのは当該会議のトランスクリプトのみ**:
同条は独立役員が執行側と「モデル系統・プロンプト資産・記憶を共有しない」ことを求める。
本モジュールが共有するのは**その場で交わされた発言**(会議の議事録に全文が残るもの)
だけであり、以下は従来どおり役職別に分離したままである:

- 永続記憶(``governance.stances``)は role 単位で読み書きする(``personas.recent_stances``)
- 着任プロンプト(人格・charter)は役職ごとに独立(``personas.assume_role``)
- 盲検レビュー(戦略昇格・IPS 改訂案の評価)は本モジュールを経由しない別経路

すなわち「同じ会議に出席した役員が相手の発言を聞いている」以上のことは起きない。
会議で聞いた他役職の発言が自分の永続記憶に混入しないことは ``digest_stances`` の
指示(本人の発言のみを要約対象とする)と role 別の書込で担保する。

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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple

import psycopg

from ryza.governance.personas import record_stance
from ryza.research.llm import StructuredLLM

# 役員室のタスク種別(コスト台帳のタグ。部門は dept_tag='governance')。
TASK_TYPE = "boardroom"

# 役員室の会議に出席する役職(05 §5: CIO/独立役員。監査も対話窓口として許す)と表示名。
# **この dict の順序が会議の発言順**(CIO が提案・執行の立場を述べ、独立役員が批判し、
# 監査が証跡・手続の観点で締める — 05 §3 の牽制構造をそのまま発言順に写す)。
BOARDROOM_ROLES: dict[str, str] = {
    "cio": "CIO",
    "independent_officer": "独立役員",
    "audit": "監査",
}

# 会議の発言順(固定)。ダッシュボードもテストもこの順を正とする。
MEETING_ORDER: tuple[str, ...] = tuple(BOARDROOM_ROLES)

# 「追加すべき論点がない」ことの明示。同意の繰り返しでトークンを焼かないための出口で
# あり、発言機会があった証跡として議事録にも UI にも残す(非表示にはしない)。
PASS_TEXT = "(発言なし)"

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


def attendees_of(turns: Sequence[ChatTurn]) -> list[str]:
    """会議の出席者(代表+実際に発言機会があった役職)を固定順で返す。

    ``(発言なし)`` でパスした役職も出席者に含める(パスは欠席ではない)。
    """
    spoke = {t.speaker for t in turns}
    return ["representative", *[r for r in MEETING_ORDER if r in spoke]]


# ── 会話の Markdown 化(議事録本文)───────────────────────────────────────────
def transcript_markdown(turns: Sequence[ChatTurn], *, held_at: datetime) -> str:
    """会議全文を議事録本文(Markdown)へ決定論的に整形する。

    05 §4「要約でなく全文を残す」。パス(``(発言なし)``)もそのまま残す — 発言機会が
    あったことの証跡であり、05 §6-5(独立役員の懸念ゼロ回答の連続は監査アラート対象)を
    後から検証できるようにするため。
    """
    lines = [
        "# 役員室会議",
        "",
        f"- 開催: {held_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC",
        "- 出席: " + "、".join(_label(r) for r in attendees_of(turns)),
        "",
    ]
    for turn in turns:
        lines.append(f"**{_label(turn.speaker)}**: {turn.text}")
        lines.append("")
    return "\n".join(lines)


def _conversation_block(turns: Sequence[ChatTurn]) -> str:
    return "\n\n".join(f"{_label(t.speaker)}: {t.text}" for t in turns)


# ── 会議での応答 ───────────────────────────────────────────────────────────────
_ATTENDEE_LIST = "・".join(BOARDROOM_ROLES[r] for r in MEETING_ORDER)
_ORDER_LIST = " → ".join(BOARDROOM_ROLES[r] for r in MEETING_ORDER)

_MEETING_DIRECTIVE = (
    "\n\n---\n"
    "# 役員室会議(05-governance §5)\n"
    "ここはダッシュボードの役員室で、いま開かれているのは**会議**である。出席者は代表"
    f"(ユーザー)と全役職({_ATTENDEE_LIST})であり、あなたは上記の役職として発言する。\n"
    f"- 発言順は {_ORDER_LIST} の固定順。あなたより前の役員の発言は"
    "「# これまでの会議」に全て入っている。**前の発言を踏まえて議論する**"
    "(同意・反論・補足を明示し、誰の何に対してかを書く)\n"
    "- 発言は reply フィールドに Markdown で書く(見出し・箇条書き可)\n"
    "- 議論規約(追従の禁止): 代表や他の役員の提案・判断には妥当性の評価(根拠付き)・"
    "リスクや反例・より良い代替案を返す。全面同意する場合は「反対すべき点を探して"
    "見つからなかった」と明示する\n"
    f"- **追加すべき論点がなければ「{PASS_TEXT}」とだけ返してよい。同意の繰り返しは不要**\n"
    "- 他の役職になりきって発言しない(自分の役職の発言だけを書く)\n"
    "- この対話は判断材料であり、何も自動執行されない。発効する決定は代表が「決議」として"
    "明示的にマークしたもののみ(05 §4)\n"
)

# 独立役員の応答義務(05 §3: 全ての重要決定に最低1つの懸念を出す義務)は会議形式でも
# 維持する。パスを許すのは軽微な話題に限る(05 §6-5: 懸念ゼロの連続は監査アラート対象)。
_ROLE_DIRECTIVES: dict[str, str] = {
    "independent_officer": (
        "- **応答義務(05 §3)**: 議題に重要決定(実弾移行・IPS 改訂・リスクリミット変更・"
        "保護領域の変更・戦略昇格など)が含まれる場合は、必ず懸念か反対視点を1つ以上出す。"
        f"「{PASS_TEXT}」でパスしてよいのは軽微な話題のみ\n"
    ),
}


def meeting_directive(role: str) -> str:
    """当該役職に付ける会議指示(共通指示+役職固有の義務)。"""
    return _MEETING_DIRECTIVE + _ROLE_DIRECTIVES.get(role, "")


def is_pass(text: str) -> bool:
    """発言が「(発言なし)」パスかどうか(前後の空白・改行は無視する)。"""
    return text.strip() == PASS_TEXT


def speak(
    llm: StructuredLLM,
    *,
    role: str,
    onboarding_prompt: str,
    turns: Sequence[ChatTurn],
    model: str,
    model_tier: str,
) -> str:
    """当該役職の会議発言を1つ得る。

    system = 着任プロンプト(``personas.assume_role``)+会議指示。会議トランスクリプトは
    プロバイダ契約(system+user の 2 引数)に合わせて user 側に直列化する。``turns`` には
    代表の発言と、この会議で**先行した役員の発言**が時系列で入っている必要がある。
    """
    if not turns:
        raise ValueError("会議のトランスクリプトが空(代表の発言が必要)")
    parts = [
        "# これまでの会議(古い順)\n\n" + _conversation_block(turns),
        f"# あなたの発言({_label(role)})\n\n"
        "上記を踏まえ、あなたの役職として発言せよ。",
    ]
    result = llm.complete(
        system=onboarding_prompt + meeting_directive(role),
        user="\n\n".join(parts),
        schema=REPLY_SCHEMA,
        task_type=TASK_TYPE,
        model_tier=model_tier,
        model=model,
    )
    return str(result.content["reply"])


def conduct_meeting(
    llm: StructuredLLM,
    *,
    onboarding_for_role: Callable[[str], str],
    turns: Sequence[ChatTurn],
    model: str,
    model_tier: str,
    roles: Sequence[str] = MEETING_ORDER,
    on_reply: Callable[[ChatTurn], None] | None = None,
) -> list[ChatTurn]:
    """代表の1発言に対し、全役職を固定順で逐次発言させる(会議の1ターン)。

    ``turns`` は代表の新しい発言までを含む会議トランスクリプト(末尾は代表の発言)。
    各役職には**先行役員の発言を追加したトランスクリプト**を渡す(後の発言者が前の
    発言を踏まえて議論するため — モジュール docstring の 05 §6-2 に関する注記を参照)。
    ``onboarding_for_role`` は役職キー → 着任プロンプトの関数で、役職ごとに独立に
    組み立てられる(永続記憶の分離はここで担保される)。

    ``on_reply`` は1発言ごとに呼ばれるコールバック(UI の逐次描画用)。LLM 呼び出しが
    失敗した場合は例外をそのまま送出する(既に得た発言は呼び出し側が ``on_reply`` で
    受け取っている — 発言を握り潰さない)。
    """
    if not turns or turns[-1].speaker != "representative":
        raise ValueError("turns の末尾は代表(representative)の発言でなければならない")
    transcript = list(turns)
    new_turns: list[ChatTurn] = []
    for role in roles:
        reply = speak(
            llm,
            role=role,
            onboarding_prompt=onboarding_for_role(role),
            turns=transcript,
            model=model,
            model_tier=model_tier,
        )
        turn = ChatTurn(role, reply.strip())
        transcript.append(turn)
        new_turns.append(turn)
        if on_reply is not None:
            on_reply(turn)
    return new_turns


# ── 議事録保存・決議マーク ─────────────────────────────────────────────────────
def save_office_chat_minute(
    conn: psycopg.Connection,
    *,
    turns: Sequence[ChatTurn],
    run_id: int,
    held_at: datetime | None = None,
) -> SavedMinute:
    """会議全文を ``governance.minutes``(meeting='office_chat')へ保存する。

    出席者は発言から導出する(代表+実際に発言機会があった役職 — ``attendees_of``)。
    話者は role 名で記録し、キャラクター名は台帳(config/org.yaml)側の表示に任せる。
    テーブルは追記オンリー(0013)なので保存 = 確定。
    """
    if not turns:
        raise ValueError("空の会話は議事録として保存できない")
    held_at = held_at or datetime.now(UTC)
    body_md = transcript_markdown(turns, held_at=held_at)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.minutes (meeting, held_at, attendees, body_md, run_id)
            VALUES ('office_chat', %s, %s, %s, %s)
            RETURNING minute_id
            """,
            (held_at, attendees_of(turns), body_md, run_id),
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
    "あなたは Ryza の議事録係。役員室会議の全文から、指定された役職**本人**の"
    "主要な主張・懸念を抽出して要約する(05-governance §4: 次回着任時に引き継がれる"
    "永続記憶になる)。\n"
    "- kind の判定: claim=主張(積極的な意見・提案)/ concern=懸念(リスク指摘)/ "
    "dissent=反対意見・少数意見\n"
    "- summary は 1 件 120 字以内。文脈なしで単独で読める一文にする\n"
    "- **代表および他の役職の発言は対象外**(会議の全文を読むが、要約して永続記憶に"
    "書き込むのは指定された役職本人の発言のみ — 役職間で記憶を共有しない 05 §6-2)。"
    "他者の発言は本人の主張を理解する文脈としてのみ使う\n"
    f"- 「{PASS_TEXT}」だけの発言は引き継ぐ内容がない。引き継ぐ価値のある内容が"
    "なければ stances は空配列でよい\n"
)


def speaking_roles(turns: Sequence[ChatTurn]) -> list[str]:
    """実質的な発言(パスでない発言)をした役職を固定順で返す。

    stances 要約の対象を絞る用途(``(発言なし)`` しか出していない役職に永続記憶を
    作らせない)。
    """
    spoke = {t.speaker for t in turns if t.speaker != "representative" and not is_pass(t.text)}
    return [r for r in MEETING_ORDER if r in spoke]


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
    "MEETING_ORDER",
    "PASS_TEXT",
    "REPLY_SCHEMA",
    "STANCE_DIGEST_SCHEMA",
    "TASK_TYPE",
    "ChatTurn",
    "SavedMinute",
    "attendees_of",
    "conduct_meeting",
    "digest_stances",
    "fetch_resolutions",
    "is_pass",
    "mark_resolution",
    "meeting_directive",
    "record_chat_stances",
    "save_office_chat_minute",
    "speak",
    "speaking_roles",
    "transcript_markdown",
]
