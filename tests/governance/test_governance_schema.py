"""governance スキーマ(0013/0019/0021/0022)とローダの DB 依存部分の受け入れテスト。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行する。
接続不可なら skip(Docker 未導入環境向け)。テストは commit せず rollback で隔離。
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
import yaml

from ryza.db.conn import connect
from ryza.governance.personas import assume_role, recent_stances, record_stance
from ryza.provenance import start_run


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def run_id(conn) -> int:
    """実 Run(meta.runs 行)。minutes/stances の run_id FK が要求する(不変原則3)。"""
    return start_run("test.governance", conn=conn).run_id


def _new_minute(cur, run_id: int, meeting: str = "investment_committee") -> int:
    cur.execute(
        """
        INSERT INTO governance.minutes (meeting, held_at, attendees, body_md, run_id)
        VALUES (%s, now(), %s, '# 議事録\n対話全文', %s)
        RETURNING minute_id
        """,
        (meeting, ["representative", "cio", "independent_officer"], run_id),
    )
    return cur.fetchone()[0]


# ── スキーマの存在 ──────────────────────────────────────────────────────────
def test_governance_tables_exist(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'governance'
            """
        )
        tables = {r[0] for r in cur.fetchall()}
    # decisions は 0007、minutes/minute_resolutions/stances は 0013、
    # decision_vetoes と current_decisions(view)は 0021。
    assert {
        "decisions", "minutes", "minute_resolutions", "stances",
        "decision_vetoes", "current_decisions",
    }.issubset(tables)


# ── 議事録と決議マーク ──────────────────────────────────────────────────────
def test_minutes_and_resolution_roundtrip(conn, run_id):
    with conn.cursor() as cur:
        minute_id = _new_minute(cur, run_id)
        cur.execute(
            """
            INSERT INTO governance.minute_resolutions
                (minute_id, seq, title, resolution_md, proposal_ref, resolved_by)
            VALUES (%s, 1, 'IPS 改訂第1号', '決議本文(反対意見含む)',
                    'ips-rev-2026-08', 'representative')
            RETURNING resolution_id
            """,
            (minute_id,),
        )
        assert cur.fetchone()[0] > 0
        # 同一議事録内の決議番号は一意(二重マーク防止)。
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """
                INSERT INTO governance.minute_resolutions
                    (minute_id, seq, title, resolution_md, resolved_by)
                VALUES (%s, 1, '重複', 'x', 'representative')
                """,
                (minute_id,),
            )
    conn.rollback()


def test_unknown_meeting_rejected(conn, run_id):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            _new_minute(cur, run_id, meeting="watercooler_chat")
    conn.rollback()


