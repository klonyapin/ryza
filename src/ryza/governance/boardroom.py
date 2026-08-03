"""boardroom — 役員室(会議)のロジック層(05-governance §5・Issue #9)。

ダッシュボードの「役員室」タブ(``dashboard/app.py``)から分離した、UI に依存しない
関数群。代表が発言すると**反応すべきと判断された役員だけ**が発言する会議形式で審議し、
会話を議事録(``governance.minutes``・meeting='office_chat')に保存し、「決議」を
``governance.minute_resolutions`` にマークし、セッションの主要な主張・懸念を LLM 要約で
``governance.stances`` に蓄積する。

**会議形式(2026-08-03 代表指示。役職を1つ選ぶ1対1チャットの廃止)**: 会議の1ターンは
「ルータ段 → 発言段 → 反応ラウンド(最大1回)」で構成する。

1. **ルータ段(安価な階層・既定 mid)**: 会議トランスクリプトを入力に、どの役職が発言
   すべきかを発言順つきで選ぶ(``route_speakers``)。全員が毎回話す必要はない
2. **発言段(役員の階層・既定 fable)**: 選ばれた役職だけが、ルータの与えた順で逐次発言
   する(``speak``)。後の発言者には先行者の発言を含む**当該会議のトランスクリプト全文**
   を渡す(会議体の審議は相互批判が本質であり、先行発言を見ずに並列独白させると牽制が
   働かない — 05 §3 の「CIO の提案を独立役員が批判し、代表が決める」構造)
3. **反応ラウンド(最大1回)**: 新たに出た発言に反応すべき役職をルータがもう一度だけ選び、
   選ばれた役職が発言する。1ターンの発言(高階層呼び出し)は ``MAX_SPEECHES_PER_TURN``
   件までにコードで強制的に打ち切る(会議が発散して費用が青天井になるのを防ぐ)

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
# 実際に誰が発言するかは毎ターン ``route_speakers`` が選ぶ(この dict は出席者名簿であり
# 発言順ではない)。議事録の出席者表記・要約対象の並び順にはこの定義順を使う。
BOARDROOM_ROLES: dict[str, str] = {
    "cio": "CIO",
    "independent_officer": "独立役員",
    "audit": "監査",
}

# 出席役職の正準順(議事録の出席者行・stances 要約の順序に使う決定論的な並び)。
MEETING_ORDER: tuple[str, ...] = tuple(BOARDROOM_ROLES)

# 誰も選ばれなかったときに応答する進行役。会議で代表の発言が黙殺されるのを避ける
# (無応答は UI 上「壊れている」ようにしか見えない)。
FACILITATOR_ROLE = "cio"

# 1ターン(代表発言 → 発言段 → 反応ラウンド)で許す発言数の上限。**コストの硬い上限**で
# あり、ルータが何人選んでもここで打ち切る(2026-08-03 代表指示: 高階層呼び出しは
# 1ターン最大4回)。ルータ呼び出し自体は安価な階層のため上限に数えない。
MAX_SPEECHES_PER_TURN = 4

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

# ルータ段の出力(発言すべき役職を発言順で)。空配列 = 誰も発言しない、も許す
# (その場合の扱いは ``conduct_meeting`` が決める — FACILITATOR_ROLE が短く応答)。
# enum で役職キーを縛るため、未知の役職名はスキーマ検証で弾かれる。
SPEAKER_ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["roles"],
    "additionalProperties": True,
    "properties": {
        "roles": {
            "type": "array",
            "items": {"type": "string", "enum": list(MEETING_ORDER)},
        },
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
    """会議の出席者(代表+実際に発言した役職)を正準順で返す。"""
    spoke = {t.speaker for t in turns}
    return ["representative", *[r for r in MEETING_ORDER if r in spoke]]


# ── 会話の Markdown 化(議事録本文)───────────────────────────────────────────
def transcript_markdown(turns: Sequence[ChatTurn], *, held_at: datetime) -> str:
    """会議全文を議事録本文(Markdown)へ決定論的に整形する(05 §4「全文を残す」)。"""
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


# ── ルータ段(誰が発言するかの判定)─────────────────────────────────────────────
_ROLE_MENU = "\n".join(
    f"- {r}({BOARDROOM_ROLES[r]})" for r in MEETING_ORDER
)

# ルータは会議の進行役であって役員ではない(役職資産・stances を読まない — 記憶分離の
# 外側にある純粋な交通整理)。安価な階層で回すことを前提にした短いシステム指示にする。
_ROUTER_SYSTEM = (
    "あなたは Ryza 役員室の会議進行役。代表(ユーザー)の発言に対し、**どの役職が"
    "発言すべきか**を選ぶ。選ばれた役職だけが発言する(全員が毎回話す必要はない)。\n\n"
    f"# 出席役職\n{_ROLE_MENU}\n\n"
    "# 判定規則(上から順に適用)\n"
    "1. 代表が特定の役職・その担当者名に呼びかけている場合(「CIO はどう思う」「ほむらの"
    "意見は」など)は、**必ずその役職を含める**\n"
    "2. 議題が重要決定・提案・数値の主張(実弾移行・IPS 改訂・リスクリミット・資本配分・"
    "戦略の採否・パフォーマンスや確率の数値主張など)を含む場合は、**必ず independent_officer"
    "を含める**(批判義務 — 05-governance §3)\n"
    "3. 単なる雑談・事実確認・軽い相談なら、最も関連する役職**1名で十分**\n"
    "4. 手続・証跡・コンプライアンス・監査に関わる論点があるときだけ audit を含める\n"
    "5. 誰も発言する必要がなければ roles は空配列でよい\n\n"
    "# 出力\n"
    "roles に役職キー(上記の英字キー)を**発言させたい順**で並べる。同じ役職を"
    "重複させない。理由は書かない。"
)

_REACTION_ROUTER_SYSTEM = (
    "あなたは Ryza 役員室の会議進行役。直前のラウンドで役員の発言があった。**それに"
    "反応すべき役職**を選ぶ(反応ラウンドは1回だけで、ここで選ばれなければこのターンは"
    "終了する)。\n\n"
    f"# 出席役職\n{_ROLE_MENU}\n\n"
    "# 判定規則\n"
    "1. まだ発言していない役職のうち、直前の発言に**付け加えるべき実質的な論点**を持つ"
    "者を選ぶ\n"
    "2. 既に発言した役職でも、直前の発言が自分の主張への反論・事実誤認を含み、**反論が"
    "必要な場合**は選んでよい\n"
    "3. 同意の繰り返し・要約・相槌しか生まないなら選ばない。**空配列が既定**と考える\n"
    "4. 代表の判断を待つべき局面(論点が出そろっている)なら空配列にする\n\n"
    "# 出力\n"
    "roles に役職キーを発言させたい順で並べる。同じ役職を重複させない。理由は書かない。"
)


def route_speakers(
    llm: StructuredLLM,
    *,
    turns: Sequence[ChatTurn],
    model: str,
    model_tier: str,
    reaction: bool = False,
    limit: int = MAX_SPEECHES_PER_TURN,
) -> list[str]:
    """このラウンドで発言すべき役職を、発言順のリストで返す(空も可)。

    ``reaction=False`` は代表の発言を受けた1回目のラウンド、``True`` は役員の発言に
    反応するラウンド。出力は ``SPEAKER_ROUTE_SCHEMA``(enum)で役職キーを検証済みで、
    さらに重複除去と ``limit`` 件への打ち切りを決定論的に行う(LLM の出力をそのまま
    実行数にしない — 不変原則1「LLM は判断材料を作る側」)。
    """
    if not turns:
        raise ValueError("会議のトランスクリプトが空(代表の発言が必要)")
    result = llm.complete(
        system=_REACTION_ROUTER_SYSTEM if reaction else _ROUTER_SYSTEM,
        user="# 会議のこれまでの発言(古い順)\n\n" + _conversation_block(turns),
        schema=SPEAKER_ROUTE_SCHEMA,
        task_type=TASK_TYPE,
        model_tier=model_tier,
        model=model,
    )
    selected: list[str] = []
    for role in result.content["roles"]:
        role = str(role)
        if role in BOARDROOM_ROLES and role not in selected:
            selected.append(role)
    return selected[:limit]


# ── 発言段 ─────────────────────────────────────────────────────────────────────
_MEETING_DIRECTIVE = (
    "\n\n---\n"
    "# 役員室会議(05-governance §5)\n"
    "ここはダッシュボードの役員室で、いま開かれているのは**会議**である。出席者は代表"
    f"(ユーザー)と全役職({'・'.join(BOARDROOM_ROLES[r] for r in MEETING_ORDER)})であり、"
    "あなたは上記の役職として発言する。\n"
    "- 毎ターン、進行役が**発言すべき役職だけ**を選ぶ。あなたが選ばれたのは、この論点に"
    "あなたの発言が要るからである。同意の繰り返しではなく、**あなたにしか出せない論点**を"
    "述べる\n"
    "- これまでの発言(代表・先行した役員)は「# これまでの会議」に全て入っている。"
    "**前の発言を踏まえて議論する**(同意・反論・補足を明示し、誰の何に対してかを書く)\n"
    "- 発言は reply フィールドに Markdown で書く(見出し・箇条書き可)\n"
    "- 議論規約(追従の禁止): 代表や他の役員の提案・判断には妥当性の評価(根拠付き)・"
    "リスクや反例・より良い代替案を返す。全面同意する場合は「反対すべき点を探して"
    "見つからなかった」と明示する\n"
    "- 他の役職になりきって発言しない(自分の役職の発言だけを書く)\n"
    "- この対話は判断材料であり、何も自動執行されない。発効する決定は代表が「決議」として"
    "明示的にマークしたもののみ(05 §4)\n"
)

# 独立役員の応答義務(05 §3: 全ての重要決定に最低1つの懸念を出す義務)は会議形式でも
# 維持する。ルータ側の規則2(重要決定なら必ず選ぶ)と対で機能する。
_ROLE_DIRECTIVES: dict[str, str] = {
    "independent_officer": (
        "- **応答義務(05 §3)**: 議題に重要決定(実弾移行・IPS 改訂・リスクリミット変更・"
        "保護領域の変更・戦略昇格など)が含まれる場合は、必ず懸念か反対視点を1つ以上出す"
        "(05 §6-5: 懸念ゼロ回答の連続は監査アラート対象)\n"
    ),
}

# ルータが誰も選ばなかったときの進行役への追加指示(代表の発言を黙殺しないための最小応答)。
_FACILITATOR_DIRECTIVE = (
    "- **この発言は会議の進行役としての応答である**: 他の役員が発言する必要はないと"
    "判断された論点なので、**簡潔に(数行で)**応答し、必要なら次に議論すべき点を1つ示す\n"
)


def meeting_directive(role: str, *, facilitator: bool = False) -> str:
    """当該役職に付ける会議指示(共通指示+役職固有の義務+進行役指示)。"""
    directive = _MEETING_DIRECTIVE + _ROLE_DIRECTIVES.get(role, "")
    return directive + _FACILITATOR_DIRECTIVE if facilitator else directive


def speak(
    llm: StructuredLLM,
    *,
    role: str,
    onboarding_prompt: str,
    turns: Sequence[ChatTurn],
    model: str,
    model_tier: str,
    facilitator: bool = False,
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
        system=onboarding_prompt + meeting_directive(role, facilitator=facilitator),
        user="\n\n".join(parts),
        schema=REPLY_SCHEMA,
        task_type=TASK_TYPE,
        model_tier=model_tier,
        model=model,
    )
    return str(result.content["reply"])


# ── 会議の1ターン(ルータ段 → 発言段 → 反応ラウンド)──────────────────────────
class MeetingResult(NamedTuple):
    """会議1ターンの結果。``rounds`` はラウンドごとにルータが選んだ役職(コスト記録用)。"""

    turns: list[ChatTurn]
    rounds: list[list[str]]


def conduct_meeting(
    *,
    router_llm: StructuredLLM,
    speaker_llm: StructuredLLM,
    onboarding_for_role: Callable[[str], str],
    turns: Sequence[ChatTurn],
    router_model: str,
    router_tier: str,
    speaker_model: str,
    speaker_tier: str,
    on_reply: Callable[[ChatTurn], None] | None = None,
    max_speeches: int = MAX_SPEECHES_PER_TURN,
) -> MeetingResult:
    """代表の1発言に対し、反応すべき役員だけを発言させる(会議の1ターン)。

    手順(2026-08-03 代表指示):

    1. ルータ(安価な階層)が発言すべき役職を発言順で選ぶ。誰も選ばれなければ
       ``FACILITATOR_ROLE`` が簡潔に応答する(代表の発言を黙殺しない)
    2. 選ばれた役職が順に発言する。各役職には**先行者の発言を追加したトランスクリプト**
       を渡す(モジュール docstring の 05 §6-2 に関する注記を参照)
    3. 予算が残っていればルータをもう1度だけ回し、反応すべき役職が発言する(最大1回)

    発言(高階層呼び出し)は合計 ``max_speeches`` 件を超えない — ルータの出力が何であれ
    コード側で打ち切る。``on_reply`` は1発言ごとに呼ばれるコールバック(UI の逐次描画用)。
    LLM 呼び出しが失敗した場合は例外をそのまま送出する(既に得た発言は呼び出し側が
    ``on_reply`` で受け取っている — 発言を握り潰さない)。
    """
    if not turns or turns[-1].speaker != "representative":
        raise ValueError("turns の末尾は代表(representative)の発言でなければならない")
    if max_speeches < 1:
        raise ValueError("max_speeches は 1 以上でなければならない(無応答の会議は作らない)")
    transcript = list(turns)
    new_turns: list[ChatTurn] = []
    rounds: list[list[str]] = []

    def _speak_round(roles: Sequence[str], *, facilitator: bool = False) -> None:
        for role in roles:
            reply = speak(
                speaker_llm,
                role=role,
                onboarding_prompt=onboarding_for_role(role),
                turns=transcript,
                model=speaker_model,
                model_tier=speaker_tier,
                facilitator=facilitator,
            )
            turn = ChatTurn(role, reply.strip())
            transcript.append(turn)
            new_turns.append(turn)
            if on_reply is not None:
                on_reply(turn)

    selected = route_speakers(
        router_llm, turns=transcript, model=router_model, model_tier=router_tier,
        limit=max_speeches,
    )
    if selected:
        rounds.append(selected)
        _speak_round(selected)
    else:
        # 進行役の最小応答。ルータの空選択でも会議は必ず1発言を返す。
        rounds.append([FACILITATOR_ROLE])
        _speak_round([FACILITATOR_ROLE], facilitator=True)

    remaining = max_speeches - len(new_turns)
    if remaining > 0:
        reacting = route_speakers(
            router_llm, turns=transcript, model=router_model, model_tier=router_tier,
            reaction=True, limit=remaining,
        )
        if reacting:
            rounds.append(reacting)
            _speak_round(reacting)
    return MeetingResult(turns=new_turns, rounds=rounds)


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
    "- 引き継ぐ価値のある内容がなければ stances は空配列でよい\n"
)


def speaking_roles(turns: Sequence[ChatTurn]) -> list[str]:
    """発言した役職を正準順で返す(stances 要約の対象 — 発言していない役職は作らない)。"""
    spoke = {t.speaker for t in turns if t.speaker != "representative"}
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
    "FACILITATOR_ROLE",
    "MAX_SPEECHES_PER_TURN",
    "MEETING_ORDER",
    "REPLY_SCHEMA",
    "SPEAKER_ROUTE_SCHEMA",
    "STANCE_DIGEST_SCHEMA",
    "TASK_TYPE",
    "ChatTurn",
    "MeetingResult",
    "SavedMinute",
    "attendees_of",
    "conduct_meeting",
    "digest_stances",
    "fetch_resolutions",
    "mark_resolution",
    "meeting_directive",
    "record_chat_stances",
    "route_speakers",
    "save_office_chat_minute",
    "speak",
    "speaking_roles",
    "transcript_markdown",
]
