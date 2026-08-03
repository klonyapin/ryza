"""Kill Switch 多段状態機械(IPS v1.3 §5・Issue #24。保護領域)。

凍結が常に最善ではない(凍結中も市場リスク・追証・満期は残る)ため3モードを持つ:

- ``/kill``(凍結)      … 全新規発注停止・ポジション維持。「システムが信用できない」時の既定
- ``/winddown``(現金化)… 決定論アルゴ(TWAP 等)で段階的に全ポジションを現金化
- ``/flatten``(緊急)   … 成行で即時全清算。2段階確認必須

状態機械(現在値 ``ops.trading_state``・監査 ``governance.killswitch_events``):

    normal ──/kill──────────────→ frozen ⟲(/kill は冪等)
    normal|frozen ──/winddown──→ winding_down
    normal|frozen|winding_down ──/flatten(2段階)──→ flattening
    winding_down ──/kill──→ frozen(清算プログラム停止)
    flattening   ──/kill──→ frozen(清算プログラム停止)
    winding_down|flattening ──執行層完了通知──→ flattened
    frozen|winding_down|flattening|flattened ──/resume(2段階)──→ normal

復帰(→normal)はユーザーの明示操作(``/resume``)のみ。実際の清算実行は執行層
(ブローカーアダプタ・未実装)が担うため、``ExecutionHook`` Protocol で切り出す。
フック未接続時は状態遷移のみ記録し「執行層未接続」を ``#運営`` に明示通知する。

**この経路に LLM を入れてはならない**(CLAUDE.md 不変原則1・6)。清算は決定論コードのみ。
既存の ``ops.flags.kill_switch`` は「state <> 'normal'」の派生ミラーとして同一トランザクション
内で維持する(全発注経路・日次ジョブの ``is_engaged`` 参照を壊さない)。
discord.py には依存しない。オーナー検証は ``approvals.is_owner`` を再利用する。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

import psycopg

from ryza.bot import COLOR_FLASH, COLOR_NORMAL, KILL_SWITCH
from ryza.bot import outbox as outbox_mod
from ryza.bot.approvals import NotOwnerError, build_approval_embed, is_owner

# 取引状態(ops.trading_state.state)
NORMAL = "normal"
FROZEN = "frozen"
WINDING_DOWN = "winding_down"
FLATTENING = "flattening"
FLATTENED = "flattened"
STATES = (NORMAL, FROZEN, WINDING_DOWN, FLATTENING, FLATTENED)

# コマンドごとの許可遷移(from → to)。ここに無い組は InvalidTransitionError。
_TRANSITIONS: dict[str, dict[str, str]] = {
    "kill": {
        NORMAL: FROZEN,
        FROZEN: FROZEN,  # 冪等(理由の更新・監査記録は残す)
        WINDING_DOWN: FROZEN,  # 清算プログラムを停止して凍結
        FLATTENING: FROZEN,  # 同上
    },
    "winddown": {NORMAL: WINDING_DOWN, FROZEN: WINDING_DOWN},
    "flatten": {NORMAL: FLATTENING, FROZEN: FLATTENING, WINDING_DOWN: FLATTENING},
    "resume": {FROZEN: NORMAL, WINDING_DOWN: NORMAL, FLATTENING: NORMAL, FLATTENED: NORMAL},
    "liquidation_complete": {WINDING_DOWN: FLATTENED, FLATTENING: FLATTENED},
}


class InvalidTransitionError(RuntimeError):
    """現在状態から許可されていない遷移を要求した。"""


class ExecutionHook(Protocol):
    """執行層(ブローカーアダプタ)への清算指示インターフェース。

    実装は決定論コードのみ(LLM 禁止)。清算完了時は ``complete_liquidation`` を
    呼び戻して状態を ``flattened`` へ進める。未実装の間は ``None`` を渡す
    (状態遷移のみ記録し ``#運営`` に「執行層未接続」を通知する)。
    """

    def start_winddown(self, conn: psycopg.Connection) -> None:
        """段階的現金化(TWAP 等の決定論アルゴ)を開始する。"""
        ...

    def start_flatten(self, conn: psycopg.Connection) -> None:
        """成行での即時全清算を開始する。"""
        ...

    def halt(self, conn: psycopg.Connection) -> None:
        """実行中の清算プログラムを停止する(/kill による凍結時)。"""
        ...


@dataclass(frozen=True)
class TransitionResult:
    """状態遷移1回の結果。"""

    previous: str
    state: str
    actor: str
    reason: str | None
    hook_engaged: bool | None  # None=フック不要の遷移


def get_state(conn: psycopg.Connection) -> str:
    """現在の取引状態。行が無ければ ``normal``(マイグレーション前の互換)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM ops.trading_state")
        row = cur.fetchone()
        return row[0] if row else NORMAL


def is_engaged(conn: psycopg.Connection) -> bool:
    """Kill Switch(いずれかのモード)が有効か。発注経路はこれを参照して新規発注を止める。"""
    return get_state(conn) != NORMAL


def _record_event(
    conn: psycopg.Connection,
    *,
    event_type: str,
    command: str,
    from_state: str,
    to_state: str,
    actor: str,
    reason: str | None,
    confirmed: bool,
    hook_engaged: bool | None,
) -> None:
    """``governance.killswitch_events`` に監査行を追記する(呼び出し側が commit)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.killswitch_events
                (event_type, command, from_state, to_state, actor, reason, confirmed, hook_engaged)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_type, command, from_state, to_state,
                str(actor), reason, confirmed, hook_engaged,
            ),
        )


def _mirror_flag(conn: psycopg.Connection, state: str, actor: str, reason: str | None) -> None:
    """``ops.flags.kill_switch`` を派生ミラーとして更新する(state<>normal)。

    値が変わるときだけ ``ops.flag_events`` にも追記し、旧監査証跡の連続性を保つ。
    """
    enabled = state != NORMAL
    with conn.cursor() as cur:
        cur.execute("SELECT enabled FROM ops.flags WHERE name = %s", (KILL_SWITCH,))
        row = cur.fetchone()
        previous = bool(row[0]) if row else False
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
            (KILL_SWITCH, enabled, reason, str(actor)),
        )
        if row is None or previous != enabled:
            cur.execute(
                "INSERT INTO ops.flag_events (name, enabled, reason, actor)"
                " VALUES (%s, %s, %s, %s)",
                (KILL_SWITCH, enabled, reason, str(actor)),
            )


def _transition(
    conn: psycopg.Connection,
    command: str,
    actor: str,
    *,
    reason: str | None,
    confirmed: bool,
    hook_engaged: bool | None,
) -> TransitionResult:
    """状態遷移を実行し、現在値・ミラー・監査証跡を同一トランザクションで更新する。"""
    previous = get_state(conn)
    allowed = _TRANSITIONS[command]
    if previous not in allowed:
        raise InvalidTransitionError(f"/{command} は {previous} 状態からは実行できない")
    state = allowed[previous]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, reason, updated_by, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (singleton) DO UPDATE
            SET state = EXCLUDED.state,
                reason = EXCLUDED.reason,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (state, reason, str(actor)),
        )
    _record_event(
        conn,
        event_type="transition",
        command=command,
        from_state=previous,
        to_state=state,
        actor=actor,
        reason=reason,
        confirmed=confirmed,
        hook_engaged=hook_engaged,
    )
    _mirror_flag(conn, state, actor, reason)
    return TransitionResult(
        previous=previous, state=state, actor=str(actor), reason=reason, hook_engaged=hook_engaged
    )


def _notify_ops(
    conn: psycopg.Connection, run_id: int, title: str, body: str, *, urgent: bool
) -> None:
    """``#運営`` へ通知を投入する(配送は outbox 経由・Bot ポーラー)。"""
    embed = {
        "title": title,
        "description": body,
        "color": COLOR_FLASH if urgent else COLOR_NORMAL,
    }
    outbox_mod.enqueue(conn, "ops", embed, run_id, urgent=urgent)


def _require_owner(command: str, actor: str, owner_ids: Iterable[str]) -> None:
    if not is_owner(actor, owner_ids):
        raise NotOwnerError(f"非オーナーの /{command} を拒否: user={actor}")


# ────────────────────────────────────────────────────────────────────────────
# コマンド(オーナーのみ。呼び出し側が commit)
# ────────────────────────────────────────────────────────────────────────────
def engage(
    conn: psycopg.Connection,
    actor: str,
    owner_ids: Iterable[str],
    *,
    reason: str | None = None,
    run_id: int | None = None,
    hook: ExecutionHook | None = None,
) -> TransitionResult:
    """凍結(``/kill``)。全新規発注停止・ポジション維持。安全側なので確認なしで即時。

    清算中(winding_down/flattening)からの ``/kill`` は清算プログラムを停止して凍結する
    (``hook.halt``)。フック未接続でその状況なら ``#運営`` へ「執行層未接続」を通知する
    (``run_id`` 必須)。frozen からの再発動は冪等(理由を更新し監査記録は残す)。
    """
    _require_owner("kill", actor, owner_ids)
    liquidating = get_state(conn) in (WINDING_DOWN, FLATTENING)
    hook_engaged: bool | None = None
    if liquidating:
        hook_engaged = hook is not None
        if hook is not None:
            hook.halt(conn)
    result = _transition(
        conn, "kill", actor, reason=reason, confirmed=True, hook_engaged=hook_engaged
    )
    if liquidating and hook is None:
        if run_id is None:
            raise ValueError("執行層未接続の通知には run_id が必要")
        _notify_ops(
            conn,
            run_id,
            "⛔ /kill: 清算停止は執行層未接続",
            f"{result.previous} → frozen へ遷移を記録した。執行層(ブローカーアダプタ)が"
            "未接続のため、実行中の清算プログラムの停止指示は送っていない。手動確認が必要。",
            urgent=True,
        )
    return result


def winddown(
    conn: psycopg.Connection,
    actor: str,
    owner_ids: Iterable[str],
    run_id: int,
    *,
    reason: str | None = None,
    hook: ExecutionHook | None = None,
) -> TransitionResult:
    """計画的現金化(``/winddown``)。決定論アルゴで段階的に全ポジションを現金化する。

    normal/frozen からのみ。LLM はこの経路に一切関与しない。フック未接続時は状態遷移のみ
    記録し ``#運営`` へ「執行層未接続」を明示通知する。
    """
    _require_owner("winddown", actor, owner_ids)
    hook_engaged = hook is not None
    result = _transition(
        conn, "winddown", actor, reason=reason, confirmed=True, hook_engaged=hook_engaged
    )
    if hook is not None:
        hook.start_winddown(conn)
    else:
        _notify_ops(
            conn,
            run_id,
            "🪙 /winddown: 執行層未接続",
            f"{result.previous} → winding_down へ遷移を記録した。執行層(ブローカーアダプタ)が"
            "未接続のため、実際の段階的現金化は開始していない。手動での清算が必要。",
            urgent=True,
        )
    return result


def request_flatten(
    conn: psycopg.Connection,
    actor: str,
    owner_ids: Iterable[str],
    *,
    reason: str | None = None,
) -> str:
    """緊急清算の1段目(``/flatten`` コマンド発行)。監査記録のみで遷移しない。

    オーナー検証と遷移可能性を先に検査し(2段目で初めて失敗する誤操作を防ぐ)、
    ``governance.killswitch_events`` に request 行を残す。現在状態を返す。
    """
    _require_owner("flatten", actor, owner_ids)
    current = get_state(conn)
    if current not in _TRANSITIONS["flatten"]:
        raise InvalidTransitionError(f"/flatten は {current} 状態からは実行できない")
    _record_event(
        conn,
        event_type="request",
        command="flatten",
        from_state=current,
        to_state=FLATTENING,
        actor=actor,
        reason=reason,
        confirmed=False,
        hook_engaged=None,
    )
    return current


def flatten(
    conn: psycopg.Connection,
    actor: str,
    owner_ids: Iterable[str],
    run_id: int,
    *,
    confirmed: bool = False,
    reason: str | None = None,
    hook: ExecutionHook | None = None,
) -> TransitionResult:
    """緊急清算の2段目(確認ボタン押下)。成行で即時全清算を開始する。

    ``confirmed=False`` では遷移せず ``PermissionError``(2段階確認の強制)。
    コスト・スリッページを受け入れる緊急用であり、LLM はこの経路に一切関与しない。
    フック未接続時は状態遷移のみ記録し ``#運営`` へ「執行層未接続」を明示通知する。
    """
    _require_owner("flatten", actor, owner_ids)
    if not confirmed:
        raise PermissionError("/flatten は確認ボタンによる2段階確認が必要")
    hook_engaged = hook is not None
    result = _transition(
        conn, "flatten", actor, reason=reason, confirmed=True, hook_engaged=hook_engaged
    )
    if hook is not None:
        hook.start_flatten(conn)
    else:
        _notify_ops(
            conn,
            run_id,
            "🚨 /flatten: 執行層未接続",
            f"{result.previous} → flattening へ遷移を記録した。執行層(ブローカーアダプタ)が"
            "未接続のため、実際の成行全清算は開始していない。手動での清算が必要。",
            urgent=True,
        )
    return result


def release(
    conn: psycopg.Connection,
    actor: str,
    owner_ids: Iterable[str],
    *,
    confirmed: bool = False,
    reason: str | None = None,
) -> TransitionResult:
    """復帰(``/resume``)。frozen/winding_down/flattening/flattened → normal。

    復帰はユーザーの明示操作のみ(自動復帰なし)。``confirmed=False`` では遷移せず
    ``PermissionError``(確認ボタン未押下の誤操作防止)。復帰は事故を招きうるため
    kill と非対称に2段階とする。
    """
    _require_owner("resume", actor, owner_ids)
    if not confirmed:
        raise PermissionError("/resume は確認ボタンによる2段階確認が必要")
    return _transition(conn, "resume", actor, reason=reason, confirmed=True, hook_engaged=None)


# ────────────────────────────────────────────────────────────────────────────
# 執行層からの完了通知・凍結中の例外的取引
# ────────────────────────────────────────────────────────────────────────────
def complete_liquidation(
    conn: psycopg.Connection,
    source: str,
    run_id: int,
    *,
    detail: str | None = None,
) -> TransitionResult:
    """清算完了の報告(執行層の決定論コードが呼ぶ)。winding_down/flattening → flattened。

    Discord コマンドではなくシステム内部の遷移(actor は ``system:<source>``)。
    完了は ``#運営`` へ報告する。flattened 後も発注は停止のまま(復帰は ``/resume`` のみ)。
    """
    actor = f"system:{source}"
    result = _transition(
        conn, "liquidation_complete", actor, reason=detail, confirmed=True, hook_engaged=None
    )
    _notify_ops(
        conn,
        run_id,
        "✅ 清算完了(flattened)",
        f"{result.previous} → flattened。全ポジションの現金化が完了した。"
        f"発注は停止のまま(復帰は /resume のみ)。{detail or ''}".rstrip(),
        urgent=False,
    )
    return result


def request_frozen_exception(
    conn: psycopg.Connection,
    proposal_ref: str,
    title: str,
    body: str,
    run_id: int,
) -> int:
    """凍結中の例外的取引を ``#承認`` に1件ずつ起票する(IPS v1.3 §5)。

    frozen 状態でのみ起票できる。承認/却下は既存の承認 UI(ボタン → オーナー検証 →
    ``governance.decisions``、kind=``frozen_exception_trade``)で1件=1決定として記録される。
    投入した outbox id を返す。
    """
    current = get_state(conn)
    if current != FROZEN:
        raise InvalidTransitionError(f"例外的取引の起票は frozen 中のみ(現在: {current})")
    embed = build_approval_embed(proposal_ref, title, body, kind="frozen_exception_trade")
    return outbox_mod.enqueue(conn, "approval", embed, run_id, urgent=True)
