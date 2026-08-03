"""みなし承認・事後否認の writer(src/ryza/governance/decisions.py)のテスト。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行し、
commit せず rollback で隔離する。接続不可なら skip。
"""

from __future__ import annotations

import json

import psycopg
import pytest

from ryza.bot.approvals import NotOwnerError, record_decision
from ryza.db.conn import connect
from ryza.governance import decisions as decisions_mod
from ryza.governance.decisions import (
    RESERVED_KINDS,
    DuplicateDecisionError,
    NotVetoableError,
    ProposalRefMismatchError,
    ReservedMatterError,
    current_decision,
    current_decision_by_id,
    main,
    record_deemed_approval,
    record_revert_completion,
    record_veto,
    record_veto_withdrawal,
)
from ryza.provenance import start_run

OWNER = "424242"
OWNERS = (OWNER,)
NOTICE = "discord://承認/1234567890"


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _deemed(conn, ref: str, kind: str = "other"):
    return record_deemed_approval(conn, ref, kind, NOTICE)


def _veto(conn, decision, reason: str = "リスク上限を緩める方向のため否認", **kw):
    """既定のオーナー・proposal_ref で否認を1件記録する。"""
    return record_veto(
        conn, decision.id, reason,
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=decision.proposal_ref, **kw,
    )


# ── みなし承認の記録(定款第3条3号・0019 C-3)──────────────────────────────
def test_record_deemed_approval_writes_deemed_row(conn):
    """decision='deemed'・decided_by='system:deemed'・通知参照つきで記録される。"""
    got = record_deemed_approval(
        conn, "https://github.com/x/pull/101", "pr", NOTICE
    )
    assert got.decided_by == "system:deemed"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision, decided_by, channel_msg_id, kind "
            "FROM governance.decisions WHERE id = %s",
            (got.id,),
        )
        assert cur.fetchone() == ("deemed", "system:deemed", NOTICE, "pr")
    conn.rollback()


def test_deemed_source_is_reflected_in_actor(conn):
    """発効源は decided_by='system:<source>' に載る(0019 の system:% CHECK に適合)。"""
    got = record_deemed_approval(
        conn, "ips-rev-2026-09", "other", NOTICE, source="ips_monthly_review"
    )
    assert got.decided_by == "system:ips_monthly_review"
    conn.rollback()


def test_deemed_row_appears_in_current_decisions(conn):
    """現決定 view から読める(A-18 の突合・deemed_ratio 集計の読み口)。"""
    _deemed(conn, "mandate-rev-2026-09")
    row = current_decision(conn, "mandate-rev-2026-09")
    assert row["effective_decision"] == "deemed"
    assert row["is_vetoed"] is False
    conn.rollback()


def test_current_decision_returns_none_for_unknown_ref(conn):
    assert current_decision(conn, "no-such-proposal-ref") is None
    conn.rollback()


# ── 3専決事項はみなし承認できない(定款第3条・0019 C-2)────────────────────
@pytest.mark.parametrize("kind", sorted(RESERVED_KINDS))
def test_reserved_kinds_rejected_before_insert(conn, kind):
    """スキーマに届く前に明確なエラーで弾く。

    CheckViolation はどの制約かが呼び出し側に伝わりにくく、かつトランザクションを
    中断させるため、通知と同一トランザクションで記録する設計では通知の書込まで
    巻き添えになる。スキーマ側の CHECK が一次統制であることは変わらない
    (test_reserved_matter_cannot_be_deemed が DB 側を直接検証している)。
    """
    with pytest.raises(ReservedMatterError, match="専決事項"):
        record_deemed_approval(conn, f"reserved-{kind}", kind, NOTICE)
    # トランザクションが中断していない = 続けて別の記録ができる。
    assert record_deemed_approval(conn, f"ok-after-{kind}", "pr", NOTICE).id > 0
    conn.rollback()


def test_unknown_kind_rejected(conn):
    with pytest.raises(ValueError, match="未知の提案種別"):
        record_deemed_approval(conn, "unknown-kind", "wishlist", NOTICE)
    conn.rollback()


@pytest.mark.parametrize("missing", ["proposal_ref", "notice_ref"])
def test_blank_required_fields_rejected(conn, missing):
    """通知参照は必須 — 定款第3条は通知を発効要件とする(通知なき発効は A-18 違反)。"""
    args = {"proposal_ref": "blank-test", "notice_ref": NOTICE}
    args[missing] = "  "
    with pytest.raises(ValueError, match=missing):
        record_deemed_approval(conn, args["proposal_ref"], "pr", args["notice_ref"])
    conn.rollback()


