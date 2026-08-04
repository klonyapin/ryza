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
    validate_proposal_ref,
)
from ryza.provenance import start_run

OWNER = "424242"
OWNERS = (OWNER,)
NOTICE = "discord://承認/1234567890"
# 0030 の origin。writer を直接呼ぶテストは「人手の直接呼び出し」= cli にあたる。
ORIGIN = "cli"


@pytest.fixture
def conn(migrated_db):
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _mref(name: str) -> str:
    """テストの短い ID を F-10 の manual: 形式に揃える(既に前置済みの値はそのまま)。

    F-10 で proposal_ref は 3 形式に制限された。テストは意味を持たない短い ID を大量に
    使うが、そのまま渡すと writer の検証で弾かれる。書き込み時のみ manual:… に揃えて
    「1提案=1決定」の意味を保つ(生成規則が決まっているので UNIQUE を邪魔しない)。
    """
    if name.startswith(("manual:", "decision:", "https://github.com/")):
        return name
    return f"manual:{name}"


def _deemed(conn, ref: str, kind: str = "other"):
    return record_deemed_approval(conn, _mref(ref), kind, NOTICE)


def _veto(
    conn, decision, reason: str = "リスク上限を緩める方向のため否認",
    *, origin: str = ORIGIN, **kw,
):
    """既定のオーナー・proposal_ref で否認を1件記録する。"""
    return record_veto(
        conn, decision.id, reason,
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=decision.proposal_ref, origin=origin, **kw,
    )


# ── F-10: proposal_ref 様式の検証(純粋関数)──────────────────────────────
@pytest.mark.parametrize(
    "value",
    [
        # (a) PR URL
        "https://github.com/klonyapin/ryza/pull/122",
        "https://github.com/x/y/pull/1",
        "https://github.com/A-B_1/repo.name/pull/9999",
        # (b) decision:<数字>
        "decision:1",
        "decision:1234567890",
        # (c) manual:<スラッグ>
        "manual:abc",  # 下限 3 文字
        "manual:a" + "b" * 63,  # 上限 64 文字
        "manual:ips-2026-09",
        "manual:frozen-ex-001",
        "manual:with_underscore",
        "manual:0abc",  # 先頭は数字も OK
    ],
)
def test_validate_proposal_ref_accepts_3_forms(value):
    """F-10 の 3 形式(PR URL / decision:数字 / manual:スラッグ)を素通ししていること。"""
    assert validate_proposal_ref(value) == value


@pytest.mark.parametrize(
    "value",
    [
        # 短い任意文字列(旧テストで衝突していた形)
        "dup-ref",
        "prop-1",
        "explicit-ref",
        # 別リポジトリ(github.com 以外)の PR URL は不可
        "https://gitlab.com/x/y/pull/1",
        "http://github.com/x/y/pull/1",  # http は不可
        "https://github.com/x/pull/1",  # owner のみで repo なし
        "https://github.com/x/y/pull/0",  # PR 番号 0
        # decision: が数字でない
        "decision:abc",
        "decision:",
        "decision:0",
        # manual: が短すぎ / 長すぎ / 大文字 / 空白 / メタ文字
        "manual:ab",  # 3 文字未満
        "manual:" + "a" * 65,  # 65 文字
        "manual:ABC",  # 大文字不可
        "manual:has space",
        "manual:has.dot",  # . は許可外
        "manual:",
        # 空白入り / 空文字 / 型不一致
        " manual:ok-ref",
        "manual:ok-ref ",
        "",
        "   ",
        "manual:改行\n入り",
    ],
)
def test_validate_proposal_ref_rejects_bad(value):
    with pytest.raises(ValueError):
        validate_proposal_ref(value)


