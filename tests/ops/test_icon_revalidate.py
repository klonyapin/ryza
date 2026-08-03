"""アイコン URL の定期再検証(0033)のテスト。

独立役員審査 0020 C-7(保存時の検証は保存時点のスナップショットでしかない)への
**検知側**の是正。再ホストを採らない判断の根拠は docs/research/icon-hosting-legal.md。

実ネットワークは叩かない — ``prober``(URL → 指紋)を差し替える。テスト DB に対して
実行し commit しない。
"""

from __future__ import annotations

import pytest

from ryza import org
from ryza.bot import COLOR_FLASH, COLOR_NORMAL
from ryza.db.conn import connect
from ryza.ops import icon_revalidate
from ryza.provenance import start_run

_URL = "https://example.test/aya.png"
_URL2 = "https://example.test/aya-v2.png"
_MEMBER = "aya"

_FP_A = org.IconFingerprint(content_type="image/png", content_length=1024, etag='"a"')
_FP_B = org.IconFingerprint(content_type="image/png", content_length=2048, etag='"b"')


@pytest.fixture
def conn(migrated_db):
    """rollback で隔離する接続。

    先に1文実行してトランザクションを開いておく — ``org.set_icon_override`` が使う
    ``conn.transaction()`` は、未開始なら BEGIN して脱出時に COMMIT してしまい隔離が
    効かなくなる(既に開始済みなら SAVEPOINT として振る舞う)。
    tests/ops/test_org_icon_overrides.py と同じ理由。
    """
    c = connect()
    c.execute("SELECT 1")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run_id(conn):
    return start_run("test.icon_revalidate", conn=conn).run_id


def _prober(result):
    """固定の指紋(または例外)を返す prober。呼ばれた URL を記録する。"""
    calls: list[str] = []

    def _probe(url: str) -> org.IconFingerprint:
        calls.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    _probe.calls = calls  # type: ignore[attr-defined]
    return _probe


def _events(conn, member_id: str = _MEMBER) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event, icon_url, before_json, after_json, detail "
            "FROM ops.org_icon_check_events WHERE member_id = %s ORDER BY id",
            (member_id,),
        )
        return cur.fetchall()


def _baseline(conn, member_id: str = _MEMBER) -> dict:
    return icon_revalidate.load_baselines(conn)[member_id]


# ── 基準の確立と定常状態 ─────────────────────────────────────────────────────
def test_first_observation_records_baseline_without_event(conn):
    """初回観測は「変化」ではない。基準を作るだけで通知しない。"""
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL})
    assert result.checked == 1
    assert result.events == []
    assert _events(conn) == []
    base = _baseline(conn)
    assert base["icon_url"] == _URL
    assert base["fingerprint"] == _FP_A
    assert base["last_error"] is None


def test_unchanged_fingerprint_produces_no_event(conn):
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    assert result.events == []
    assert _events(conn) == []


# ── すり替えの検知(C-7 の本題)──────────────────────────────────────────────
def test_changed_fingerprint_is_detected_and_logged(conn):
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_B), urls=urls)

    assert [e.event for e in result.events] == ["changed"]
    event = result.changed[0]
    assert event.before == _FP_A.as_dict() and event.after == _FP_B.as_dict()
    rows = _events(conn)
    assert len(rows) == 1 and rows[0][0] == "changed"
    assert rows[0][2] == _FP_A.as_dict() and rows[0][3] == _FP_B.as_dict()
    # 新しい指紋が次回の基準になる(同じ変化を毎日報告し続けない)。
    assert _baseline(conn)["fingerprint"] == _FP_B


def test_changed_is_reported_once_then_becomes_the_new_baseline(conn):
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    icon_revalidate.revalidate(conn, prober=_prober(_FP_B), urls=urls)
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_B), urls=urls)
    assert result.events == []
    assert len(_events(conn)) == 1


def test_authorized_url_change_is_rebaselined_not_reported_as_change(conn):
    """指示記録のある URL 差し替えは「変化」ではなく再基準化として扱う。

    0020 の履歴に残る代表の操作であり、すり替えではない。新しい URL の指紋が新しい基準に
    なる(記録が無い場合は C-13 の url_unverified になる — 別テスト)。
    """
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL})
    org.set_icon_override(conn, _MEMBER, _URL2, "representative")
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_B), urls={_MEMBER: _URL2})
    assert result.events == []
    assert result.changed == []
    assert _events(conn) == []
    base = _baseline(conn)
    assert base["icon_url"] == _URL2 and base["fingerprint"] == _FP_B