# ── 1提案=1決定(0007 の UNIQUE)──────────────────────────────────────────
def test_duplicate_proposal_ref_raises_clear_error(conn):
    """二重通知・リトライでも承認記録は増えない。UniqueViolation を包んで返す。"""
    _deemed(conn, "dup-ref", "pr")
    with pytest.raises(DuplicateDecisionError, match="1提案=1決定"):
        _deemed(conn, "dup-ref", "pr")
    # 事前検査で弾くためトランザクションは生きている(通知の書込を巻き添えにしない)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decisions WHERE proposal_ref = 'dup-ref'"
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_duplicate_against_explicit_decision_raises(conn):
    """明示承認済みの提案をみなし承認で上書きできない(承認経路の格上げ防止)。"""
    record_decision(conn, "explicit-then-deemed", "approve", OWNER, OWNERS, kind="pr")
    with pytest.raises(DuplicateDecisionError, match="approve"):
        _deemed(conn, "explicit-then-deemed", "pr")
    conn.rollback()


# ── 事後否認(定款第3条2号・0021)──────────────────────────────────────────
def test_record_veto_marks_decision_vetoed(conn):
    deemed = _deemed(conn, "veto-target")
    veto = _veto(conn, deemed)
    assert veto.veto_id > 0
    assert veto.kind == "veto"
    row = current_decision(conn, "veto-target")
    assert row["effective_decision"] == "vetoed"
    assert row["recorded_decision"] == "deemed"  # 何が発効していたかは残る
    assert row["vetoed_by"] == OWNER
    conn.rollback()


def test_record_veto_accepts_revert_and_derived_effects(conn):
    """取消コミットと取消不能な派生効果の参照を記録できる(第3条の報告義務)。"""
    deemed = _deemed(conn, "veto-with-revert")
    run_id = start_run("test.governance", conn=conn).run_id
    _veto(
        conn, deemed, "否認",
        revert_commit="0123abc", derived_effects_ref="discord://運営/999", run_id=run_id,
    )
    row = current_decision(conn, "veto-with-revert")
    assert row["revert_commit"] == "0123abc"
    assert row["derived_effects_ref"] == "discord://運営/999"
    conn.rollback()


def test_revert_completion_is_appended(conn):
    """取消完了は追記で表現し、現決定に反映される(追記オンリーのため UPDATE 不可)。"""
    deemed = _deemed(conn, "veto-two-step")
    _veto(conn, deemed, "否認(取消未完了)")
    assert current_decision(conn, "veto-two-step")["revert_commit"] is None
    got = record_revert_completion(
        conn, deemed.id, "否認に伴う取消完了",
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, revert_commit="feedface",
    )
    assert got.kind == "revert_complete"
    row = current_decision(conn, "veto-two-step")
    assert row["revert_commit"] == "feedface"
    assert row["is_vetoed"] is True  # 取消完了は否認を解除しない
    conn.rollback()


def test_uninformative_append_does_not_erase_revert_commit(conn):
    """情報の無い追記が既記録を消さない(独立役員審査 0021 C-4)。"""
    deemed = _deemed(conn, "veto-column-wise-writer")
    _veto(conn, deemed, "否認")
    record_revert_completion(
        conn, deemed.id, "取消完了", vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, revert_commit="cafebabe",
    )
    record_revert_completion(
        conn, deemed.id, "派生効果の追加報告", vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref,
        derived_effects_ref="discord://運営/777",
    )
    row = current_decision(conn, "veto-column-wise-writer")
    assert row["revert_commit"] == "cafebabe"
    assert row["derived_effects_ref"] == "discord://運営/777"
    conn.rollback()


def test_veto_withdrawal_restores_previous_state(conn):
    """否認の撤回で現決定は否認前に戻る(誤った対象への否認からの復旧 — C-3)。"""
    deemed = _deemed(conn, "veto-withdraw-writer")
    _veto(conn, deemed, "誤った対象への否認")
    got = record_veto_withdrawal(
        conn, deemed.id, "対象取り違えのため撤回",
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref,
    )
    assert got.kind == "withdrawal"
    row = current_decision(conn, "veto-withdraw-writer")
    assert row["is_vetoed"] is False
    assert row["effective_decision"] == "deemed"
    assert row["veto_kind"] == "withdrawal"  # 履歴は残る
    conn.rollback()


def test_explicit_approval_can_be_vetoed(conn):
    """明示承認も否認できる(定款は明示承認の撤回を禁じていない)。"""
    got = record_decision(
        conn, "explicit-veto", "approve", OWNER, OWNERS, kind="strategy_promotion"
    )
    record_veto(
        conn, got.id, "前提データの誤りが判明したため",
        vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="explicit-veto",
    )
    assert current_decision(conn, "explicit-veto")["effective_decision"] == "vetoed"
    conn.rollback()