def test_validate_proposal_ref_rejects_non_str():
    with pytest.raises(ValueError):
        validate_proposal_ref(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        validate_proposal_ref(None)  # type: ignore[arg-type]


def test_writer_calls_validate_proposal_ref(conn):
    """writer(record_deemed_approval)経由でも同じ検証が効くこと(A-12-04 の一次責任)。"""
    with pytest.raises(ValueError, match="許可"):
        record_deemed_approval(conn, "short-ref", "pr", NOTICE)
    conn.rollback()


# ── みなし承認の記録(定款第3条3号・0019 C-3)──────────────────────────────
def test_record_deemed_approval_writes_deemed_row(conn):
    """decision='deemed'・decided_by='system:deemed'・通知参照つきで記録される。"""
    got = record_deemed_approval(
        conn, "https://github.com/x/y/pull/101", "pr", NOTICE
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
        conn, "manual:ips-rev-2026-09", "other", NOTICE, source="ips_monthly_review"
    )
    assert got.decided_by == "system:ips_monthly_review"
    conn.rollback()


def test_deemed_row_appears_in_current_decisions(conn):
    """現決定 view から読める(A-18 の突合・deemed_ratio 集計の読み口)。"""
    _deemed(conn, "mandate-rev-2026-09")
    row = current_decision(conn, "manual:mandate-rev-2026-09")
    assert row["effective_decision"] == "deemed"
    assert row["is_vetoed"] is False
    conn.rollback()


def test_current_decision_returns_none_for_unknown_ref(conn):
    assert current_decision(conn, "manual:no-such-proposal-ref") is None
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
        record_deemed_approval(conn, f"manual:reserved-{kind}", kind, NOTICE)
    # トランザクションが中断していない = 続けて別の記録ができる。
    assert record_deemed_approval(conn, f"manual:ok-after-{kind}", "pr", NOTICE).id > 0
    conn.rollback()


def test_unknown_kind_rejected(conn):
    with pytest.raises(ValueError, match="未知の提案種別"):
        record_deemed_approval(conn, "manual:unknown-kind", "wishlist", NOTICE)
    conn.rollback()


@pytest.mark.parametrize("missing", ["proposal_ref", "notice_ref"])
def test_blank_required_fields_rejected(conn, missing):
    """通知参照は必須 — 定款第3条は通知を発効要件とする(通知なき発効は A-18 違反)。"""
    args = {"proposal_ref": "manual:blank-test", "notice_ref": NOTICE}
    args[missing] = "  "
    with pytest.raises(ValueError, match=missing):
        record_deemed_approval(conn, args["proposal_ref"], "pr", args["notice_ref"])
    conn.rollback()


# ── 1提案=1決定(0007 の UNIQUE)──────────────────────────────────────────
def test_duplicate_proposal_ref_raises_clear_error(conn):
    """二重通知・リトライでも承認記録は増えない。UniqueViolation を包んで返す。"""
    _deemed(conn, "manual:dup-ref", "pr")
    with pytest.raises(DuplicateDecisionError, match="1提案=1決定"):
        _deemed(conn, "manual:dup-ref", "pr")
    # 事前検査で弾くためトランザクションは生きている(通知の書込を巻き添えにしない)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM governance.decisions WHERE proposal_ref = 'manual:dup-ref'"
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_duplicate_against_explicit_decision_raises(conn):
    """明示承認済みの提案をみなし承認で上書きできない(承認経路の格上げ防止)。"""
    record_decision(conn, "manual:explicit-then-deemed", "approve", OWNER, OWNERS, kind="pr")
    with pytest.raises(DuplicateDecisionError, match="approve"):
        _deemed(conn, "manual:explicit-then-deemed", "pr")
    conn.rollback()


# ── 事後否認(定款第3条2号・0021)──────────────────────────────────────────
def test_record_veto_marks_decision_vetoed(conn):
    deemed = _deemed(conn, "manual:veto-target")
    veto = _veto(conn, deemed)
    assert veto.veto_id > 0
    assert veto.kind == "veto"
    row = current_decision(conn, "manual:veto-target")
    assert row["effective_decision"] == "vetoed"
    assert row["recorded_decision"] == "deemed"  # 何が発効していたかは残る
    assert row["vetoed_by"] == OWNER
    conn.rollback()


def test_record_veto_accepts_revert_and_derived_effects(conn):
    """取消コミットと取消不能な派生効果の参照を記録できる(第3条の報告義務)。"""
    deemed = _deemed(conn, "manual:veto-with-revert")
    run_id = start_run("test.governance", conn=conn).run_id
    _veto(
        conn, deemed, "否認",
        revert_commit="0123abc", derived_effects_ref="discord://運営/999", run_id=run_id,
    )
    row = current_decision(conn, "manual:veto-with-revert")
    assert row["revert_commit"] == "0123abc"
    assert row["derived_effects_ref"] == "discord://運営/999"
    conn.rollback()


def test_revert_completion_is_appended(conn):
    """取消完了は追記で表現し、現決定に反映される(追記オンリーのため UPDATE 不可)。"""
    deemed = _deemed(conn, "manual:veto-two-step")
    _veto(conn, deemed, "否認(取消未完了)")
    assert current_decision(conn, "manual:veto-two-step")["revert_commit"] is None
    got = record_revert_completion(
        conn, deemed.id, "否認に伴う取消完了",
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, origin=ORIGIN,
        revert_commit="feedface",
    )
    assert got.kind == "revert_complete"
    row = current_decision(conn, "manual:veto-two-step")
    assert row["revert_commit"] == "feedface"
    assert row["is_vetoed"] is True  # 取消完了は否認を解除しない
    conn.rollback()


def test_uninformative_append_does_not_erase_revert_commit(conn):
    """情報の無い追記が既記録を消さない(独立役員審査 0021 C-4)。"""
    deemed = _deemed(conn, "manual:veto-column-wise-writer")
    _veto(conn, deemed, "否認")
    record_revert_completion(
        conn, deemed.id, "取消完了", vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, origin=ORIGIN,
        revert_commit="cafebabe",
    )
    record_revert_completion(
        conn, deemed.id, "派生効果の追加報告", vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, origin=ORIGIN,
        derived_effects_ref="discord://運営/777",
    )
    row = current_decision(conn, "manual:veto-column-wise-writer")
    assert row["revert_commit"] == "cafebabe"
    assert row["derived_effects_ref"] == "discord://運営/777"
    conn.rollback()


def test_veto_withdrawal_restores_previous_state(conn):
    """否認の撤回で現決定は否認前に戻る(誤った対象への否認からの復旧 — C-3)。"""
    deemed = _deemed(conn, "manual:veto-withdraw-writer")
    _veto(conn, deemed, "誤った対象への否認")
    got = record_veto_withdrawal(
        conn, deemed.id, "対象取り違えのため撤回",
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, origin=ORIGIN,
    )
    assert got.kind == "withdrawal"
    row = current_decision(conn, "manual:veto-withdraw-writer")
    assert row["is_vetoed"] is False
    assert row["effective_decision"] == "deemed"
    assert row["veto_kind"] == "withdrawal"  # 履歴は残る
    conn.rollback()


def test_explicit_approval_can_be_vetoed(conn):
    """明示承認も否認できる(定款は明示承認の撤回を禁じていない)。"""
    got = record_decision(
        conn, "manual:explicit-veto", "approve", OWNER, OWNERS, kind="strategy_promotion"
    )
    record_veto(
        conn, got.id, "前提データの誤りが判明したため",
        vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="manual:explicit-veto",
        origin=ORIGIN,
    )
    assert current_decision(conn, "manual:explicit-veto")["effective_decision"] == "vetoed"
    conn.rollback()


# ── 否認できない決定・非オーナー・対象取り違え(独立役員審査 C-2 / C-3)────────
@pytest.mark.parametrize("decision", ["reject", "question"])
def test_reject_and_question_are_not_vetoable(conn, decision):
    """却下・質問は否認できない — 否認できると阻止の根拠が fail-open で消える。"""
    got = record_decision(
        conn, f"manual:nonvetoable-{decision}", decision, OWNER, OWNERS, kind="pr"
    )
    with pytest.raises(NotVetoableError, match="否認できない"):
        record_veto(
            conn, got.id, "却下を覆す",
            vetoed_by=OWNER, owner_ids=OWNERS,
            expected_proposal_ref=f"manual:nonvetoable-{decision}", origin=ORIGIN,
        )
    # 事前検査で弾くためトランザクションは生きている。
    assert _deemed(conn, f"after-nonvetoable-{decision}").id > 0
    conn.rollback()


def test_reject_veto_blocked_by_schema_trigger(conn):
    """アプリ検証を迂回してもトリガが最後の防衛線(一次統制はスキーマ側)。"""
    got = record_decision(conn, "manual:nonvetoable-raw", "reject", OWNER, OWNERS, kind="pr")
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
    deemed = _deemed(conn, "manual:veto-by-non-owner")
    with pytest.raises(NotOwnerError):
        record_veto(
            conn, deemed.id, "越権否認",
            vetoed_by="999999", owner_ids=OWNERS,
            expected_proposal_ref=deemed.proposal_ref, origin=ORIGIN,
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
    a = _deemed(conn, "manual:veto-ref-a")
    _deemed(conn, "manual:veto-ref-b")
    with pytest.raises(ProposalRefMismatchError, match="manual:veto-ref-a"):
        record_veto(
            conn, a.id, "取り違え否認",
            vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="manual:veto-ref-b",
            origin=ORIGIN,
        )
    assert current_decision(conn, "manual:veto-ref-a")["is_vetoed"] is False
    conn.rollback()


def test_veto_of_unknown_decision_raises_clear_error(conn):
    """FK 違反を待たず明確なエラーにする(FK 違反はトランザクションを中断させる)。"""
    with pytest.raises(ValueError, match="存在しない"):
        record_veto(
            conn, -1, "対象なし否認",
            vetoed_by=OWNER, owner_ids=OWNERS, expected_proposal_ref="manual:whatever",
            origin=ORIGIN,
        )
    assert _deemed(conn, "manual:after-bad-veto").id > 0
    conn.rollback()


@pytest.mark.parametrize("field", ["reason", "vetoed_by", "expected_proposal_ref"])
def test_veto_requires_non_blank_fields(conn, field):
    deemed = _deemed(conn, f"manual:veto-blank-{field}")
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
            expected_proposal_ref=kwargs["expected_proposal_ref"], origin=ORIGIN,
        )
    conn.rollback()


# ── 否認の出所 origin(0030 / 独立役員審査 0021 C-8)──────────────────────────
@pytest.mark.parametrize("origin", ["discord_button", "discord_command", "cli", "job"])
def test_writer_records_origin(conn, origin):
    """writer が渡した出所がそのまま行と現決定 view に載る(語彙4値すべて)。"""
    deemed = _deemed(conn, f"manual:writer-origin-{origin}")
    veto = _veto(conn, deemed, origin=origin)
    assert veto.origin == origin
    assert current_decision(conn, f"manual:writer-origin-{origin}")["veto_origin"] == origin
    conn.rollback()


def test_unknown_origin_rejected_before_insert(conn):
    """語彙外の出所は INSERT 前に弾く。

    一次統制は 0030 の CHECK だが、CheckViolation はトランザクションを中断させ、
    同一トランザクションで書く通知まで巻き添えにする(kind・3専決の事前検査と同型)。
    """
    deemed = _deemed(conn, "manual:writer-origin-unknown")
    with pytest.raises(ValueError, match="未知の否認の出所"):
        _veto(conn, deemed, origin="webhook")
    # 事前検査で弾くためトランザクションは生きている。
    assert _deemed(conn, "manual:after-bad-origin").id > 0
    conn.rollback()


def test_withdrawal_records_origin(conn):
    """撤回にも出所が要る(身に覚えのない撤回は否認より危険 — 取消義務が消える)。"""
    deemed = _deemed(conn, "manual:writer-origin-withdraw")
    _veto(conn, deemed, "誤った否認", origin="discord_button")
    got = record_veto_withdrawal(
        conn, deemed.id, "撤回",
        vetoed_by=OWNER, owner_ids=OWNERS,
        expected_proposal_ref=deemed.proposal_ref, origin="cli",
    )
    assert got.origin == "cli"
    assert current_decision(conn, "manual:writer-origin-withdraw")["veto_origin"] == "cli"
    conn.rollback()


# ── 現決定の ID 引き(A-18-1 のトレーラ突合の読み口)────────────────────────
def test_current_decision_by_id_reflects_veto(conn):
    """ID 引きでも否認が反映される(decisions 直読なら承認に見えてしまう)。"""
    deemed = _deemed(conn, "manual:by-id-ref")
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
        "--kind", "pr", "--notice", "保護領域の変更",
        "--review", "docs/reviews/x-review.md", "--review-missing-ok", "--dry-run",
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
        "--deemed-for-pr", "99",
        "--review", "docs/reviews/gate-review.md", "--review-missing-ok", "--dry-run",
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


# ── --review の必須性(後続配線審査 後-1)────────────────────────────────────
# 旧実装の条件は `not (args.review or args.notice)` で、--notice を渡せば審査参照ゼロで
# rc=0 になった(審査が実証コマンドで再現)。--notice は --review の代替にならない。
def test_cli_deemed_for_pr_rejects_notice_without_review(fake_gh, capsys):
    """**審査の実証コマンド**: `--deemed-for-pr 99 --notice x` は拒否される。"""
    fake_gh(PR_JSON)
    rc = main(["--deemed-for-pr", "99", "--notice", "審査なしで発効", "--dry-run"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--review" in err and "--notice で文面を差し替えても免除されない" in err


def test_cli_deemed_pr_kind_requires_review(capsys):
    """**審査の実証コマンド**: 旧来形 `--deemed --kind pr --notice x` も拒否される。"""
    rc = main([
        "--deemed", "--proposal-ref", "https://github.com/klonyapin/ryza/pull/1",
        "--kind", "pr", "--notice", "審査なしで発効", "--dry-run",
    ])
    assert rc == 1
    assert "--review" in capsys.readouterr().err


def test_cli_deemed_non_pr_kind_does_not_require_review(capsys):
    """PR 以外の kind には課さない — 独立役員審査が前置されない手続を塞がないため。"""
    rc = main([
        "--deemed", "--proposal-ref", "ips-2026-09", "--kind", "other",
        "--notice", "IPS 月次改訂", "--dry-run",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["fields"]


def test_cli_review_reference_reaches_the_notice_body(capsys):
    """--review を渡した手書き文面にも審査参照の行が残る(引数を渡させるだけにしない)。"""
    rc = main([
        "--deemed", "--proposal-ref", "https://github.com/klonyapin/ryza/pull/2",
        "--kind", "pr", "--notice", "保護領域 X の変更",
        "--review", "docs/reviews/x-review.md", "--review-missing-ok", "--dry-run",
    ])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)["description"]
    assert "保護領域 X の変更" in body and "独立役員審査: docs/reviews/x-review.md" in body


def test_cli_review_line_is_not_duplicated(capsys):
    """文面に既に審査参照が書かれていれば二重に足さない。"""
    rc = main([
        "--deemed", "--proposal-ref", "https://github.com/klonyapin/ryza/pull/3",
        "--kind", "pr", "--notice", "独立役員審査: docs/reviews/x-review.md で完了",
        "--review", "docs/reviews/x-review.md", "--review-missing-ok", "--dry-run",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["description"].count("docs/reviews/x-review.md") == 1


def test_cli_deemed_for_pr_refuses_a_closed_pr(fake_gh, capsys):
    """取り下げられた PR を発効させない(通知だけ出て取消義務が残る)。"""
    fake_gh({**PR_JSON, "state": "closed", "merged": False})
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/x.md",
        "--review-missing-ok", "--dry-run",
    ])
    assert rc == 1
    assert "クローズ済み" in capsys.readouterr().err


def test_cli_deemed_for_pr_accepts_a_merged_pr(fake_gh, capsys):
    """マージ済み PR は対象になる(事後の記録漏れを CLI で埋められる — A-18-7 の是正手段)。"""
    fake_gh({**PR_JSON, "state": "closed", "merged": True})
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/x.md",
        "--review-missing-ok", "--dry-run",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["fields"]


def test_cli_explicit_arguments_win_over_the_generated_ones(fake_gh, capsys):
    """自動生成の文面が状況に合わないときは手で上書きできる。"""
    fake_gh(PR_JSON)
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/x.md", "--review-missing-ok",
        "--notice", "手書きの要旨", "--kind", "other", "--dry-run",
    ])
    assert rc == 0
    embed = json.loads(capsys.readouterr().out)
    assert "手書きの要旨" in embed["description"] and "PR #99" not in embed["description"]
    assert any(f["value"] == "other" for f in embed["fields"])


def test_cli_deemed_for_pr_survives_a_file_list_failure(fake_gh, capsys):
    """変更ファイル一覧は文面の補助でしかない — 取れなくても発効そのものは止めない。"""
    fake_gh(PR_JSON, files_fail=True)
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/x.md",
        "--review-missing-ok", "--dry-run",
    ])
    assert rc == 0
    assert "変更ファイル" not in json.loads(capsys.readouterr().out)["description"]