def test_resolution_by_non_representative_rejected(conn, run_id):
    """決議ボタンは代表のみ(05 §5)— resolved_by は CHECK で強制。"""
    with conn.cursor() as cur:
        minute_id = _new_minute(cur, run_id)
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO governance.minute_resolutions
                    (minute_id, seq, title, resolution_md, resolved_by)
                VALUES (%s, 1, '越権決議', 'x', 'cio')
                """,
                (minute_id,),
            )
    conn.rollback()


def test_minutes_run_id_requires_real_run(conn):
    """run_id は meta.runs への FK(不変原則3・0001 の慣行)。"""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _new_minute(cur, run_id=-1)
    conn.rollback()


def test_minutes_are_append_only(conn, run_id):
    """議事録・決議は証憑(05 §4)— UPDATE / DELETE は禁止。"""
    with conn.cursor() as cur:
        minute_id = _new_minute(cur, run_id)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.minutes SET body_md = '改竄' WHERE minute_id = %s",
                (minute_id,),
            )
    conn.rollback()
    run2 = start_run("test.governance", conn=conn).run_id
    with conn.cursor() as cur:
        minute_id = _new_minute(cur, run2)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "DELETE FROM governance.minutes WHERE minute_id = %s", (minute_id,)
            )
    conn.rollback()


# ── stances の書込/読出とローダ ─────────────────────────────────────────────
def test_stance_write_read_roundtrip(conn, run_id):
    sid = record_stance(
        conn, role="independent_officer", kind="concern",
        summary="バックテスト期間がカットオフ前を含む懸念", run_id=run_id,
    )
    assert sid > 0
    got = recent_stances(conn, "independent_officer")
    assert [s.stance_id for s in got][:1] == [sid]
    assert got[0].kind == "concern"
    assert got[0].role == "independent_officer"
    conn.rollback()


def test_stances_isolated_by_role(conn, run_id):
    """独立役員の着任プロンプトに執行側(CIO)の stances を混ぜない(05 §6-2)。

    注: これはローダ API(単一 role 読み)の慣習をテストで固定するものであり、
    DB レベルの強制(RLS・資格情報分離)は未実装(personas.py docstring 参照)。
    """
    record_stance(conn, role="cio", kind="claim", summary="CIO の主張", run_id=run_id)
    record_stance(
        conn, role="independent_officer", kind="concern",
        summary="独立役員の懸念", run_id=run_id,
    )
    ind = recent_stances(conn, "independent_officer", limit=50)
    assert all(s.role == "independent_officer" for s in ind)
    assert not any("CIO の主張" in s.summary for s in ind)
    conn.rollback()


def test_recent_stances_limit_and_order(conn, run_id):
    for i in range(5):
        record_stance(conn, role="audit", kind="claim", summary=f"指摘 {i}", run_id=run_id)
    got = recent_stances(conn, "audit", limit=3)
    assert len(got) == 3
    # 新しい順(stated_at 同時刻でも stance_id 降順で安定)。
    assert [s.summary for s in got] == ["指摘 4", "指摘 3", "指摘 2"]
    conn.rollback()


def test_stance_unknown_kind_rejected(conn, run_id):
    with pytest.raises(psycopg.errors.CheckViolation):
        record_stance(conn, role="cio", kind="applause", summary="拍手", run_id=run_id)
    conn.rollback()


# ── stances の追記オンリーと撤回行方式(独立役員審査 是正1)─────────────────
def test_stances_are_append_only(conn, run_id):
    sid = record_stance(conn, role="cio", kind="claim", summary="主張", run_id=run_id)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.stances SET summary = '改竄' WHERE stance_id = %s",
                (sid,),
            )
    conn.rollback()
    run2 = start_run("test.governance", conn=conn).run_id
    sid = record_stance(conn, role="cio", kind="claim", summary="主張", run_id=run2)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "DELETE FROM governance.stances WHERE stance_id = %s", (sid,)
            )
    conn.rollback()


def test_retraction_excludes_row_from_onboarding(conn, run_id):
    """撤回された行と撤回行自体は着任読み込みから除外される。"""
    sid = record_stance(
        conn, role="cio", kind="claim", summary="誤った主張", run_id=run_id
    )
    keep = record_stance(
        conn, role="cio", kind="concern", summary="残る懸念", run_id=run_id
    )
    record_stance(
        conn, role="cio", kind="retraction", summary="根拠データの誤りにより撤回",
        run_id=run_id, retracts=sid,
    )
    got = recent_stances(conn, "cio", limit=50)
    assert [s.stance_id for s in got] == [keep]
    conn.rollback()


def test_retraction_requires_target_and_same_role(conn, run_id):
    """retraction は retracts 必須(CHECK)・他 role の行は撤回できない(ローダ検証)。"""
    with pytest.raises(psycopg.errors.CheckViolation):
        record_stance(
            conn, role="cio", kind="retraction", summary="対象なし撤回", run_id=run_id
        )
    conn.rollback()
    run2 = start_run("test.governance", conn=conn).run_id
    sid = record_stance(
        conn, role="cio", kind="claim", summary="CIO の主張", run_id=run2
    )
    with pytest.raises(ValueError, match="撤回できない"):
        record_stance(
            conn, role="independent_officer", kind="retraction",
            summary="越権撤回", run_id=run2, retracts=sid,
        )
    conn.rollback()


# ── stances の出所種別と盲検着任(0022・議論規約3)─────────────────────────
def test_stance_source_defaults_to_direct(conn, run_id):
    """既定は 'direct' — 従来の書込経路は挙動を変えない(既存行も同値で据え置き)。"""
    sid = record_stance(conn, role="cio", kind="claim", summary="単独記録", run_id=run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT source FROM governance.stances WHERE stance_id = %s", (sid,))
        assert cur.fetchone()[0] == "direct"
    assert recent_stances(conn, "cio")[0].source == "direct"
    conn.rollback()


def test_unknown_source_rejected(conn, run_id):
    """語彙外の出所は CHECK で拒否(出所不明の行を作らせない)。"""
    with pytest.raises(psycopg.errors.CheckViolation):
        record_stance(
            conn, role="cio", kind="claim", summary="出所不明",
            run_id=run_id, source="hallway",
        )
    conn.rollback()


def test_blind_assume_role_excludes_meeting_sources(conn, run_id):
    """盲検着任は会議由来を読まない(代表の選好が盲検レビューへ透過しない)。"""
    for source in ("office_chat", "committee"):
        record_stance(
            conn, role="independent_officer", kind="claim",
            summary=f"{source} 由来の主張", run_id=run_id, source=source,
        )
    record_stance(
        conn, role="independent_officer", kind="concern",
        summary="個別レビュー由来の懸念", run_id=run_id,
    )
    blind = assume_role(conn, "independent_officer", limit=50, blind=True)
    assert "個別レビュー由来の懸念" in blind
    assert "office_chat 由来の主張" not in blind
    assert "committee 由来の主張" not in blind
    conn.rollback()


def test_assume_role_default_reads_all_sources(conn, run_id):
    """既定(blind=False)は従来どおり全出所を読む — 既存経路の挙動不変。"""
    record_stance(
        conn, role="independent_officer", kind="claim",
        summary="会議で述べた主張", run_id=run_id, source="office_chat",
    )
    prompt = assume_role(conn, "independent_officer", limit=50)
    assert "会議で述べた主張" in prompt
    conn.rollback()


def test_blind_mode_does_not_resurrect_retracted_stances(conn, run_id):
    """撤回判定は出所除外の影響を受けない(会議で撤回した主張は盲検でも復活しない)。"""
    sid = record_stance(
        conn, role="cio", kind="claim", summary="誤った主張", run_id=run_id
    )
    record_stance(
        conn, role="cio", kind="retraction", summary="会議で撤回",
        run_id=run_id, retracts=sid, source="office_chat",
    )
    got = recent_stances(
        conn, "cio", limit=50, exclude_sources=("office_chat", "committee")
    )
    assert [s.stance_id for s in got] == []
    conn.rollback()


# ── decisions の決定語彙(0019・定款 v0.4 第3条)──────────────────────────
# 定款第3条の3専決事項(config/governance.yaml の representative_reserved)と
# decisions.kind の対応。**governance.yaml に専決事項を足したらここも足す** —
# test_reserved_matters_cover_governance_yaml が漏れを検出する。
RESERVED_KIND_BY_MATTER = {
    "constitution_amendment": "constitution",  # 現 kind 語彙には未登録(0019 で先回り列挙)
    "live_money": "budget",
    "kill_switch_resume": "breaker_resume",
}


def _new_decision(
    cur,
    proposal_ref: str,
    decision: str,
    kind: str = "pr",
    decided_by: str | None = None,
) -> int:
    # みなし承認は代表の作為ではなく通知による自動発効(0019 C-4 の CHECK)。
    if decided_by is None:
        decided_by = "system:deemed" if decision == "deemed" else "representative"
    cur.execute(
        """
        INSERT INTO governance.decisions
            (proposal_ref, kind, decision, decided_by, note)
        VALUES (%s, %s, %s, %s, 'test')
        RETURNING id
        """,
        (proposal_ref, kind, decision, decided_by),
    )
    return cur.fetchone()[0]


def test_deemed_decision_accepted(conn):
    """みなし承認は decision='deemed' で記録できる(定款 v0.4 第3条)。"""
    with conn.cursor() as cur:
        assert _new_decision(cur, "ips-rev-2026-08-deemed", "deemed") > 0
    conn.rollback()


def test_explicit_and_deemed_are_distinct(conn):
    """明示承認と区別して残る — 監査の deemed_ratio 計算の前提(定款第3条)。"""
    with conn.cursor() as cur:
        _new_decision(cur, "live-money-2026-08", "approve", kind="budget")
        _new_decision(cur, "mandate-rev-2026-08", "deemed")
        cur.execute(
            """
            SELECT decision FROM governance.decisions
            WHERE proposal_ref IN ('live-money-2026-08', 'mandate-rev-2026-08')
            ORDER BY proposal_ref
            """
        )
        assert [r[0] for r in cur.fetchall()] == ["approve", "deemed"]
    conn.rollback()


def test_legacy_decisions_still_accepted(conn):
    """0007 の既存語彙は壊れない(0019 は語彙の拡大のみ)。"""
    with conn.cursor() as cur:
        for i, decision in enumerate(("approve", "reject", "question")):
            assert _new_decision(cur, f"legacy-{i}", decision) > 0
    conn.rollback()


def test_unknown_decision_rejected(conn):
    """語彙外の決定は CHECK で拒否される(承認記録の語彙を固定する)。"""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            _new_decision(cur, "rubber-stamp-2026-08", "deemed_approved")
    conn.rollback()


# ── 3専決事項は「みなし」で発効させられない(独立役員審査 C-2)──────────────
@pytest.mark.parametrize("kind", sorted(set(RESERVED_KIND_BY_MATTER.values())))
def test_reserved_matter_cannot_be_deemed(conn, kind):
    """3専決(定款第3条)の kind に decision='deemed' は付けられない。

    kind と decision は互いに独立な列なので、この CHECK が無いと
    (kind='budget', decision='deemed') で実弾投入の承認証跡を偽装できる。
    """
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            _new_decision(cur, f"forged-{kind}", "deemed", kind=kind)
    conn.rollback()


@pytest.mark.parametrize("kind", sorted(set(RESERVED_KIND_BY_MATTER.values())))
def test_reserved_matter_accepts_explicit_approval(conn, kind):
    """禁止されるのは 'deemed' だけ — 明示承認の経路は塞がない。

    'constitution' は 0019 が先回りで列挙した未登録 kind なので、
    decisions_kind_check(0012)の側で弾かれることを期待値として固定する。
    """
    expected_ok = kind != "constitution"
    with conn.cursor() as cur:
        if expected_ok:
            assert _new_decision(cur, f"explicit-{kind}", "approve", kind=kind) > 0
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                _new_decision(cur, f"explicit-{kind}", "approve", kind=kind)
    conn.rollback()


def test_reserved_matters_cover_governance_yaml(conn):
    """不変条件: governance.yaml の全専決事項が 'deemed' 不可の kind に対応する。

    定款第3条の representative_reserved に4つ目が足された(= 委任範囲が縮んだ)のに
    スキーマ側の禁止リストが据え置かれる、という乖離を検出する。
    """
    root = Path(__file__).resolve().parents[2]
    gov = yaml.safe_load((root / "config" / "governance.yaml").read_text("utf-8"))
    reserved = set(gov["representative_reserved"])
    assert reserved == set(RESERVED_KIND_BY_MATTER), (
        "governance.yaml の専決事項と RESERVED_KIND_BY_MATTER が乖離している。"
        "対応する kind を決め、0019 の decisions_deemed_not_reserved_check にも足すこと。"
    )
    # 対応表の kind が実際に DB 側で禁止されていることまで確認する(宣言で終わらせない)。
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid = 'governance.decisions'::regclass
              AND conname = 'decisions_deemed_not_reserved_check'
            """
        )
        row = cur.fetchone()
    assert row is not None, "decisions_deemed_not_reserved_check が存在しない"
    for matter, kind in RESERVED_KIND_BY_MATTER.items():
        assert f"'{kind}'" in row[0], f"{matter} の kind={kind} が CHECK に無い"


