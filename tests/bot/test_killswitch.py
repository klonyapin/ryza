"""Kill Switch 状態遷移のテスト(受け入れ基準: kill→flags 反映 / 非オーナー拒否 / 2段階確認)。

純ロジック(engage/release/is_engaged)をライブ DB で検証する。各テストは rollback で隔離する。
ops.flags は PRIMARY KEY(name)の単一行なので、同一トランザクション内での遷移を確認する。
"""

from __future__ import annotations

import pytest

from ryza.bot import killswitch
from ryza.bot.approvals import NotOwnerError

OWNERS = ["1001"]


def _events(conn, name: str = "kill_switch") -> list[bool]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled FROM ops.flag_events WHERE name = %s ORDER BY id", (name,)
        )
        return [r[0] for r in cur.fetchall()]


def test_default_disengaged(conn):
    # 行が無ければ False(安全側は engage 側なので、既定は通常運転)。
    assert killswitch.is_engaged(conn) is False


def test_engage_sets_flag_and_event(conn):
    killswitch.engage(conn, "1001", OWNERS, reason="異常検知")
    assert killswitch.is_engaged(conn) is True
    with conn.cursor() as cur:
        cur.execute("SELECT enabled, reason, updated_by FROM ops.flags WHERE name = 'kill_switch'")
        assert cur.fetchone() == (True, "異常検知", "1001")
    # 追記オンリーの遷移履歴に True が刻まれる。
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
    # 確認ボタン未押下(confirmed=False)では遷移しない。
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
    # 非オーナーの解除は無視され、有効のまま。
    assert killswitch.is_engaged(conn) is True