# ── 到達不能・復旧 ───────────────────────────────────────────────────────────
def test_probe_failure_is_recorded_once(conn):
    """同じ失敗が続く間は毎日イベントを積まない(形骸化の防止)。"""
    urls = {_MEMBER: _URL}
    boom = org.IconUrlError("URL に到達できない(HEAD: OSError: timed out)")
    first = icon_revalidate.revalidate(conn, prober=_prober(boom), urls=urls)
    assert [e.event for e in first.events] == ["error"]
    second = icon_revalidate.revalidate(conn, prober=_prober(boom), urls=urls)
    assert second.events == []
    assert len(_events(conn)) == 1
    assert _baseline(conn)["last_error"].startswith("IconUrlError")


def test_error_dedup_is_by_exception_kind_not_message(conn):
    """抑止は例外**型名**で行う(追補審査 C-16)。文言が毎回揺れても増殖しない。"""
    urls = {_MEMBER: _URL}
    for i in range(3):
        icon_revalidate.revalidate(
            conn,
            prober=_prober(org.IconUrlError(f"到達できない(request-id: {i})")),
            urls=urls,
        )
    assert len(_events(conn)) == 1
    # 文言そのものは現在値に残る(情報は失われない)。
    assert "request-id: 2" in _baseline(conn)["last_error"]


def test_different_exception_kind_is_reported_again(conn):
    """型が変われば別の障害。抑止しない。"""
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(org.IconUrlError("到達不能")), urls=urls)
    result = icon_revalidate.revalidate(conn, prober=_prober(OSError("socket 断")), urls=urls)
    assert [e.event for e in result.events] == ["error"]
    assert [r[0] for r in _events(conn)] == ["error", "error"]


def test_recovery_after_failure_emits_cleared(conn):
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    icon_revalidate.revalidate(
        conn, prober=_prober(org.IconUrlError("到達不能")), urls=urls
    )
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    assert [e.event for e in result.events] == ["cleared"]
    assert _baseline(conn)["last_error"] is None
    assert [r[0] for r in _events(conn)] == ["error", "cleared"]


def test_failure_keeps_previous_fingerprint_as_baseline(conn):
    """失敗しても直前の指紋は消さない — 復旧時の比較基準に要る。"""
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    icon_revalidate.revalidate(conn, prober=_prober(org.IconUrlError("x")), urls=urls)
    assert _baseline(conn)["fingerprint"] == _FP_A


# ── C-12: 障害中のすり替えを「復旧」に化けさせない ──────────────────────────
def test_swap_during_outage_is_reported_as_change_not_plain_recovery(conn):
    """1日 404 を返してから差し替えても、復旧時に温存指紋と比較して検知する。"""
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    icon_revalidate.revalidate(conn, prober=_prober(org.IconUrlError("404")), urls=urls)
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_B), urls=urls)

    assert {e.event for e in result.events} == {"cleared", "changed"}
    changed = result.changed[0]
    assert changed.before == _FP_A.as_dict() and changed.after == _FP_B.as_dict()
    # 通常色の「復旧」1通に化けない。
    assert icon_revalidate.build_report_embed(result)["color"] == COLOR_FLASH
    assert "内容が変わっている" in result.cleared[0].detail
    assert [r[0] for r in _events(conn)] == ["error", "cleared", "changed"]


def test_recovery_without_change_is_not_a_warning(conn):
    """障害中に中身が変わっていなければ、通常色の復旧のままにする(過剰警告を出さない)。"""
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    icon_revalidate.revalidate(conn, prober=_prober(org.IconUrlError("404")), urls=urls)
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    assert [e.event for e in result.events] == ["cleared"]
    assert icon_revalidate.build_report_embed(result)["color"] == COLOR_NORMAL


def test_error_before_any_success_does_not_fake_a_change(conn):
    """一度も成功観測が無い(全項目 NULL の)基準を「変化」と誤検知しない。"""
    urls = {_MEMBER: _URL}
    icon_revalidate.revalidate(conn, prober=_prober(org.IconUrlError("初回から死亡")), urls=urls)
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls=urls)
    assert [e.event for e in result.events] == ["cleared"]
    assert result.changed == []


# ── C-13: 指示記録の無い URL 変更を検知する ──────────────────────────────────
def _set_override(conn, url: str) -> None:
    """代表の差し替えを模す(0020 の書込ヘルパ = 現在値+追記オンリー履歴)。"""
    org.set_icon_override(conn, _MEMBER, url, "representative")


def test_url_change_without_any_record_is_urgent(conn):
    """DB に書ける主体が URL を差し替えただけの状態 — 検知機構の迂回そのもの。"""
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL})
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_B), urls={_MEMBER: _URL2})

    assert [e.event for e in result.events] == ["url_unverified"]
    assert result.urgent is True
    assert result.url_unverified[0].before == {"icon_url": _URL}
    assert _events(conn)[0][0] == "url_unverified"


def test_url_change_with_override_log_record_is_accepted(conn):
    """代表の差し替え(0020 の履歴に残る)は再基準化するだけで通知しない。"""
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL})
    _set_override(conn, _URL2)
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_B), urls={_MEMBER: _URL2})
    assert result.events == []
    assert _baseline(conn)["icon_url"] == _URL2