# ── deemed の実行主体はシステム(独立役員審査 C-4)────────────────────────
def test_deemed_by_representative_rejected(conn):
    """みなし承認は代表の作為ではない — decided_by は 'system:%' に限る。"""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            _new_decision(
                cur, "deemed-by-rep", "deemed", decided_by="representative"
            )
    conn.rollback()


def test_explicit_approval_by_representative_still_allowed(conn):
    """明示承認側に 'system:%' 制約は掛からない(C-4 の CHECK は deemed 限定)。"""
    with conn.cursor() as cur:
        assert (
            _new_decision(cur, "explicit-by-rep", "approve", decided_by="representative")
            > 0
        )
    conn.rollback()


# ── 事後否認と現決定 view(0021・定款 v0.4 第3条「いつでも否認できる」)────────
def _new_veto(
    cur,
    decision_id: int,
    *,
    reason: str = "リスク上限の緩和方向のため否認",
    vetoed_by: str = "999",
    revert_commit: str | None = None,
    derived_effects_ref: str | None = None,
) -> int:
    cur.execute(
        """
        INSERT INTO governance.decision_vetoes
            (decision_id, vetoed_by, reason, revert_commit, derived_effects_ref)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING veto_id
        """,
        (decision_id, vetoed_by, reason, revert_commit, derived_effects_ref),
    )
    return cur.fetchone()[0]