# ── 否認できない決定・非オーナー・対象取り違え(独立役員審査 C-2 / C-3)────────
@pytest.mark.parametrize("decision", ["reject", "question"])
def test_reject_and_question_are_not_vetoable(conn, decision):
    """却下・質問は否認できない — 否認できると阻止の根拠が fail-open で消える。"""
    got = record_decision(
        conn, f"nonvetoable-{decision}", decision, OWNER, OWNERS, kind="pr"
    )
    with pytest.raises(NotVetoableError, match="否認できない"):
        record_veto(
            conn, got.id, "却下を覆す",
            vetoed_by=OWNER, owner_ids=OWNERS,
            expected_proposal_ref=f"nonvetoable-{decision}",
        )
    # 事前検査で弾くためトランザクションは生きている。
    assert _deemed(conn, f"after-nonvetoable-{decision}").id > 0
    conn.rollback()


def test_reject_veto_blocked_by_schema_trigger(conn):
    """アプリ検証を迂回してもトリガが最後の防衛線(一次統制はスキーマ側)。"""
    got = record_decision(conn, "nonvetoable-raw", "reject", OWNER, OWNERS, kind="pr")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="否認できない"):
            cur.execute(
                """
                INSERT INTO governance.decision_vetoes (decision_id, vetoed_by, reason)
                VALUES (%s, %s, '直接 INSERT')
                """,
                (got.id, OWNER),
            )
    conn.rollback()


def test_non_owner_veto_rejected(conn):
    """否認は代表の専権(定款第3条)— record_decision と同型のオーナー検証。"""
    deemed = _deemed(conn, "veto-by-non-owner")
    with pytest.raises(NotOwnerError):
        record_veto(
            conn, deemed.id, "越権否認",
            vetoed_by="999999", owner_ids=OWNERS,
            expected_proposal_ref=deemed.proposal_ref,
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decision_vetoes WHERE decision_id = %s",
            (deemed.id,),
        )
        assert cur.fetchone()[0] == 0
    conn.rollback()


def test_proposal_ref_mismatch_rejected(conn):
    """decision_id の取り違えは INSERT 前に失敗する(無関係な承認を汚染しない)。"""
    a = _deemed(conn, "veto-ref-a")
    _deemed(conn, "veto-ref-b")
    with pytest.raises(ProposalRefMismatchError, match="veto-ref-a"):
        record_veto(
            conn, a.id, "取り違え否認",
            vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="veto-ref-b",
        )
    assert current_decision(conn, "veto-ref-a")["is_vetoed"] is False
    conn.rollback()


def test_veto_of_unknown_decision_raises_clear_error(conn):
    """FK 違反を待たず明確なエラーにする(FK 違反はトランザクションを中断させる)。"""
    with pytest.raises(ValueError, match="存在しない"):
        record_veto(
            conn, -1, "対象なし否認",
            vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="whatever",
        )
    assert _deemed(conn, "after-bad-veto").id > 0
    conn.rollback()


@pytest.mark.parametrize("field", ["reason", "vetoed_by", "expected_proposal_ref"])
def test_veto_requires_non_blank_fields(conn, field):
    deemed = _deemed(conn, f"veto-blank-{field}")
    kwargs = {
        "reason": "理由",
        "vetoed_by": OWNER,
        "expected_proposal_ref": deemed.proposal_ref,
    }
    kwargs[field] = "   "
    with pytest.raises(ValueError, match=field):
        record_veto(
            conn, deemed.id, kwargs["reason"],
            vetoed_by=kwargs["vetoed_by"], owner_ids=OWNERS,
            expected_proposal_ref=kwargs["expected_proposal_ref"],
        )
    conn.rollback()


# ── 現決定の ID 引き(A-18-1 のトレーラ突合の読み口)────────────────────────
def test_current_decision_by_id_reflects_veto(conn):
    """ID 引きでも否認が反映される(decisions 直読なら承認に見えてしまう)。"""
    deemed = _deemed(conn, "by-id-ref")
    assert current_decision_by_id(conn, deemed.id)["effective_decision"] == "deemed"
    _veto(conn, deemed, "否認")
    assert current_decision_by_id(conn, deemed.id)["effective_decision"] == "vetoed"
    assert current_decision_by_id(conn, -1) is None
    conn.rollback()