def test_cli_deemed_for_pr_reports_gh_failure(monkeypatch, capsys):
    """gh が失敗したら黙って別の参照で発効させず、失敗として返す。

    実在検査は gh 呼び出しの前に済ませる(存在しない審査参照でネットワークを使わない)ため、
    ここでは ``--review-missing-ok`` を明示して「gh 応答の失敗」だけをテストの対象にする。
    """

    def failing(path, *, paginate=False, jq=None):
        raise decisions_mod.PullRequestLookupError("gh api に失敗した(未認証)")

    monkeypatch.setattr(decisions_mod, "_gh_api", failing)
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/x.md",
        "--review-missing-ok", "--dry-run",
    ])
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
        rc = main([
            "--deemed", "--proposal-ref", ref, "--kind", "pr", "--notice", "保護領域の変更",
            "--review", "docs/reviews/deemed-wiring-independent-review.md",
        ])
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
    deemed = _deemed(conn, "manual:veto-immutable")
    veto = _veto(conn, deemed, "否認")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE governance.decision_vetoes SET reason = '改竄' WHERE veto_id = %s",
                (veto.veto_id,),
            )
    conn.rollback()


# ────────────────────────────────────────────────────────────────────────────
# 審査対象 SHA / 審査参照(0029 — reminders decision-reviewed-sha)
#
# トレーラの reviewed=<sha40> は書き手の申告でしかなく、A-18 に照合先が無かった。
# 承認記録側に同じ主張を**別経路で**書かせることで、片側だけの改変が突合で出るようにする。
# ────────────────────────────────────────────────────────────────────────────
SHA_A = "a" * 40
SHA_B = "b" * 40