def _current(cur, proposal_ref: str) -> dict:
    cur.execute(
        """
        SELECT effective_decision, recorded_decision, is_vetoed, veto_reason,
               revert_commit, derived_effects_ref, veto_id
        FROM governance.current_decisions WHERE proposal_ref = %s
        """,
        (proposal_ref,),
    )
    row = cur.fetchone()
    assert row is not None, f"現決定に {proposal_ref} が無い"
    return dict(
        zip(
            ("effective_decision", "recorded_decision", "is_vetoed", "veto_reason",
             "revert_commit", "derived_effects_ref", "veto_id"),
            row,
            strict=True,
        )
    )


def test_veto_insert_and_view_reflects_it(conn):
    """否認を追記すると現決定 view が 'vetoed' を返す(元の決定は残る)。"""
    with conn.cursor() as cur:
        did = _new_decision(cur, "mandate-rev-veto", "deemed")
        assert _current(cur, "mandate-rev-veto")["effective_decision"] == "deemed"
        assert _new_veto(cur, did) > 0
        cur_row = _current(cur, "mandate-rev-veto")
    assert cur_row["effective_decision"] == "vetoed"
    assert cur_row["recorded_decision"] == "deemed"  # 何が承認されたかは失われない
    assert cur_row["is_vetoed"] is True
    assert "否認" in cur_row["veto_reason"]
    conn.rollback()