# ── CLI(python -m ryza.governance.decisions --deemed ...)───────────────────
def test_cli_dry_run_prints_notice_embed(capsys):
    """--dry-run は DB に触れず、投稿される embed を出す(通知文面の事前確認)。"""
    from ryza.governance.notices import parse_deemed_notice

    rc = main([
        "--deemed", "--proposal-ref", "https://x/pull/1",
        "--kind", "pr", "--notice", "保護領域の変更", "--dry-run",
    ])
    assert rc == 0
    embed = json.loads(capsys.readouterr().out)
    assert parse_deemed_notice(embed) == "https://x/pull/1"


def test_cli_requires_deemed_flag(capsys):
    rc = main(["--proposal-ref", "x", "--kind", "pr", "--notice", "y", "--dry-run"])
    assert rc == 2


@pytest.mark.parametrize("kind", sorted(RESERVED_KINDS))
def test_cli_rejects_reserved_kinds(kind):
    """3専決事項は CLI の選択肢に存在しない(定款第3条 — 明示承認のみ)。"""
    with pytest.raises(SystemExit):
        main(["--deemed", "--proposal-ref", "x", "--kind", kind, "--notice", "y", "--dry-run"])


def test_cli_reports_missing_arguments(capsys):
    """--deemed 単体では参照・種別・文面が必須(何が足りないかを名指しする)。"""
    rc = main(["--deemed", "--kind", "pr", "--dry-run"])
    assert rc == 1
    assert "--proposal-ref" in capsys.readouterr().err


# ── CLI 簡易形(--deemed-for-pr: gh api で PR から文面を組み立てる)───────────
PR_JSON = {
    "number": 99,
    "html_url": "https://github.com/klonyapin/ryza/pull/99",
    "title": "feat(gate): コンプラゲートの閾値を追加",
    "state": "open",
    "merged": False,
}


@pytest.fixture
def fake_gh(monkeypatch):
    """``gh api`` を差し替え、呼ばれたパスを記録する(実ネットワークは使わない)。"""
    calls: list[str] = []

    def _install(pr_json: dict, files: list[str] | None = None, *, files_fail: bool = False):
        def fake_gh_api(path: str, *, paginate: bool = False, jq: str | None = None):
            calls.append(path)
            if path.endswith("/files"):
                if files_fail:
                    raise decisions_mod.PullRequestLookupError("files 取得に失敗")
                return list(files or [])
            return pr_json

        monkeypatch.setattr(decisions_mod, "_gh_api", fake_gh_api)
        return calls

    return _install


def test_cli_deemed_for_pr_builds_the_notice(fake_gh, capsys):
    """PR 番号1つで参照・種別・文面が埋まる(叩き忘れを減らすための簡易形)。"""
    from ryza.governance.notices import parse_deemed_notice

    calls = fake_gh(PR_JSON, ["src/ryza/gate/limits.py", "migrations/0025_x.sql"])
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/gate-review.md", "--dry-run",
    ])
    assert rc == 0
    embed = json.loads(capsys.readouterr().out)
    assert parse_deemed_notice(embed) == PR_JSON["html_url"]
    assert any(f["value"] == "pr" for f in embed["fields"])  # kind の既定は pr
    body = embed["description"]
    assert "PR #99" in body and PR_JSON["title"] in body
    assert "src/ryza/gate/limits.py" in body
    assert "docs/reviews/gate-review.md" in body  # 審査参照が文面に残る
    assert calls == [
        "repos/:owner/:repo/pulls/99",
        "repos/:owner/:repo/pulls/99/files",
    ]


def test_cli_deemed_for_pr_requires_a_review_reference(fake_gh, capsys):
    """審査を経ていない変更をワンコマンドで発効させない(reminders deemed-auto-announce ②)。"""
    fake_gh(PR_JSON)
    rc = main(["--deemed-for-pr", "99", "--dry-run"])
    assert rc == 1
    assert "--review" in capsys.readouterr().err


def test_cli_deemed_for_pr_refuses_a_closed_pr(fake_gh, capsys):
    """取り下げられた PR を発効させない(通知だけ出て取消義務が残る)。"""
    fake_gh({**PR_JSON, "state": "closed", "merged": False})
    rc = main(["--deemed-for-pr", "99", "--review", "docs/reviews/x.md", "--dry-run"])
    assert rc == 1
    assert "クローズ済み" in capsys.readouterr().err


