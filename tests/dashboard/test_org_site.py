"""組織サイト化(組織/承認・通知/規則/計画ページ)の DB 層・ローダのテスト。

2026-08-03 代表指示「組織の動きがわかるホームページ」→ ダッシュボード拡張。
UI(app.py)自体はテスト対象外の方針を維持し、``queries.py`` の追加分だけを検証する。
governance.decisions / meta.runs は残留クリア対象外のため、件数でなく
「挿入した行が結果に含まれる」ことを見る(rollback 隔離)。
"""

from __future__ import annotations

import uuid

import pytest
import queries

# ── 接続の分離(独立役員審査 2026-08-03 重大-2)────────────────────────────────
# 読取ページと役員室は**別 DB ロール**で接続する。権限の実体は DB 側
# (ops/deploy-dashboard.sh)だが、アプリが正しい DSN を選ぶことをここで固定する。


def test_connect_boardroom_uses_dedicated_dsn(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setenv(queries.BOARDROOM_URL_ENV, "postgresql://ryza_boardroom:pw@h/ryza")
    monkeypatch.setattr(
        queries.psycopg, "connect", lambda dsn, **kw: seen.update(dsn=dsn, kw=kw)
    )
    monkeypatch.setattr(queries, "connect", lambda **kw: pytest.fail("既定 DSN を使った"))
    queries.connect_boardroom()
    assert seen["dsn"] == "postgresql://ryza_boardroom:pw@h/ryza"
    assert seen["kw"] == {"autocommit": True}


def test_connect_boardroom_falls_back_to_default_dsn(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.delenv(queries.BOARDROOM_URL_ENV, raising=False)
    monkeypatch.setattr(queries, "connect", lambda **kw: seen.update(kw=kw))
    queries.connect_boardroom()
    assert seen["kw"] == {"autocommit": True}


# ── DB 層 ─────────────────────────────────────────────────────────────────────


def test_fetch_decisions_returns_inserted_row(conn):
    ref = f"test-{uuid.uuid4()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.decisions (proposal_ref, kind, decision, decided_by, note)
            VALUES (%s, 'pr', 'approve', 'tester', 'unit test')
            """,
            (ref,),
        )
    rows = queries.fetch_decisions(conn, limit=10)
    mine = next(r for r in rows if r["proposal_ref"] == ref)
    assert mine["decision"] == "approve"
    assert mine["kind"] == "pr"
    assert mine["decided_by"] == "tester"
    assert mine["is_vetoed"] is False
    assert mine["effective_decision"] == "approve"


def test_fetch_decisions_surfaces_veto(conn):
    """否認された承認を「承認済み」のまま表示しない(独立役員審査 0021 C-5)。

    ``governance.decisions`` を直読すると、代表が定款第3条の否認権を行使した
    事実がダッシュボードから消える。現決定 view 経由で読むことをテストで固定する。
    """
    ref = f"test-veto-{uuid.uuid4()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.decisions (proposal_ref, kind, decision, decided_by)
            VALUES (%s, 'pr', 'approve', 'tester')
            RETURNING id
            """,
            (ref,),
        )
        decision_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO governance.decision_vetoes
                (decision_id, vetoed_by, reason, origin)
            VALUES (%s, 'tester', '前提の誤りが判明したため否認', 'discord_button')
            """,
            (decision_id,),
        )
    mine = next(
        r for r in queries.fetch_decisions(conn, limit=10) if r["proposal_ref"] == ref
    )
    assert mine["is_vetoed"] is True
    assert mine["effective_decision"] == "vetoed"
    assert mine["decision"] == "approve"  # 何が承認されていたかは残る
    assert mine["veto_reason"].startswith("前提の誤り")


def test_fetch_running_runs_includes_started_run(conn, run):
    rows = queries.fetch_running_runs(conn)
    mine = next(r for r in rows if r["run_id"] == run.run_id)
    assert mine["job_name"] == "test.dashboard"


# ── リポジトリ内ファイルのローダ(DB 不要) ─────────────────────────────────────


def test_load_org_members_have_display_fields():
    org = queries.load_org()
    members = org["members"]
    assert len(members) >= 9
    for m in members:
        assert m["id"] and m["name"] and m["title"] and m["dept"] and m["tagline"]
        assert str(m["color"]).startswith("#")
        assert m["model_tier"] in {"fable", "mid", "light"}
        assert "icon_url" in m  # null 可(UI は頭文字フォールバック)


def test_load_governance_controls_and_protected_areas():
    gov = queries.load_governance()
    assert gov["protected_areas"], "保護領域が空"
    assert all(p.get("path") and p.get("area") for p in gov["protected_areas"])
    controls = gov["controls"]
    assert controls
    enforcement = {c["enforcement"] for c in controls}
    assert enforcement <= {"schema", "gate", "ci", "audit", "declaration"}


def test_load_reminders_rows():
    rows = queries.load_reminders()
    assert rows
    for r in rows:
        assert r["id"] and r["what"]
        # status は自由記述気味(pending / fired(...) / done)。UI は pending だけを拾う
        assert isinstance(r["status"], str) and r["status"]


def test_load_roadmap_phase_structure():
    roadmap = queries.load_roadmap()
    ids = [p["id"] for p in roadmap["phases"]]
    assert ids == ["p1", "p2", "p3", "p4", "p5", "p6"]
    for p in roadmap["phases"]:
        assert p["status"] in {"done", "doing", "todo", "future"}
        for m in p.get("milestones", []):
            assert m["status"] in {"done", "doing", "todo"}
    # Phase 4 が進行中で、代表待ち事項が curated されている(計画ページの表示前提)
    p4 = roadmap["phases"][3]
    assert p4["status"] == "doing"
    assert roadmap["awaiting_representative"]