def test_explicit_approval_can_also_be_vetoed(conn):
    """否認は deemed 行に限らない(スキーマは全 decision に一般化 — 0021 の設計判断)。

    定款第3条の否認権はみなし承認の文脈で定められているが、明示承認の撤回を
    スキーマが拒むと証跡が DB の外へ逃げる。否認は効力を弱める方向にしか
    働かないため、一般化しても 3専決の統制は緩まない。
    """
    with conn.cursor() as cur:
        did = _new_decision(cur, "live-money-veto", "approve", kind="budget")
        assert _new_veto(cur, did, reason="増資額の再検討のため") > 0
        assert _current(cur, "live-money-veto")["effective_decision"] == "vetoed"
    conn.rollback()


def test_latest_veto_wins_in_view(conn):
    """1決定に複数行を許し、view は最新行を返す(否認 → 取消完了 を追記で表現)。"""
    with conn.cursor() as cur:
        did = _new_decision(cur, "ips-rev-veto-then-revert", "deemed")
        _new_veto(cur, did, reason="否認(取消未完了)")
        _new_veto(
            cur, did, reason="否認に伴う取消完了",
            revert_commit="deadbeef", derived_effects_ref="discord://運営/12345",
        )
        row = _current(cur, "ips-rev-veto-then-revert")
    assert row["revert_commit"] == "deadbeef"
    assert row["derived_effects_ref"] == "discord://運営/12345"
    conn.rollback()