def test_cli_deemed_for_pr_accepts_a_merged_pr(fake_gh, capsys):
    """マージ済み PR は対象になる(事後の記録漏れを CLI で埋められる — A-18-7 の是正手段)。"""
    fake_gh({**PR_JSON, "state": "closed", "merged": True})
    rc = main(["--deemed-for-pr", "99", "--review", "docs/reviews/x.md", "--dry-run"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["fields"]


def test_cli_explicit_arguments_win_over_the_generated_ones(fake_gh, capsys):
    """自動生成の文面が状況に合わないときは手で上書きできる。"""
    fake_gh(PR_JSON)
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/x.md",
        "--notice", "手書きの要旨", "--kind", "other", "--dry-run",
    ])
    assert rc == 0
    embed = json.loads(capsys.readouterr().out)
    assert "手書きの要旨" in embed["description"] and "PR #99" not in embed["description"]
    assert any(f["value"] == "other" for f in embed["fields"])


def test_cli_deemed_for_pr_survives_a_file_list_failure(fake_gh, capsys):
    """変更ファイル一覧は文面の補助でしかない — 取れなくても発効そのものは止めない。"""
    fake_gh(PR_JSON, files_fail=True)
    rc = main(["--deemed-for-pr", "99", "--review", "docs/reviews/x.md", "--dry-run"])
    assert rc == 0
    assert "変更ファイル" not in json.loads(capsys.readouterr().out)["description"]


def test_cli_deemed_for_pr_reports_gh_failure(monkeypatch, capsys):
    """gh が失敗したら黙って別の参照で発効させず、失敗として返す。"""

    def failing(path, *, paginate=False, jq=None):
        raise decisions_mod.PullRequestLookupError("gh api に失敗した(未認証)")

    monkeypatch.setattr(decisions_mod, "_gh_api", failing)
    rc = main(["--deemed-for-pr", "99", "--review", "docs/reviews/x.md", "--dry-run"])
    assert rc == 1
    assert "未認証" in capsys.readouterr().err


def test_build_pr_notice_truncates_long_file_lists():
    """embed のフィールド長に収める(全件は PR を見れば分かる)。"""
    files = tuple(f"src/f{i}.py" for i in range(30))
    pr = decisions_mod.PullRequestRef(
        number=7, url="https://x/pull/7", title="t", state="open", merged=False, files=files
    )
    body = decisions_mod.build_pr_notice(pr, "docs/reviews/r.md")
    assert "変更ファイル(30 件)" in body
    assert f"ほか {30 - decisions_mod.NOTICE_FILE_LIMIT} 件" in body


def test_cli_records_and_notifies_on_a_fresh_connection(migrated_db, monkeypatch):
    """CLI 本経路(IDLE の新規接続)で記録と通知が同一トランザクションに入る(軽微-12)。

    ``governance.decisions`` は追記オンリー(0021)で DELETE できないため、CLI の
    ``commit`` を無効化した接続を渡して確定させずに検証する。IDLE 接続で
    ``announce_deemed_approval`` が呼ばれる分岐そのものは本物を通る。
    """
    import ryza.db.conn as db_conn

    inner = connect()

    class _NoCommitConn:
        """commit / close だけ握り潰す薄い委譲(テスト DB に残留を作らないため)。"""

        def commit(self):
            pass

        def close(self):
            pass

        def __getattr__(self, name):
            return getattr(inner, name)

    monkeypatch.setattr(db_conn, "connect", lambda autocommit=False: _NoCommitConn())
    ref = "https://github.com/klonyapin/ryza/pull/9911"
    run_id = None
    try:
        rc = main(["--deemed", "--proposal-ref", ref, "--kind", "pr", "--notice", "保護領域の変更"])
        with inner.cursor() as cur:
            cur.execute(
                "SELECT channel_msg_id FROM governance.decisions WHERE proposal_ref = %s", (ref,)
            )
            notice_ref = cur.fetchone()[0]
            cur.execute(
                "SELECT channel, run_id FROM press.outbox WHERE id = %s",
                (int(notice_ref.removeprefix("outbox:")),),
            )
            channel, run_id = cur.fetchone()
        assert rc == 0
        assert current_decision(inner, ref)["effective_decision"] == "deemed"
        assert channel == "approval"  # 記録と通知が同じトランザクションに入っている
        inner.rollback()
        assert current_decision(inner, ref) is None  # commit させていない
    finally:
        if run_id is not None:  # Run は自前接続で確定するので消しておく
            inner.rollback()
            with inner.cursor() as cur:
                cur.execute("DELETE FROM meta.runs WHERE run_id = %s", (run_id,))
            inner.commit()
        inner.close()


def test_veto_is_append_only(conn):
    """記録した否認は書き換えられない(0021 の追記オンリートリガ)。"""
    deemed = _deemed(conn, "veto-immutable")
    veto = _veto(conn, deemed, "否認")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.decision_vetoes SET reason = '改竄' WHERE veto_id = %s",
                (veto.veto_id,),
            )
    conn.rollback()
