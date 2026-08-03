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
from ryza.governance import personas
from ryza.governance.decisions import RESERVED_KIND_BY_MATTER
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


def test_unknown_source_rejected_before_insert(conn, run_id):
    """語彙外の出所は INSERT 前に弾く(独立役員審査 0021 C-6 の対称化)。

    DB の CHECK が一次統制であることは変わらないが、CheckViolation は
    トランザクションを中断させ、同一トランザクションで議事録を書いている
    役員室の書込を巻き添えにする。
    """
    with pytest.raises(ValueError, match="未知の stance 出所"):
        record_stance(
            conn, role="cio", kind="claim", summary="出所不明",
            run_id=run_id, source="hallway",
        )
    # トランザクションが中断していない = 続けて正常な記録ができる。
    assert record_stance(
        conn, role="cio", kind="claim", summary="正常記録", run_id=run_id
    ) > 0
    conn.rollback()


def test_unknown_source_rejected_by_schema(conn, run_id):
    """アプリ検証を迂回しても CHECK が最後の防衛線(一次統制はスキーマ側)。"""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO governance.stances (role, kind, summary, run_id, source)
                VALUES ('cio', 'claim', '出所不明', %s, 'hallway')
                """,
                (run_id,),
            )
    conn.rollback()


def test_blind_mode_is_allowlist_not_denylist(conn, run_id):
    """盲検は allowlist — 語彙に後から足された出所は自動的に載らない(C-6)。

    denylist(会議由来を除外)だと、新しい出所を足した者・source の指定を忘れた
    書き手の双方が「盲検に載る」側へ倒れる(fail-open)。盲検の穴は静かに開いて
    気付かれないため、未知の出所は載せない側へ倒す。
    """
    assert set(personas.BLIND_INCLUDED_SOURCES) <= set(personas.SOURCES)
    # allowlist に無い既知の出所(committee)は載らない。
    unlisted = [s for s in personas.SOURCES if s not in personas.BLIND_INCLUDED_SOURCES]
    assert unlisted, "allowlist が全出所を含むなら盲検が機能していない"
    for source in unlisted:
        record_stance(
            conn, role="independent_officer", kind="claim",
            summary=f"{source} 由来の主張", run_id=run_id, source=source,
        )
    got = recent_stances(
        conn, "independent_officer", limit=50,
        include_sources=personas.BLIND_INCLUDED_SOURCES,
    )
    assert got == []
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
        conn, "cio", limit=50, include_sources=personas.BLIND_INCLUDED_SOURCES
    )
    assert [s.stance_id for s in got] == []
    conn.rollback()


# ── decisions の決定語彙(0019・定款 v0.4 第3条)──────────────────────────
# 定款第3条の3専決事項と decisions.kind の対応表は
# ``ryza.governance.decisions.RESERVED_KIND_BY_MATTER`` が単一の正
# (writer 側の事前検証と DB の CHECK が別々の対応表を持つと乖離するため)。
# **governance.yaml に専決事項を足したらそこに足す** —
# test_reserved_matters_cover_governance_yaml が漏れを検出する。


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
    # 双方向で突合する(独立役員審査 0021 C-11)。部分文字列検査だけでは
    # 「CHECK 側にだけ余分な kind がある」= 実際には禁止されていない専決事項が
    # あるのに Python 側が気付かない、という片方向の漏れを検出できない。
    assert _constraint_kind_set(row[0]) == set(RESERVED_KIND_BY_MATTER.values()), (
        f"CHECK の kind 集合 {_constraint_kind_set(row[0])} と "
        f"RESERVED_KIND_BY_MATTER {set(RESERVED_KIND_BY_MATTER.values())} が一致しない"
    )


def _constraint_kind_set(constraint_def: str) -> set[str]:
    """``CHECK (... kind <> ALL (ARRAY['a'::text, ...]))`` から kind 集合を取り出す。

    PostgreSQL は ``NOT IN (...)`` を ``<> ALL (ARRAY[...])`` に正規化して保存する。
    ``decision`` 側の比較値 ``'deemed'`` は ARRAY の外にあるため混入しない。
    """
    array_part = constraint_def[constraint_def.index("ARRAY[") + len("ARRAY["):]
    array_part = array_part[: array_part.index("]")]
    return {
        item.strip().split("::")[0].strip().strip("'")
        for item in array_part.split(",")
    }


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
_CURRENT_COLUMNS = (
    "effective_decision", "recorded_decision", "is_vetoed", "veto_reason",
    "revert_commit", "derived_effects_ref", "veto_id", "veto_kind",
)


def _new_veto(
    cur,
    decision_id: int,
    *,
    kind: str = "veto",
    reason: str = "リスク上限の緩和方向のため否認",
    vetoed_by: str = "999",
    revert_commit: str | None = None,
    derived_effects_ref: str | None = None,
) -> int:
    cur.execute(
        """
        INSERT INTO governance.decision_vetoes
            (decision_id, kind, vetoed_by, reason, revert_commit, derived_effects_ref)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING veto_id
        """,
        (decision_id, kind, vetoed_by, reason, revert_commit, derived_effects_ref),
    )
    return cur.fetchone()[0]


def _current(cur, proposal_ref: str) -> dict:
    cur.execute(
        f"""
        SELECT {", ".join(_CURRENT_COLUMNS)}
        FROM governance.current_decisions WHERE proposal_ref = %s
        """,  # noqa: S608 - 固定の列名タプル
        (proposal_ref,),
    )
    row = cur.fetchone()
    assert row is not None, f"現決定に {proposal_ref} が無い"
    return dict(zip(_CURRENT_COLUMNS, row, strict=True))


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


def test_explicit_approval_can_be_vetoed(conn):
    """明示承認も否認できる(定款は明示承認の撤回を禁じていない)。"""
    with conn.cursor() as cur:
        did = _new_decision(cur, "live-money-veto", "approve", kind="budget")
        assert _new_veto(cur, did, reason="増資額の再検討のため") > 0
        assert _current(cur, "live-money-veto")["effective_decision"] == "vetoed"
    conn.rollback()


@pytest.mark.parametrize("decision", ["reject", "question"])
def test_reject_and_question_cannot_be_vetoed(conn, decision):
    """却下・質問は否認できない(独立役員審査 0021 C-2)。

    却下に否認を1行付けると現決定は 'vetoed' を返し、「却下されている」という
    阻止の根拠が消える。将来この view を読んで発効を止める判定は fail-open で
    外れる(却下を否認して通す、という抜け道になる)。
    """
    with conn.cursor() as cur:
        did = _new_decision(cur, f"nonvetoable-{decision}", decision)
        with pytest.raises(psycopg.errors.RaiseException, match="否認できない"):
            _new_veto(cur, did)
    conn.rollback()


def test_unknown_veto_kind_rejected(conn):
    """否認行の種別語彙は CHECK で固定(veto|revert_complete|withdrawal)。"""
    with conn.cursor() as cur:
        did = _new_decision(cur, "veto-bad-kind", "deemed")
        with pytest.raises(psycopg.errors.CheckViolation):
            _new_veto(cur, did, kind="cancel")
    conn.rollback()


def test_withdrawal_clears_vetoed_state(conn):
    """否認の撤回で現決定は否認前に戻る(誤った decision_id への否認からの復旧)。

    0007 の UNIQUE(proposal_ref) により提案の再記録はできないため、撤回の表現が
    無いと誤操作からの復旧手段が存在しない(独立役員審査 0021 C-3)。
    """
    with conn.cursor() as cur:
        did = _new_decision(cur, "veto-withdrawn", "deemed")
        _new_veto(cur, did, reason="誤った対象への否認")
        assert _current(cur, "veto-withdrawn")["is_vetoed"] is True
        _new_veto(cur, did, kind="withdrawal", reason="対象取り違えのため否認を撤回")
        row = _current(cur, "veto-withdrawn")
    assert row["is_vetoed"] is False
    assert row["effective_decision"] == "deemed"  # 否認前の効力に戻る
    assert row["veto_kind"] == "withdrawal"       # 履歴自体は残る
    conn.rollback()


def test_revert_completion_is_appended_not_overwritten(conn):
    """否認 → 取消完了 を追記で表現し、現決定に最新の取消情報が映る。"""
    with conn.cursor() as cur:
        did = _new_decision(cur, "ips-rev-veto-then-revert", "deemed")
        _new_veto(cur, did, reason="否認(取消未完了)")
        _new_veto(
            cur, did, kind="revert_complete", reason="否認に伴う取消完了",
            revert_commit="deadbeef", derived_effects_ref="discord://運営/12345",
        )
        row = _current(cur, "ips-rev-veto-then-revert")
    assert row["revert_commit"] == "deadbeef"
    assert row["derived_effects_ref"] == "discord://運営/12345"
    assert row["is_vetoed"] is True  # 取消完了は否認を解除しない
    conn.rollback()


def test_uninformative_append_does_not_erase_existing_values(conn):
    """情報の無い追記が既記録を消さない(独立役員審査 0021 C-4)。

    view が行単位で最新1行を採ると、revert_commit を持たない後続の追記
    (例: 派生効果の追加報告)が記録済みの revert_commit を NULL で覆い隠す。
    列ごとに「最後に値が入った行」を採ることでこれを防ぐ。
    """
    with conn.cursor() as cur:
        did = _new_decision(cur, "veto-column-wise", "deemed")
        _new_veto(cur, did, reason="否認")
        _new_veto(
            cur, did, kind="revert_complete", reason="取消完了",
            revert_commit="cafebabe",
        )
        # revert_commit を持たない追記(派生効果だけの報告)。
        _new_veto(
            cur, did, kind="revert_complete", reason="派生効果の追加報告",
            derived_effects_ref="discord://運営/777",
        )
        row = _current(cur, "veto-column-wise")
    assert row["revert_commit"] == "cafebabe"          # 消えない
    assert row["derived_effects_ref"] == "discord://運営/777"
    conn.rollback()


def test_view_order_is_by_veto_id_not_timestamp(conn):
    """最新行の判定は veto_id 単独(独立役員審査 0021 C-10)。

    vetoed_at は呼び出し側が任意の値を渡せるため、過去日時の行を後から追記すると
    「最新の追記」と「最新の時刻」が食い違う。追記オンリー表で最後に書かれた行を
    一意に決めるのは IDENTITY だけ。
    """
    with conn.cursor() as cur:
        did = _new_decision(cur, "veto-stale-timestamp", "deemed")
        _new_veto(cur, did, reason="否認")
        # 過去日時を持つ撤回行を後から追記する。時刻順なら否認が最新に見える。
        cur.execute(
            """
            INSERT INTO governance.decision_vetoes
                (decision_id, kind, vetoed_by, reason, vetoed_at)
            VALUES (%s, 'withdrawal', '999', '撤回', now() - interval '10 days')
            """,
            (did,),
        )
        row = _current(cur, "veto-stale-timestamp")
    assert row["is_vetoed"] is False  # 後から書かれた撤回が勝つ
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


def test_decisions_are_append_only(conn):
    """承認記録そのものが UPDATE / DELETE 不可(独立役員審査 0021 C-1)。

    0021 が別表化を選んだ根拠は「decisions を UPDATE すると `Approved:` トレーラが
    指す承認記録の意味が遡及改変される」ことにある。原本が可変のままだと、派生記録
    (否認)だけを不変にしても証跡性は原本の可変性で決まってしまう(保護の逆転)。
    """
    with conn.cursor() as cur:
        did = _new_decision(cur, "decisions-append-only-1", "approve")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.decisions SET decision = 'reject' WHERE id = %s",
                (did,),
            )
    conn.rollback()
    with conn.cursor() as cur:
        did = _new_decision(cur, "decisions-append-only-2", "approve")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute("DELETE FROM governance.decisions WHERE id = %s", (did,))
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