def test_round_trip_url_change_is_not_authorized_by_the_old_record(conn):
    """A→B→A の往復。B への指示記録は A へ戻す操作を正当化しない(窓=前回検査以降)。"""
    _set_override(conn, _URL)
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL})
    _set_override(conn, _URL2)
    icon_revalidate.revalidate(conn, prober=_prober(_FP_B), urls={_MEMBER: _URL2})
    # 攻撃者が override_log を残さずに URL を A へ戻す(A の set 記録は前回検査より前)。
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL})
    assert [e.event for e in result.events] == ["url_unverified"]
    assert result.urgent is True


def test_url_change_back_to_the_ledger_value_warns_but_is_not_urgent(conn):
    """台帳(config/org.yaml)の値への変更は git 経路。報告はするが緊急にはしない。"""
    ledger_url = org.members()[_MEMBER].icon_url
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL})
    result = icon_revalidate.revalidate(
        conn, prober=_prober(_FP_B), urls={_MEMBER: ledger_url}
    )
    assert [e.event for e in result.events] == ["url_unverified"]
    assert result.url_unverified[0].after["source"] == "ledger"
    assert result.urgent is False


def test_reset_record_authorizes_return_to_the_ledger_value(conn):
    """上書きの解除(reset)で台帳値へ戻るのは正規の操作。通知しない。"""
    ledger_url = org.members()[_MEMBER].icon_url
    _set_override(conn, _URL)
    icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL})
    org.clear_icon_override(conn, _MEMBER, "representative")
    result = icon_revalidate.revalidate(
        conn, prober=_prober(_FP_B), urls={_MEMBER: ledger_url}
    )
    assert result.events == []


def test_first_observation_of_a_url_is_not_flagged(conn):
    """初回観測は比較対象が無い。照合の対象外にする(全メンバーが毎回警告にならない)。"""
    result = icon_revalidate.revalidate(conn, prober=_prober(_FP_A), urls={_MEMBER: _URL2})
    assert result.events == []


# ── 報告(#運営)─────────────────────────────────────────────────────────────
def _outbox_rows(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, embed_json, urgent, author_member_id FROM press.outbox "
            "WHERE embed_json->>'title' = 'アイコン再検証' ORDER BY id"
        )
        return cur.fetchall()


def test_run_revalidation_is_silent_without_events(conn, run_id):
    """遷移が無い日は投稿しない。実行の事実は meta.runs に残る。"""
    icon_revalidate.run_revalidation(
        conn, run_id, prober=_prober(_FP_A), urls={_MEMBER: _URL}
    )
    assert _outbox_rows(conn) == []


def test_run_revalidation_enqueues_report_on_change(conn, run_id):
    urls = {_MEMBER: _URL}
    icon_revalidate.run_revalidation(conn, run_id, prober=_prober(_FP_A), urls=urls)
    result = icon_revalidate.run_revalidation(conn, run_id, prober=_prober(_FP_B), urls=urls)

    rows = _outbox_rows(conn)
    assert len(rows) == 1
    channel, embed_json, urgent, author_member_id = rows[0]
    assert channel == icon_revalidate.OPS_CHANNEL
    assert urgent is False
    # 発信者は監査キャラクター。内部キーは 0032 の列へ分離されている。
    assert author_member_id == org.member_for_role(icon_revalidate.REPORT_ROLE).id
    assert org.AUTHOR_MEMBER_KEY not in embed_json["author"]
    assert any(_MEMBER in f["name"] for f in embed_json["fields"])
    assert result.as_runtime() == {
        "checked": 1, "changed": 1, "errors": 0, "cleared": 0, "url_unverified": 0
    }


def test_report_embed_is_flash_colored_only_for_changes(conn):
    changed = icon_revalidate.RevalidationResult(
        checked=1,
        events=[
            icon_revalidate.IconCheckEvent(
                member_id=_MEMBER, event="changed", icon_url=_URL,
                before=_FP_A.as_dict(), after=_FP_B.as_dict(),
            )
        ],
    )
    only_error = icon_revalidate.RevalidationResult(
        checked=1,
        events=[
            icon_revalidate.IconCheckEvent(
                member_id=_MEMBER, event="error", icon_url=_URL, detail="到達不能"
            )
        ],
    )
    assert icon_revalidate.build_report_embed(changed)["color"] != (
        icon_revalidate.build_report_embed(only_error)["color"]
    )


# ── 実効 URL の解決 ──────────────────────────────────────────────────────────
def test_urls_default_to_effective_members(conn):
    """``urls`` 未指定なら台帳+0020 の上書きを重ねた実効値を検査する。"""
    probe = _prober(_FP_A)
    result = icon_revalidate.revalidate(conn, prober=probe)
    assert result.checked == len(org.members())
    assert set(probe.calls) == {m.icon_url for m in org.effective_members(conn).values()}
