"""Kill Switch 多段状態機械のテスト(IPS v1.3 §5・Issue #24)。

受け入れ基準:
- 旧来の kill→flags 反映 / 非オーナー拒否 / 2段階確認(後方互換)
- 3モードの状態遷移(normal/frozen/winding_down/flattening/flattened)と不正遷移の拒否
- /flatten の2段階確認(request→confirm)・governance.killswitch_events への監査記録
- 執行フック(Protocol)の呼び出しと、未接続時の #運営 通知
- 凍結中の例外的取引の #承認 起票(kind=frozen_exception_trade)

純ロジックをライブ DB で検証する(discord.py は import しない)。各テストは rollback で隔離。
"""

from __future__ import annotations

import pytest

from ryza.bot import killswitch
from ryza.bot.approvals import NotOwnerError, record_decision

OWNERS = ["1001"]


class FakeHook:
    """ExecutionHook のフェイク(呼び出し記録のみ・決定論)。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def start_winddown(self, conn) -> None:
        self.calls.append("start_winddown")

    def start_flatten(self, conn) -> None:
        self.calls.append("start_flatten")

    def halt(self, conn) -> None:
        self.calls.append("halt")


def _events(conn, name: str = "kill_switch") -> list[bool]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled FROM ops.flag_events WHERE name = %s ORDER BY id", (name,)
        )
        return [r[0] for r in cur.fetchall()]


def _ks_events(conn) -> list[tuple[str, str, str, str, bool]]:
    """(event_type, command, from_state, to_state, confirmed) を id 順で返す。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_type, command, from_state, to_state, confirmed
            FROM governance.killswitch_events ORDER BY id
            """
        )
        return cur.fetchall()


def _ops_notices(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT embed_json FROM press.outbox WHERE channel = 'ops' ORDER BY id")
        return [r[0] for r in cur.fetchall()]


# ────────────────────────────────────────────────────────────────────────────
# 後方互換(T-006 の受け入れ基準)
# ────────────────────────────────────────────────────────────────────────────
def test_default_disengaged(conn):
    # 行が無ければ normal(安全側は engage 側なので、既定は通常運転)。
    assert killswitch.is_engaged(conn) is False
    assert killswitch.get_state(conn) == killswitch.NORMAL


def test_engage_sets_flag_and_event(conn):
    killswitch.engage(conn, "1001", OWNERS, reason="異常検知")
    assert killswitch.is_engaged(conn) is True
    assert killswitch.get_state(conn) == killswitch.FROZEN
    # ops.flags は派生ミラーとして維持される(全発注経路の後方互換)。
    with conn.cursor() as cur:
        cur.execute("SELECT enabled, reason, updated_by FROM ops.flags WHERE name = 'kill_switch'")
        assert cur.fetchone() == (True, "異常検知", "1001")
    assert _events(conn)[-1] is True


def test_engage_then_release_transition(conn):
    killswitch.engage(conn, "1001", OWNERS)
    assert killswitch.is_engaged(conn) is True
    killswitch.release(conn, "1001", OWNERS, confirmed=True)
    assert killswitch.is_engaged(conn) is False
    # 監査証跡は True→False の順で追記される(現在値の上書きでは履歴が消えない)。
    assert _events(conn)[-2:] == [True, False]


def test_release_requires_confirmation(conn):
    killswitch.engage(conn, "1001", OWNERS)
    with pytest.raises(PermissionError):
        killswitch.release(conn, "1001", OWNERS, confirmed=False)
    assert killswitch.is_engaged(conn) is True


def test_non_owner_cannot_engage_or_release(conn):
    with pytest.raises(NotOwnerError):
        killswitch.engage(conn, "9999", OWNERS)
    assert killswitch.is_engaged(conn) is False
    killswitch.engage(conn, "1001", OWNERS)
    with pytest.raises(NotOwnerError):
        killswitch.release(conn, "9999", OWNERS, confirmed=True)
    assert killswitch.is_engaged(conn) is True


# ────────────────────────────────────────────────────────────────────────────
# 状態機械: /kill(凍結)
# ────────────────────────────────────────────────────────────────────────────
def test_kill_is_idempotent_from_frozen(conn):
    killswitch.engage(conn, "1001", OWNERS, reason="1回目")
    result = killswitch.engage(conn, "1001", OWNERS, reason="2回目")
    assert (result.previous, result.state) == (killswitch.FROZEN, killswitch.FROZEN)
    # 監査記録は2回分残る(冪等でも操作は記録する)。
    transitions = [e for e in _ks_events(conn) if e[1] == "kill"]
    assert len(transitions) == 2


def test_kill_during_winddown_halts_liquidation(conn, run_id):
    hook = FakeHook()
    killswitch.winddown(conn, "1001", OWNERS, run_id, hook=hook)
    result = killswitch.engage(conn, "1001", OWNERS, reason="やはり凍結", hook=hook)
    assert (result.previous, result.state) == (killswitch.WINDING_DOWN, killswitch.FROZEN)
    assert hook.calls == ["start_winddown", "halt"]


def test_kill_during_winddown_without_hook_notifies_ops(conn, run_id):
    hook = FakeHook()
    killswitch.winddown(conn, "1001", OWNERS, run_id, hook=hook)
    result = killswitch.engage(conn, "1001", OWNERS, run_id=run_id, hook=None)
    assert result.state == killswitch.FROZEN
    assert result.hook_engaged is False
    assert any("執行層未接続" in n["title"] for n in _ops_notices(conn))


# ────────────────────────────────────────────────────────────────────────────
# 状態機械: /winddown(計画的現金化)
# ────────────────────────────────────────────────────────────────────────────
def test_winddown_from_normal_and_frozen(conn, run_id):
    hook = FakeHook()
    result = killswitch.winddown(conn, "1001", OWNERS, run_id, reason="撤退", hook=hook)
    assert (result.previous, result.state) == (killswitch.NORMAL, killswitch.WINDING_DOWN)
    assert killswitch.is_engaged(conn) is True  # 現金化中も新規発注は停止
    assert hook.calls == ["start_winddown"]
    # frozen からも開始できる(凍結 → 現金化への切替)。
    killswitch.release(conn, "1001", OWNERS, confirmed=True)
    killswitch.engage(conn, "1001", OWNERS)
    result = killswitch.winddown(conn, "1001", OWNERS, run_id, hook=hook)
    assert (result.previous, result.state) == (killswitch.FROZEN, killswitch.WINDING_DOWN)


def test_winddown_rejected_while_already_liquidating(conn, run_id):
    killswitch.winddown(conn, "1001", OWNERS, run_id, hook=FakeHook())
    with pytest.raises(killswitch.InvalidTransitionError):
        killswitch.winddown(conn, "1001", OWNERS, run_id, hook=FakeHook())


def test_winddown_without_hook_notifies_ops(conn, run_id):
    result = killswitch.winddown(conn, "1001", OWNERS, run_id)
    assert result.hook_engaged is False
    notices = _ops_notices(conn)
    assert any("執行層未接続" in n["title"] and "winddown" in n["title"] for n in notices)


def test_winddown_requires_owner(conn, run_id):
    with pytest.raises(NotOwnerError):
        killswitch.winddown(conn, "9999", OWNERS, run_id)
    assert killswitch.get_state(conn) == killswitch.NORMAL


# ────────────────────────────────────────────────────────────────────────────
# 状態機械: /flatten(緊急清算・2段階確認)
# ────────────────────────────────────────────────────────────────────────────
def test_flatten_requires_confirmation(conn, run_id):
    with pytest.raises(PermissionError):
        killswitch.flatten(conn, "1001", OWNERS, run_id, confirmed=False)
    assert killswitch.get_state(conn) == killswitch.NORMAL


def test_flatten_two_stage_flow_records_request_and_transition(conn, run_id):
    hook = FakeHook()
    current = killswitch.request_flatten(conn, "1001", OWNERS, reason="緊急")
    assert current == killswitch.NORMAL
    assert killswitch.get_state(conn) == killswitch.NORMAL  # 1段目では遷移しない
    result = killswitch.flatten(conn, "1001", OWNERS, run_id, confirmed=True, hook=hook)
    assert (result.previous, result.state) == (killswitch.NORMAL, killswitch.FLATTENING)
    assert hook.calls == ["start_flatten"]
    events = [e for e in _ks_events(conn) if e[1] == "flatten"]
    assert events == [
        ("request", "flatten", "normal", "flattening", False),
        ("transition", "flatten", "normal", "flattening", True),
    ]


def test_flatten_overrides_winddown(conn, run_id):
    killswitch.winddown(conn, "1001", OWNERS, run_id, hook=FakeHook())
    result = killswitch.flatten(conn, "1001", OWNERS, run_id, confirmed=True, hook=FakeHook())
    assert (result.previous, result.state) == (killswitch.WINDING_DOWN, killswitch.FLATTENING)


def test_flatten_without_hook_notifies_ops(conn, run_id):
    result = killswitch.flatten(conn, "1001", OWNERS, run_id, confirmed=True)
    assert result.hook_engaged is False
    assert any("flatten" in n["title"] and "執行層未接続" in n["title"] for n in _ops_notices(conn))


def test_flatten_request_rejected_when_already_flattening(conn, run_id):
    killswitch.flatten(conn, "1001", OWNERS, run_id, confirmed=True)
    with pytest.raises(killswitch.InvalidTransitionError):
        killswitch.request_flatten(conn, "1001", OWNERS)
    with pytest.raises(killswitch.InvalidTransitionError):
        killswitch.flatten(conn, "1001", OWNERS, run_id, confirmed=True)


def test_flatten_requires_owner(conn, run_id):
    with pytest.raises(NotOwnerError):
        killswitch.request_flatten(conn, "9999", OWNERS)
    with pytest.raises(NotOwnerError):
        killswitch.flatten(conn, "9999", OWNERS, run_id, confirmed=True)
    assert killswitch.get_state(conn) == killswitch.NORMAL


# ────────────────────────────────────────────────────────────────────────────
# 状態機械: 清算完了(執行層からの通知)と復帰
# ────────────────────────────────────────────────────────────────────────────
def test_complete_liquidation_reaches_flattened_and_stays_halted(conn, run_id):
    killswitch.winddown(conn, "1001", OWNERS, run_id, hook=FakeHook())
    result = killswitch.complete_liquidation(conn, "broker_adapter", run_id, detail="全約定")
    assert (result.previous, result.state) == (killswitch.WINDING_DOWN, killswitch.FLATTENED)
    assert result.actor == "system:broker_adapter"
    # flattened 後も発注は停止のまま(復帰は /resume のみ)。
    assert killswitch.is_engaged(conn) is True
    assert any("清算完了" in n["title"] for n in _ops_notices(conn))


def test_complete_liquidation_rejected_unless_liquidating(conn, run_id):
    with pytest.raises(killswitch.InvalidTransitionError):
        killswitch.complete_liquidation(conn, "broker_adapter", run_id)
    killswitch.engage(conn, "1001", OWNERS)
    with pytest.raises(killswitch.InvalidTransitionError):
        killswitch.complete_liquidation(conn, "broker_adapter", run_id)


def test_resume_from_flattened_returns_to_normal(conn, run_id):
    killswitch.flatten(conn, "1001", OWNERS, run_id, confirmed=True)
    killswitch.complete_liquidation(conn, "broker_adapter", run_id)
    result = killswitch.release(conn, "1001", OWNERS, confirmed=True)
    assert (result.previous, result.state) == (killswitch.FLATTENED, killswitch.NORMAL)
    assert killswitch.is_engaged(conn) is False


def test_resume_rejected_from_normal(conn):
    with pytest.raises(killswitch.InvalidTransitionError):
        killswitch.release(conn, "1001", OWNERS, confirmed=True)


# ────────────────────────────────────────────────────────────────────────────
# 凍結中の例外的取引(#承認 で1件ずつユーザー承認)
# ────────────────────────────────────────────────────────────────────────────
def test_frozen_exception_request_enqueues_approval(conn, run_id):
    killswitch.engage(conn, "1001", OWNERS, reason="凍結")
    outbox_id = killswitch.request_frozen_exception(
        conn, "frozen-ex-001", "例外的取引: 7203 100株 売り", "追証回避のための例外清算", run_id
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, urgent, embed_json FROM press.outbox WHERE id = %s", (outbox_id,)
        )
        channel, urgent, embed = cur.fetchone()
    assert (channel, urgent) == ("approval", True)
    assert "proposal:frozen-ex-001" in embed["footer"]["text"]
    # 承認は既存 UI 経路(オーナー検証 → governance.decisions・1件=1決定)。
    d = record_decision(
        conn, "manual:frozen-ex-001", "approve", "1001", OWNERS, kind="frozen_exception_trade"
    )
    assert d.kind == "frozen_exception_trade"


def test_frozen_exception_request_rejected_outside_frozen(conn, run_id):
    with pytest.raises(killswitch.InvalidTransitionError):
        killswitch.request_frozen_exception(conn, "frozen-ex-002", "t", "b", run_id)
    killswitch.winddown(conn, "1001", OWNERS, run_id, hook=FakeHook())
    with pytest.raises(killswitch.InvalidTransitionError):
        killswitch.request_frozen_exception(conn, "frozen-ex-003", "t", "b", run_id)