def test_reviewed_sha_and_review_ref_are_recorded(conn):
    """審査対象 SHA と審査参照は構造化列に入り、現決定 view から読める。"""
    got = record_deemed_approval(
        conn, "manual:reviewed-1", "pr", NOTICE,
        reviewed_sha=SHA_A, review_ref="docs/reviews/x-review.md",
    )
    assert got.reviewed_sha == SHA_A and got.review_ref == "docs/reviews/x-review.md"
    row = current_decision(conn, "manual:reviewed-1")
    assert row["reviewed_sha"] == SHA_A
    assert row["review_ref"] == "docs/reviews/x-review.md"
    conn.rollback()


def test_reviewed_sha_is_normalized_to_lowercase(conn):
    """大文字表記は不一致の誤検出になるため writer が正規化する(A-18-8 の突合前提)。"""
    got = record_deemed_approval(
        conn, "manual:reviewed-upper", "pr", NOTICE, reviewed_sha=SHA_A.upper()
    )
    assert got.reviewed_sha == SHA_A
    assert current_decision(conn, "manual:reviewed-upper")["reviewed_sha"] == SHA_A
    conn.rollback()


@pytest.mark.parametrize("bad", ["abc123", "z" * 40, "a" * 39, "a" * 41])
def test_short_or_invalid_reviewed_sha_is_rejected(conn, bad):
    """短縮・非 hex の SHA は拒否する(曖昧な参照は突合を「判定不能」にする)。"""
    with pytest.raises(ValueError, match="40 桁 hex"):
        record_deemed_approval(conn, f"manual:reviewed-bad-{bad}", "pr", NOTICE, reviewed_sha=bad)
    conn.rollback()


def test_reviewed_sha_defaults_to_null(conn):
    """申告が無い決定は NULL(独立審査が前置されない発効経路を必須化で塞がない)。"""
    got = record_deemed_approval(conn, "manual:reviewed-none", "pr", NOTICE)
    assert got.reviewed_sha is None and got.review_ref is None
    row = current_decision(conn, "manual:reviewed-none")
    assert row["reviewed_sha"] is None and row["review_ref"] is None
    conn.rollback()


def test_blank_review_ref_is_rejected(conn):
    """空白だけの審査参照は「書いたが中身が無い」= 未記入と区別できないので弾く。"""
    with pytest.raises(ValueError, match="review_ref"):
        record_deemed_approval(conn, "manual:reviewed-blank", "pr", NOTICE, review_ref="   ")
    conn.rollback()