def test_unvetoed_decision_passes_through_view(conn):
    """否認が無ければ現決定 = 記録された決定(view が既存読み口の上位互換)。"""
    with conn.cursor() as cur:
        _new_decision(cur, "pr-plain", "approve")
        row = _current(cur, "pr-plain")
    assert row["effective_decision"] == "approve"
    assert row["is_vetoed"] is False
    assert row["veto_id"] is None
    conn.rollback()


def test_veto_requires_existing_decision(conn):
    """存在しない decision_id への否認は FK が拒否する(宙に浮いた否認を作らせない)。"""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _new_veto(cur, -1)
    conn.rollback()


@pytest.mark.parametrize("field,value", [("reason", "   "), ("vetoed_by", "")])
def test_veto_requires_reason_and_actor(conn, field, value):
    """理由・否認者の空文字は CHECK で拒否(定款第3条の取消義務の起点となる情報)。"""
    with conn.cursor() as cur:
        did = _new_decision(cur, f"blank-{field}", "deemed")
        with pytest.raises(psycopg.errors.CheckViolation):
            _new_veto(cur, did, **{field: value})
    conn.rollback()


def test_vetoes_are_append_only(conn):
    """否認証跡は UPDATE / DELETE 不可(0013 の minutes/stances と同型)。"""
    with conn.cursor() as cur:
        did = _new_decision(cur, "veto-append-only-1", "deemed")
        vid = _new_veto(cur, did)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.decision_vetoes SET reason = '改竄' WHERE veto_id = %s",
                (vid,),
            )
    conn.rollback()
    with conn.cursor() as cur:
        did = _new_decision(cur, "veto-append-only-2", "deemed")
        vid = _new_veto(cur, did)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "DELETE FROM governance.decision_vetoes WHERE veto_id = %s", (vid,)
            )
    conn.rollback()


@pytest.mark.parametrize(
    "table",
    [
        "governance.decision_vetoes",
        "governance.decisions",
        "governance.minutes",
        "governance.minute_resolutions",
        "governance.stances",
    ],
)
def test_governance_tables_reject_truncate(conn, table):
    """TRUNCATE は行トリガを迂回する(0015 で実証)ため文トリガで封鎖する。

    否認証跡だけを守っても、議事録・承認記録が TRUNCATE できるなら
    「否認ゼロ」という監査上もっとも都合のよい状態を一撃で作れてしまう。
    """
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="TRUNCATE は禁止"):
            cur.execute(f"TRUNCATE {table} CASCADE")  # noqa: S608 - 固定リスト
    conn.rollback()


def test_assume_role_end_to_end(conn, run_id):
    """実 charter/system + DB の stances から着任プロンプトが組み上がる。"""
    record_stance(
        conn, role="independent_officer", kind="concern",
        summary="デモ資金スケールの外挿懸念", run_id=run_id,
    )
    prompt = assume_role(conn, "independent_officer", limit=5)
    assert "独立役員" in prompt
    assert "職務規程(charter)" in prompt
    assert "デモ資金スケールの外挿懸念" in prompt
    conn.rollback()