def test_reviewed_sha_check_is_enforced_by_the_schema(conn):
    """一次統制は DB 側(0029 の CHECK)— writer を迂回した INSERT も通らない。"""
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO governance.decisions
                (proposal_ref, kind, decision, decided_by, reviewed_sha)
            VALUES ('bypass-writer', 'pr', 'deemed', 'system:deemed', 'DEADBEEF')
            """
        )
    conn.rollback()


def test_reviewed_sha_cannot_be_backfilled(conn):
    """既存行に後から審査対象 SHA を埋められない(0021 の追記オンリー)。

    列の追加は DDL であって行の UPDATE ではないため追記オンリー原則に触れないが、
    「過去の決定を遡って審査済みに見せる」経路が開いていないことは確かめておく。
    """
    got = record_deemed_approval(conn, "manual:reviewed-backfill", "pr", NOTICE)
    with conn.cursor() as cur, pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE governance.decisions SET reviewed_sha = %s WHERE id = %s", (SHA_A, got.id)
        )
    conn.rollback()


# ── 審査参照の実在検査(拒否ではなく警告)──────────────────────────────────
def test_existing_review_ref_produces_no_warning():
    assert decisions_mod.missing_review_ref_warning("config/governance.yaml") is None


def test_missing_repository_path_review_ref_warns():
    warning = decisions_mod.missing_review_ref_warning("docs/reviews/does-not-exist.md")
    assert warning is not None and "does-not-exist" in warning


def test_url_review_ref_is_not_checked():
    """リポジトリ外(Issue・Discord スレッド)の参照は実在検査の対象にしない。"""
    assert decisions_mod.missing_review_ref_warning("https://github.com/x/y/issues/1") is None
    assert decisions_mod.missing_review_ref_warning("#承認/123") is None
    assert decisions_mod.missing_review_ref_warning(None) is None


def _build_args(argv: list[str]):
    """CLI 引数を解析して Namespace にする(_resolve_deemed_args の直接検証用)。"""
    return decisions_mod._build_parser().parse_args(argv)


def test_cli_kind_pr_aborts_on_missing_review_ref(fake_gh, capsys):
    """**C-2(c)**: --kind pr で審査参照が実在しなければ**中止**する。

    旧実装は警告のみで通していたが、``--review docs/reviews/存在しない.md`` が審査の代用に
    なるうえ、警告は stderr に流れて消えるので実質的に無検査だった。--kind pr は独立役員審査を
    前置する手続なので、遡及登録は ``--review-missing-ok`` の明示に一本化する。
    """
    fake_gh(PR_JSON)
    rc = main(["--deemed-for-pr", "99", "--review", "docs/reviews/nope.md", "--dry-run"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "実在しない" in err and "--review-missing-ok" in err


def test_cli_kind_pr_missing_ok_falls_back_to_warning(fake_gh, capsys):
    """--review-missing-ok を明示すれば遡及登録・リポジトリ外の審査の口は塞がない(警告のみ)。"""
    fake_gh(PR_JSON)
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/nope.md",
        "--review-missing-ok", "--dry-run",
    ])
    assert rc == 0
    assert "警告" in capsys.readouterr().err


def test_cli_kind_other_missing_review_still_only_warns(capsys):
    """--kind pr 以外(独立役員審査を前置しない手続)は従来どおり警告のみ。"""
    rc = main([
        "--deemed", "--proposal-ref", "ips-2026-09", "--kind", "other",
        "--notice", "IPS 月次改訂", "--review", "docs/reviews/nope.md", "--dry-run",
    ])
    assert rc == 0
    assert "警告" in capsys.readouterr().err


def test_cli_kind_pr_dot_dot_escape_reference_aborts(capsys):
    """**C-2(a)**: 正規化してリポジトリ外へ出る参照は中止(``..`` 経由の迂回を塞ぐ)。"""
    rc = main([
        "--deemed", "--proposal-ref", "https://x/pull/9", "--kind", "pr",
        "--notice", "保護領域の変更", "--review", "../outside.md", "--dry-run",
    ])
    assert rc == 1
    assert "外" in capsys.readouterr().err


# ── CLI: 審査対象 SHA の自動取得・手動指定 ────────────────────────────────────
PR_JSON_WITH_HEAD = {**PR_JSON, "head": {"sha": SHA_B}}


def test_cli_deemed_for_pr_captures_the_head_sha(fake_gh, capsys):
    """PR の head SHA が審査対象として自動で入る(手入力させない = 発効時刻に固定する)。"""
    fake_gh(PR_JSON_WITH_HEAD)
    target = decisions_mod._resolve_deemed_args(
        _build_args([
            "--deemed-for-pr", "99", "--review", "docs/reviews/x.md", "--review-missing-ok",
        ])
    )
    assert target.reviewed_sha == SHA_B
    assert target.review_ref == "docs/reviews/x.md"
    rc = main([
        "--deemed-for-pr", "99", "--review", "docs/reviews/x.md",
        "--review-missing-ok", "--dry-run",
    ])
    assert rc == 0
    assert SHA_B in json.loads(capsys.readouterr().out)["description"]


def test_cli_explicit_reviewed_sha_wins_over_the_head_sha(fake_gh):
    """審査が head より前のコミットを対象としたときは手で書ける(実際に見た SHA を残す)。"""
    fake_gh(PR_JSON_WITH_HEAD)
    target = decisions_mod._resolve_deemed_args(
        _build_args([
            "--deemed-for-pr", "99", "--review", "docs/reviews/x.md", "--review-missing-ok",
            "--reviewed-sha", SHA_A,
        ])
    )
    assert target.reviewed_sha == SHA_A


def test_cli_head_sha_is_optional(fake_gh):
    """head を返さない応答でも発効そのものは止めない(reviewed_sha は任意)。"""
    fake_gh(PR_JSON)
    target = decisions_mod._resolve_deemed_args(
        _build_args([
            "--deemed-for-pr", "99", "--review", "docs/reviews/x.md", "--review-missing-ok",
        ])
    )
    assert target.reviewed_sha is None


def test_cli_reviewed_sha_reaches_the_notice_body_in_the_manual_form(capsys):
    """旧来形でも --reviewed-sha は通知本文に出る(代表が通知だけで対象を確認できる)。"""
    rc = main([
        "--deemed", "--proposal-ref", "https://github.com/klonyapin/ryza/pull/7",
        "--kind", "pr", "--notice", "保護領域 X の変更", "--reviewed-sha", SHA_A,
        "--review", "config/governance.yaml", "--dry-run",
    ])
    assert rc == 0
    assert SHA_A in json.loads(capsys.readouterr().out)["description"]


def test_cli_rejects_an_invalid_reviewed_sha(capsys):
    rc = main([
        "--deemed", "--proposal-ref", "x", "--kind", "other", "--notice", "y",
        "--reviewed-sha", "abc123", "--dry-run",
    ])
    assert rc == 1
    assert "40 桁 hex" in capsys.readouterr().err


# ── 明示承認にも審査対象 SHA を書ける(独立役員審査 SHA-4)────────────────────
# 3専決事項(定款・実弾・KS 復帰)は必ず押下による明示承認を通るため、この経路に列が
# 無いと**最重要の決定種別が構造的に A-18-8 の射程外**になる。
def test_explicit_approval_can_record_reviewed_sha(conn):
    got = record_decision(
        conn, "manual:explicit-reviewed", "approve", OWNER, OWNERS, kind="budget",
        reviewed_sha=SHA_A.upper(), review_ref="docs/reviews/x-review.md",
    )
    row = current_decision_by_id(conn, got.id)
    assert row["reviewed_sha"] == SHA_A  # writer と同じ正規化規則
    assert row["review_ref"] == "docs/reviews/x-review.md"
    conn.rollback()


def test_explicit_approval_defaults_to_null(conn):
    """押下による承認は必ずしもコミットを対象としない — 既定は従来どおり NULL。"""
    got = record_decision(conn, "manual:explicit-plain", "approve", OWNER, OWNERS, kind="budget")
    assert current_decision_by_id(conn, got.id)["reviewed_sha"] is None
    conn.rollback()


def test_explicit_approval_rejects_an_invalid_reviewed_sha(conn):
    """様式検証は経路で変わらない(deemed と同じ規則 = 突合の前提が揃う)。"""
    with pytest.raises(ValueError, match="40 桁 hex"):
        record_decision(
            conn, "manual:explicit-bad", "approve", OWNER, OWNERS,
            kind="budget", reviewed_sha="abc123",
        )
    conn.rollback()


# ── 審査参照の警告を痕跡として残す・ルート解決の堅牢化(独立役員審査 SHA-6)──────
def test_review_warning_is_appended_to_the_note():
    """stderr だけでは事後監査から「警告が出たか」を判別できないので note に残す。"""
    warning = "参照が見つからない"
    assert decisions_mod._note_with_warning(None, warning).startswith(
        decisions_mod.REVIEW_WARNING_NOTE_PREFIX
    )
    combined = decisions_mod._note_with_warning("元の補足", warning)
    assert combined.startswith("元の補足") and warning in combined
    assert decisions_mod._note_with_warning("元の補足", None) == "元の補足"


def test_repo_root_is_resolved_via_git():
    """ソースチェックアウトでは git rev-parse でルートが解ける(パス相対の当て推量に頼らない)。"""
    root = decisions_mod._repo_root()
    assert root is not None and (root / "config" / "governance.yaml").exists()


def test_review_ref_check_is_disabled_when_the_root_is_unknown(monkeypatch):
    """ルートを決められない実行(パッケージ設置)では検査せず**誤警告を出さない**。

    誤警告は「警告が出ていても実在する」学習を生み、警告そのものを無意味にする。
    """
    monkeypatch.setattr(decisions_mod, "_repo_root", lambda: None)
    assert decisions_mod.missing_review_ref_warning("docs/reviews/does-not-exist.md") is None


# ── 審査記録(意見書 front matter)からの reviewed_sha 採用 ────────────────────
# reminder ``reviewed-sha-from-review-agent``: 0029 + A-18-8 の突合は「起票者が書いた2つの
# 値」の比較でしかなく、同じ嘘を両方に書けば通る。審査側が独立に書いた reviewed_sha が
# 手元にある場面だけが、その申告性を外から検証できる地点である。
REVIEW_PATH = "docs/reviews/sample-review.md"


@pytest.fixture
def review_artifact(monkeypatch, tmp_path):
    """一時リポジトリに意見書を置き、``_repo_root`` をそこへ向ける。

    実リポジトリの docs/reviews を書き換えずに front matter の有無を切り替えるため。
    """

    def _install(text: str, path: str = REVIEW_PATH):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        monkeypatch.setattr(decisions_mod, "_repo_root", lambda: tmp_path)
        return path

    return _install


def _front_matter(sha: str, *, verdict: str = "conditional_approve") -> str:
    return f"---\nreviewed_sha: {sha}\nreview_date: 2026-08-04\nverdict: {verdict}\n---\n\n本文\n"


def test_cli_adopts_the_reviewed_sha_recorded_by_the_review(review_artifact):
    """**突合の成立**: 起票者が SHA を渡さなくても、審査記録の値が reviewed_sha になる。"""
    ref = review_artifact(_front_matter(SHA_A))
    target = decisions_mod._resolve_deemed_args(
        _build_args([
            "--deemed", "--proposal-ref", "https://x/pull/1", "--kind", "pr",
            "--notice", "保護領域の変更", "--review", ref,
        ])
    )
    assert target.reviewed_sha == SHA_A
    assert target.reviewed_sha_source == decisions_mod.SHA_SOURCE_ARTIFACT
    assert SHA_A in target.notice  # 通知本文にも審査対象として出る


def test_cli_stops_the_effectuation_when_the_declared_sha_contradicts_the_review(review_artifact):
    """**fail-safe**: 起票者の申告と審査側の記録が食い違えば発効しない(例外で止める)。"""
    ref = review_artifact(_front_matter(SHA_A))
    with pytest.raises(decisions_mod.ReviewedShaConflictError, match="発効を中止"):
        decisions_mod._resolve_deemed_args(
            _build_args([
                "--deemed", "--proposal-ref", "https://x/pull/1", "--kind", "pr",
                "--notice", "保護領域の変更", "--review", ref, "--reviewed-sha", SHA_B,
            ])
        )


def test_cli_returns_a_failure_code_on_the_conflict(review_artifact, capsys):
    """CLI としても異常終了する(黙って片方を採らない — 何が起きたかを stderr に出す)。"""
    ref = review_artifact(_front_matter(SHA_A))
    rc = main([
        "--deemed", "--proposal-ref", "https://x/pull/1", "--kind", "pr",
        "--notice", "保護領域の変更", "--review", ref, "--reviewed-sha", SHA_B, "--dry-run",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "発効を中止" in err and SHA_A[:12] in err


def test_cli_accepts_a_declared_sha_that_agrees_with_the_review(review_artifact):
    """一致する明示指定は通る(検査対象は食い違いであって、指定そのものではない)。"""
    ref = review_artifact(_front_matter(SHA_A))
    target = decisions_mod._resolve_deemed_args(
        _build_args([
            "--deemed", "--proposal-ref", "https://x/pull/1", "--kind", "pr",
            "--notice", "保護領域の変更", "--review", ref, "--reviewed-sha", SHA_A.upper(),
        ])
    )
    assert target.reviewed_sha == SHA_A
    assert target.reviewed_sha_source == decisions_mod.SHA_SOURCE_ARTIFACT


def test_cli_review_record_wins_over_the_pr_head_sha(review_artifact, fake_gh):
    """審査記録 > PR の head。head との相違は**止めず**注記で開示する。

    意見書のコミット自身が head を進めるため、審査対象 SHA と head は通常一致しない。
    ここを致命にすると front matter を書いた PR が軒並み発効できなくなる。
    """
    fake_gh(PR_JSON_WITH_HEAD)  # head = SHA_B
    ref = review_artifact(_front_matter(SHA_A))
    target = decisions_mod._resolve_deemed_args(
        _build_args(["--deemed-for-pr", "99", "--review", ref])
    )
    assert target.reviewed_sha == SHA_A
    assert target.reviewed_sha_source == decisions_mod.SHA_SOURCE_ARTIFACT
    assert any(SHA_B[:12] in n and "head" in n for n in target.reviewed_notes)


def test_cli_old_style_review_keeps_the_previous_behaviour(review_artifact, fake_gh):
    """**後方互換**: front matter の無い旧様式では従来どおり head SHA が入る(遡及改変しない)。"""
    fake_gh(PR_JSON_WITH_HEAD)
    ref = review_artifact("# 独立役員意見書(旧様式)\n\n- 審査日: 2026-08-03\n")
    target = decisions_mod._resolve_deemed_args(
        _build_args(["--deemed-for-pr", "99", "--review", ref])
    )
    assert target.reviewed_sha == SHA_B
    assert target.reviewed_sha_source == decisions_mod.SHA_SOURCE_PR_HEAD
    assert target.reviewed_notes == ()


def test_cli_old_style_review_keeps_the_declared_sha(review_artifact):
    """旧様式 + 明示指定は従来どおり起票者の申告(由来ラベルがそれを開示する)。"""
    ref = review_artifact("# 旧様式\n")
    target = decisions_mod._resolve_deemed_args(
        _build_args([
            "--deemed", "--proposal-ref", "https://x/pull/2", "--kind", "pr",
            "--notice", "保護領域の変更", "--review", ref, "--reviewed-sha", SHA_B,
        ])
    )
    assert target.reviewed_sha == SHA_B
    assert target.reviewed_sha_source == decisions_mod.SHA_SOURCE_ARGUMENT


def test_cli_broken_front_matter_stops_the_effectuation(review_artifact, capsys):
    """様式不備を「旧様式」に読み替えない — YAML を壊すことが回避策にならないようにする。"""
    ref = review_artifact(f"---\nreviewed_sha: {SHA_A}\n\n本文(閉じフェンス無し)\n")
    rc = main([
        "--deemed", "--proposal-ref", "https://x/pull/3", "--kind", "pr",
        "--notice", "保護領域の変更", "--review", ref, "--dry-run",
    ])
    assert rc == 1
    assert "閉じフェンス" in capsys.readouterr().err


def test_cli_reports_front_matter_warnings_without_stopping(review_artifact, capsys):
    """語彙外の verdict 等は発効を止めない(判定名の揺れで様式が忌避されないように)。"""
    ref = review_artifact(_front_matter(SHA_A, verdict="とても良い"))
    rc = main([
        "--deemed", "--proposal-ref", "https://x/pull/4", "--kind", "pr",
        "--notice", "保護領域の変更", "--review", ref, "--dry-run",
    ])
    assert rc == 0
    assert "語彙外" in capsys.readouterr().err


def test_reviewed_sha_source_is_recorded_in_the_run_parameters(review_artifact, monkeypatch):
    """由来は meta.runs に残す — A-18-8 の一致が審査記録の裏付けを持つかを事後に判別する。"""
    ref = review_artifact(_front_matter(SHA_A))
    captured: dict = {}

    def _fake_start_run(job_name, params=None, **kw):
        captured.update(params or {})
        raise RuntimeError("記録経路は本テストの対象外")

    monkeypatch.setattr("ryza.provenance.start_run", _fake_start_run)
    with pytest.raises(RuntimeError):
        main([
            "--deemed", "--proposal-ref", "https://x/pull/5", "--kind", "pr",
            "--notice", "保護領域の変更", "--review", ref,
        ])
    assert captured["reviewed_sha"] == SHA_A
    assert captured["reviewed_sha_source"] == decisions_mod.SHA_SOURCE_ARTIFACT


# ── --repo-root(Issue #132)───────────────────────────────────────────────────
# メイン checkout ではなく PR ブランチの worktree にある意見書を CLI に読ませるための明示
# オプション。省略時は従来経路(``_repo_root()`` 委譲)であり、指定時は fail-closed で検証する
# (存在しないパス・目印なきパスは発効を止める — 沈黙して「リポジトリ外参照」に落とすと
# reviewed_sha が PR head へフォールバックし、A-18-8 の sha_conflict 恒久ノイズを生む)。
def _write_worktree_review(root, path: str, text: str) -> str:
    """``root`` を Ryza の checkout に見立てて意見書を1本置く(config も一緒に作る)。"""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "governance.yaml").write_text("# stub\n", encoding="utf-8")
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return path


def test_cli_repo_root_resolves_review_from_a_worktree(tmp_path):
    """**Issue #132 是正の主目的**: worktree を ``--repo-root`` に渡すと、そこにある意見書を
    読んで reviewed_sha が審査記録から採用される(メイン checkout を見に行かない)。

    ``_repo_root`` を monkeypatch せずに ``--repo-root`` の経路そのものを検証する
    (fixture review_artifact との違い: 本テストは CLI オプションの受け渡しを見る)。
    """
    ref = _write_worktree_review(tmp_path, "docs/reviews/pr132-review.md", _front_matter(SHA_A))
    target = decisions_mod._resolve_deemed_args(
        _build_args([
            "--deemed", "--proposal-ref", "https://x/pull/132", "--kind", "pr",
            "--notice", "保護領域の変更", "--review", ref,
            "--repo-root", str(tmp_path),
        ]),
        repo_root=decisions_mod._validated_repo_root(str(tmp_path)),
    )
    assert target.reviewed_sha == SHA_A
    assert target.reviewed_sha_source == decisions_mod.SHA_SOURCE_ARTIFACT


def test_cli_repo_root_nonexistent_path_aborts_before_db_write(migrated_db, capsys):
    """**fail-closed**: 存在しないパスを ``--repo-root`` に渡すと発効せずエラーで返す。

    DB に何も書かれないことを、記録用の proposal_ref を後から SELECT して確認する。
    ``--dry-run`` は付けない(DB 書き込みの直前で止まったことを実体で見るため)。
    """
    ref = "https://github.com/klonyapin/ryza/pull/9032"
    rc = main([
        "--deemed", "--proposal-ref", ref, "--kind", "pr",
        "--notice", "保護領域の変更",
        "--review", "docs/reviews/anywhere.md",
        "--repo-root", "/no/such/path/for/t027",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--repo-root" in err and "存在しない" in err
    conn = connect()
    try:
        row = current_decision(conn, ref)
        assert row is None  # 発効前に止まった = 記録は無い
    finally:
        conn.rollback()
        conn.close()


def test_cli_repo_root_missing_governance_yaml_aborts(tmp_path, migrated_db, capsys):
    """**fail-closed**: ``config/governance.yaml`` が無い(= Ryza の checkout でない)パスは弾く。

    存在するディレクトリでも、目印が無ければ意見書を沈黙して見失う可能性がある —— 中身の
    確認を CLI で実体化する(``_repo_root()`` のフォールバックと同じ目印を使う)。
    """
    (tmp_path / "docs" / "reviews").mkdir(parents=True)
    (tmp_path / "docs" / "reviews" / "x.md").write_text(_front_matter(SHA_A), encoding="utf-8")
    # 意図的に config/governance.yaml は作らない
    ref = "https://github.com/klonyapin/ryza/pull/9033"
    rc = main([
        "--deemed", "--proposal-ref", ref, "--kind", "pr",
        "--notice", "保護領域の変更",
        "--review", "docs/reviews/x.md",
        "--repo-root", str(tmp_path),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Ryza リポジトリの checkout に見えない" in err
    conn = connect()
    try:
        assert current_decision(conn, ref) is None
    finally:
        conn.rollback()
        conn.close()


def test_cli_without_repo_root_keeps_the_default_behavior(review_artifact, monkeypatch):
    """**未指定時の挙動は不変**: ``--repo-root`` を渡さなければ従来経路(``_repo_root()``
    委譲)そのままで、Run の params にも ``repo_root=None`` が記録される
    (既存テスト全体が green のままであることで担保される項目の、明示的な最小検証)。
    """
    ref_path = review_artifact(_front_matter(SHA_A))
    captured: dict = {}

    def _fake_start_run(job_name, params=None, **kw):
        captured.update(params or {})
        raise RuntimeError("記録経路は本テストの対象外")

    monkeypatch.setattr("ryza.provenance.start_run", _fake_start_run)
    with pytest.raises(RuntimeError):
        main([
            "--deemed", "--proposal-ref", "https://x/pull/9034", "--kind", "pr",
            "--notice", "保護領域の変更", "--review", ref_path,
        ])
    # 未指定なら None が入る(既定経路の意味は _repo_root() 委譲であり、値としては空)。
    assert captured["repo_root"] is None
    # 既存経路の副産物は変わらない(reviewed_sha は審査記録から採る)。
    assert captured["reviewed_sha"] == SHA_A
    assert captured["reviewed_sha_source"] == decisions_mod.SHA_SOURCE_ARTIFACT


def test_cli_repo_root_is_recorded_in_decision_note(migrated_db, monkeypatch, tmp_path):
    """``--repo-root`` を使うと決定の ``note`` に絶対パスが残る(監査時に checkout を追える)。

    DB 検証の流儀は既存 test_cli_records_and_notifies_on_a_fresh_connection に倣い、
    ``commit`` を握り潰した薄い委譲で確定させず、SELECT で note を確かめてから rollback する。
    """
    import ryza.db.conn as db_conn

    _write_worktree_review(tmp_path, "docs/reviews/repo-root-note.md", _front_matter(SHA_A))
    inner = connect()

    class _NoCommitConn:
        def commit(self):
            pass

        def close(self):
            pass

        def __getattr__(self, name):
            return getattr(inner, name)

    monkeypatch.setattr(db_conn, "connect", lambda autocommit=False: _NoCommitConn())
    ref = "https://github.com/klonyapin/ryza/pull/9035"
    run_id = None
    try:
        rc = main([
            "--deemed", "--proposal-ref", ref, "--kind", "pr",
            "--notice", "保護領域の変更",
            "--review", "docs/reviews/repo-root-note.md",
            "--repo-root", str(tmp_path),
        ])
        assert rc == 0
        with inner.cursor() as cur:
            cur.execute(
                "SELECT note, channel_msg_id FROM governance.decisions "
                "WHERE proposal_ref = %s",
                (ref,),
            )
            note, notice_ref = cur.fetchone()
            cur.execute(
                "SELECT run_id FROM press.outbox WHERE id = %s",
                (int(notice_ref.removeprefix("outbox:")),),
            )
            run_id = cur.fetchone()[0]
        assert note is not None
        assert decisions_mod.REPO_ROOT_NOTE_PREFIX in note
        # 絶対パス(resolve 済み)が入っている。tmp_path は既に絶対だが realpath 経由での
        # 別名(macOS の /private/var/... 等)にも耐えるよう、``.resolve()`` の文字列で照合する。
        assert str(tmp_path.resolve()) in note
        inner.rollback()
    finally:
        if run_id is not None:
            inner.rollback()
            with inner.cursor() as cur:
                cur.execute("DELETE FROM meta.runs WHERE run_id = %s", (run_id,))
            inner.commit()
        inner.close()
