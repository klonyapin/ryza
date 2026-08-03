"""A-18 規則⇔実装トレーサビリティ監査のテスト。

一時 git リポジトリを作り、保護領域突合(A-18-1)を承認あり/なし/PR マージ/発効日前の
4 象限で検証する。バージョン整合(A-18-2)・宣言棚卸し(A-18-3)はフィクスチャファイルで、
outbox 投入はテスト専用 DB で検証する。全変更 PR 化(A-18-4)は PR マージのみ/直 push/
非 PR マージ/基準以前の4象限+例外なし(Approved トレーラ付き直 push も違反)を検証する。

一時リポジトリでは実リポジトリの基準コミット(``PR_RULE_BASELINE_COMMIT``)が存在しない
ため、``run_a18`` には ``pr_since_commit`` を明示的に渡す。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ryza.audit import a18

GOV_YAML = """\
version: "0.3"
approval_trailer: "Approved:"
protected_areas:
  - path: docs/protected.md
    area: constitution
  - path: src/prot/**
    area: kill_switch
  - path: migrations/*.sql
    area: schema
controls:
  - rule: 宣言その1(異議経路の保護)
    enforcement: declaration
    verification: 四半期棚卸し
  - rule: 宣言その2(反対意見書)
    enforcement: declaration
    verification: 四半期棚卸し
  - rule: ゲート条文
    enforcement: gate
    verification: validate_mandates
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """一時リポジトリと「批准コミット」sha を返す。

    履歴: 発効前の無承認変更 → 批准コミット(governance.yaml 追加)。以後の履歴は各テストが作る。
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "test")
    _git(r, "config", "user.email", "test@example.com")
    # 発効前: 保護ファイルへの無承認変更(対象外になるべき)。
    _commit(r, "docs/protected.md", "before\n", "pre: 発効前の無承認変更")
    ratification = _commit(r, "config/governance.yaml", GOV_YAML, "governance: 批准コミット")
    return r, ratification


def _run_a181(repo_path: Path, since: str | None):
    gov = a18.load_governance(repo_path)
    return a18.check_protected_commits(repo_path, gov, since_commit=since)


# ────────────────────────────────────────────────────────────────────────────
# A-18-1 保護領域突合
# ────────────────────────────────────────────────────────────────────────────
def test_unapproved_direct_commit_is_violation(repo):
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: 無承認の保護領域変更")
    violations, checked = _run_a181(r, since)
    assert checked == 1
    assert [v["commit"] for v in violations] == [sha[:12]]
    assert violations[0]["files"] == ["docs/protected.md"]
    assert "Approved" in violations[0]["reason"]


def test_approved_trailer_commit_is_ok(repo):
    r, since = repo
    _commit(
        r, "docs/protected.md", "v2\n",
        "docs: 承認済み変更\n\nApproved: https://github.com/x/y/issues/1",
    )
    violations, _ = _run_a181(r, since)
    assert violations == []


def test_trailer_without_reference_is_violation(repo):
    r, since = repo
    _commit(r, "docs/protected.md", "v2\n", "docs: 空トレーラ\n\nApproved:")
    violations, _ = _run_a181(r, since)
    assert len(violations) == 1


def test_pr_merge_commit_path_is_ok(repo):
    r, since = repo
    _git(r, "checkout", "-q", "-b", "feature")
    _commit(r, "src/prot/ks.py", "x = 1\n", "feat: 保護コード変更(ブランチ)")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "feature", "-m", "Merge pull request #5 from k/feature")
    violations, _ = _run_a181(r, since)
    assert violations == []


def test_non_pr_merge_path_is_violation(repo):
    """PR でないただの --no-ff マージ経由は承認と見なさない。"""
    r, since = repo
    _git(r, "checkout", "-q", "-b", "feature")
    sha = _commit(r, "src/prot/ks.py", "x = 1\n", "feat: 保護コード変更(ブランチ)")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "feature", "-m", "ローカルマージ(PR でない)")
    violations, _ = _run_a181(r, since)
    assert [v["commit"] for v in violations] == [sha[:12]]


def test_pre_ratification_commits_are_excluded(repo):
    """発効日前(批准コミットの祖先)の無承認変更は対象外。since=None だと対象になる。"""
    r, since = repo
    violations, checked = _run_a181(r, since)
    assert violations == [] and checked == 0
    # since=None なら全履歴が対象になり、発効前の無承認変更が検出される。
    violations_all, _ = _run_a181(r, None)
    assert len(violations_all) == 1
    assert violations_all[0]["subject"].startswith("pre:")


def test_unprotected_change_is_ignored(repo):
    r, since = repo
    _commit(r, "README.md", "hello\n", "docs: 保護外の変更")
    _commit(
        r, "migrations/sub/0001_x.sql", "-- sub\n",
        "sql: サブディレクトリは migrations/*.sql の対象外",
    )
    violations, checked = _run_a181(r, since)
    assert violations == [] and checked == 2


def test_unknown_since_commit_raises(repo):
    r, _ = repo
    with pytest.raises(ValueError):
        _run_a181(r, "deadbeef" * 5)


def _make_evil_merge(r: Path, merge_message: str) -> None:
    """main とブランチで同一保護ファイルを競合させ、独自内容で解消したマージを作る。

    セットアップコミット自体は Approved トレーラ付き(それら自身が違反にならないように)。
    """
    _git(r, "checkout", "-q", "-b", "feature")
    _commit(r, "docs/protected.md", "branch side\n", "docs: branch\n\nApproved: https://x/1")
    _git(r, "checkout", "-q", "main")
    _commit(r, "docs/protected.md", "main side\n", "docs: main\n\nApproved: https://x/2")
    merge = subprocess.run(
        ["git", "-C", str(r), "merge", "feature"], capture_output=True, text=True, check=False
    )
    assert merge.returncode != 0  # コンフリクトが起きていること
    # どちらの親とも異なる内容で解消(= evil merge の持ち込み差分)。
    (r / "docs/protected.md").write_text("resolved: neither side\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", merge_message)


def test_evil_merge_without_trailer_is_violation(repo):
    """マージ自身のコンフリクト解消差分は PR 件名だけでは承認と見なさない(バイパス封鎖)。"""
    r, since = repo
    _make_evil_merge(r, "Merge pull request #9 from k/feature")
    violations, _ = _run_a181(r, since)
    assert len(violations) == 1
    assert "evil merge" in violations[0]["reason"]
    assert violations[0]["files"] == ["docs/protected.md"]


def test_evil_merge_with_trailer_is_ok(repo):
    r, since = repo
    _make_evil_merge(
        r, "Merge pull request #9 from k/feature\n\nApproved: https://x/3"
    )
    violations, _ = _run_a181(r, since)
    assert violations == []


# ────────────────────────────────────────────────────────────────────────────
# A-18-1 Approved トレーラの実在照合(governance.current_decisions との突合)
#
# 「トレーラがある」で受理すると、代表が否認した承認を A-18 が承認として受理し、
# 取消義務が生じている変更が無承認変更として検出されない(独立役員審査 0021 C-5)。
# 照合は必ず現決定 view 経由で行い、否認済みは受理しない。
# ────────────────────────────────────────────────────────────────────────────
def test_decision_ref_id_parsing():
    assert a18.decision_ref_id("123") == 123
    assert a18.decision_ref_id("decision:45") == 45
    assert a18.decision_ref_id("https://github.com/x/y/issues/1") is None
    assert a18.decision_ref_id("#12") is None


def test_approval_trailer_refs_collects_all():
    msg = "fix: x\n\nApproved: 12\nApproved: https://github.com/x/y/issues/3\n"
    assert a18.approval_trailer_refs(msg) == ["12", "https://github.com/x/y/issues/3"]


def _deemed(conn, run_id, proposal_ref: str) -> int:
    """みなし承認を1件記録し decision id を返す(通知と同一トランザクション)。"""
    from ryza.governance import notices

    return notices.announce_deemed_approval(
        conn, proposal_ref, "pr", "保護領域の変更", run_id
    ).decision.id


def _commit_with_trailer(r: Path, ref: str) -> str:
    return _commit(
        r, "docs/protected.md", f"v-{ref}\n", f"docs: 保護領域変更\n\nApproved: {ref}"
    )


def test_deemed_decision_trailer_is_accepted(repo, conn, run_id):
    """みなし承認(deemed)を指すトレーラは承認記録として受理される(0019 C-3 の⑤)。"""
    r, since = repo
    decision_id = _deemed(conn, run_id, "https://github.com/x/y/pull/101")
    _commit_with_trailer(r, str(decision_id))
    gov = a18.load_governance(r)
    violations, _ = a18.check_protected_commits(r, gov, since_commit=since, conn=conn)
    assert violations == []
    conn.rollback()


def test_vetoed_decision_trailer_is_violation(repo, conn, run_id):
    """否認された承認を指すトレーラは受理しない(取消されるまで無承認変更)。"""
    from ryza.governance import notices

    r, since = repo
    decision_id = _deemed(conn, run_id, "https://github.com/x/y/pull/102")
    notices.apply_veto(
        conn, "https://github.com/x/y/pull/102", "リスク上限を緩めるため",
        vetoed_by="424242", owner_ids=("424242",), run_id=run_id,
    )
    _commit_with_trailer(r, str(decision_id))
    gov = a18.load_governance(r)
    violations, _ = a18.check_protected_commits(r, gov, since_commit=since, conn=conn)
    assert len(violations) == 1
    assert "否認済み" in violations[0]["reason"]
    conn.rollback()


def test_pr_merge_does_not_rescue_a_vetoed_trailer(repo, conn, run_id):
    """PR マージ経由でも、トレーラが否認済みの承認を指すなら違反のまま。"""
    from ryza.governance import notices

    r, since = repo
    decision_id = _deemed(conn, run_id, "https://github.com/x/y/pull/103")
    notices.apply_veto(
        conn, "https://github.com/x/y/pull/103", "否認",
        vetoed_by="424242", owner_ids=("424242",), run_id=run_id,
    )
    _git(r, "checkout", "-q", "-b", "feature-vetoed")
    sha = _commit(
        r, "src/prot/ks.py", "x = 1\n", f"feat: 保護コード\n\nApproved: {decision_id}"
    )
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "feature-vetoed", "-m", "Merge pull request #7 from k/f")
    gov = a18.load_governance(r)
    violations, _ = a18.check_protected_commits(r, gov, since_commit=since, conn=conn)
    assert [v["commit"] for v in violations] == [sha[:12]]
    assert "否認済み" in violations[0]["reason"]
    conn.rollback()


def test_missing_decision_record_is_violation(repo, conn):
    """存在しない決定 ID を指すトレーラは承認と見なさない(自己申告の空手形)。"""
    r, since = repo
    _commit_with_trailer(r, "999999999")
    gov = a18.load_governance(r)
    violations, _ = a18.check_protected_commits(r, gov, since_commit=since, conn=conn)
    assert len(violations) == 1
    assert "存在しない" in violations[0]["reason"]
    conn.rollback()


def test_rejected_decision_trailer_is_violation(repo, conn):
    """却下された決定を指すトレーラも承認ではない。"""
    from ryza.bot.approvals import record_decision

    r, since = repo
    got = record_decision(conn, "rejected-proposal", "reject", "424242", ("424242",), kind="pr")
    _commit_with_trailer(r, str(got.id))
    gov = a18.load_governance(r)
    violations, _ = a18.check_protected_commits(r, gov, since_commit=since, conn=conn)
    assert len(violations) == 1
    assert "承認ではない" in violations[0]["reason"]
    conn.rollback()


def test_issue_url_trailer_is_accepted_without_lookup(repo, conn):
    """Issue URL 形式は照合対象外(GitHub API 未実装 — 従来どおり存在検査まで)。"""
    r, since = repo
    _commit_with_trailer(r, "https://github.com/x/y/issues/1")
    gov = a18.load_governance(r)
    violations, _ = a18.check_protected_commits(r, gov, since_commit=since, conn=conn)
    assert violations == []
    conn.rollback()


def test_without_conn_vetoed_trailer_is_not_detected(repo, conn, run_id):
    """conn 無しでは照合できない。従来動作を保ちつつ、その限界を notes で開示する。"""
    from ryza.governance import notices

    r, since = repo
    decision_id = _deemed(conn, run_id, "https://github.com/x/y/pull/104")
    notices.apply_veto(
        conn, "https://github.com/x/y/pull/104", "否認",
        vetoed_by="424242", owner_ids=("424242",), run_id=run_id,
    )
    _commit_with_trailer(r, str(decision_id))
    gov = a18.load_governance(r)
    violations, _ = a18.check_protected_commits(r, gov, since_commit=since, conn=None)
    assert violations == []  # 検出できない(= conn を渡さない実行の限界)
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert result["decision_refs_verified"] is False
    assert any("未照合" in n for n in result["notes"])
    conn.rollback()


def test_run_a18_with_conn_marks_refs_verified(repo, conn):
    r, since = repo
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since, conn=conn)
    assert result["decision_refs_verified"] is True
    conn.rollback()


# ────────────────────────────────────────────────────────────────────────────
# glob 変換
# ────────────────────────────────────────────────────────────────────────────
def test_glob_to_regex_semantics():
    assert a18.glob_to_regex("migrations/*.sql").match("migrations/0001_a.sql")
    assert not a18.glob_to_regex("migrations/*.sql").match("migrations/sub/a.sql")
    assert a18.glob_to_regex("src/ryza/ledger/**").match("src/ryza/ledger/a/b.py")
    assert a18.glob_to_regex("CLAUDE.md").match("CLAUDE.md")
    assert not a18.glob_to_regex("CLAUDE.md").match("docs/CLAUDE.md")


# ────────────────────────────────────────────────────────────────────────────
# A-18-4 全変更 PR 化(直 push 検査)
# ────────────────────────────────────────────────────────────────────────────
def _merge_pr(r: Path, branch: str, path: str, content: str, pr_no: int) -> None:
    """ブランチにコミットを作り PR マージ(Merge pull request マージコミット)で main へ取り込む。"""
    _git(r, "checkout", "-q", "-b", branch)
    _commit(r, path, content, f"feat: {branch} の作業")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", branch, "-m", f"Merge pull request #{pr_no} from k/{branch}")


def test_a18_4_pr_merges_only_is_clean(repo):
    """PR マージのみの履歴は違反 0。検査数はマージコミット(first-parent)のみ数える。"""
    r, since = repo
    _merge_pr(r, "f1", "README.md", "a\n", 1)
    _merge_pr(r, "f2", "src/app.py", "x = 1\n", 2)
    violations, checked = a18.check_direct_pushes(r, since_commit=since)
    assert violations == []
    assert checked == 2  # マージコミット2つ(ブランチ内コミットは first-parent でない)


def test_a18_4_direct_push_is_violation(repo):
    """保護領域外のファイルでも main への直 push は違反(全変更が対象)。"""
    r, since = repo
    sha = _commit(r, "README.md", "x\n", "docs: 直 push(保護領域外)")
    violations, checked = a18.check_direct_pushes(r, since_commit=since)
    assert checked == 1
    assert [v["commit"] for v in violations] == [sha[:12]]
    assert violations[0]["files"] == ["README.md"]
    assert "直 push" in violations[0]["reason"]


def test_a18_4_approved_trailer_is_still_violation(repo):
    """例外なし: Approved トレーラ付きの直 push も違反(全 PR 化ルールに例外を設けない)。"""
    r, since = repo
    _commit(
        r, "README.md", "x\n",
        "docs: 承認トレーラ付き直 push\n\nApproved: https://github.com/x/y/issues/1",
    )
    violations, _ = a18.check_direct_pushes(r, since_commit=since)
    assert len(violations) == 1


def test_a18_4_non_pr_merge_is_violation(repo):
    """親数>1 でも件名が PR マージ形式でないマージは違反(A-18-1 と同じ件名検査を併用)。"""
    r, since = repo
    _git(r, "checkout", "-q", "-b", "f1")
    _commit(r, "README.md", "a\n", "feat: ブランチ作業")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "f1", "-m", "ローカルマージ(PR でない)")
    violations, checked = a18.check_direct_pushes(r, since_commit=since)
    assert checked == 1
    assert len(violations) == 1
    assert "非 PR マージ" in violations[0]["reason"]
    assert violations[0]["files"] == ["README.md"]  # マージが main に持ち込んだ first-parent 差分


def test_a18_4_pre_baseline_commits_are_excluded(repo):
    """基準コミット以前の直 push は対象外(遡及しない)。since=None だと対象になる。"""
    r, since = repo
    violations, checked = a18.check_direct_pushes(r, since_commit=since)
    assert violations == [] and checked == 0
    # since=None なら全履歴が対象になり、基準以前の直 push(発効前コミット等)が検出される。
    violations_all, _ = a18.check_direct_pushes(r, since_commit=None)
    assert len(violations_all) == 2  # フィクスチャの pre コミット+批准コミット


def test_a18_4_mixed_history_detects_only_direct_pushes(repo):
    """PR マージと直 push の混在履歴で直 push だけを検出する。"""
    r, since = repo
    _merge_pr(r, "f1", "README.md", "a\n", 1)
    direct = _commit(r, "docs/note.md", "n\n", "docs: 直 push")
    _merge_pr(r, "f2", "src/app.py", "x = 1\n", 2)
    violations, checked = a18.check_direct_pushes(r, since_commit=since)
    assert checked == 3
    assert [v["commit"] for v in violations] == [direct[:12]]


def test_a18_4_unknown_since_commit_raises(repo):
    r, _ = repo
    with pytest.raises(ValueError):
        a18.check_direct_pushes(r, since_commit="deadbeef" * 5)


# ────────────────────────────────────────────────────────────────────────────
# A-18-2 文書⇔config 整合
# ────────────────────────────────────────────────────────────────────────────
def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_version_match_ok(tmp_path: Path):
    _write(tmp_path, "docs/design/80-ips.md", "# Ryza 投資方針書(IPS)v1.3\n本文\n")
    _write(tmp_path, "config/ips.yaml", 'version: "1.3"\n')
    pairs = (("docs/design/80-ips.md", "config/ips.yaml"),)
    assert a18.check_versions(tmp_path, pairs) == []


def test_version_mismatch_detected(tmp_path: Path):
    _write(tmp_path, "docs/design/06-constitution.md", "# Ryza 定款(Constitution)v0.3\n")
    _write(tmp_path, "config/governance.yaml", 'version: "0.2"\n')
    pairs = (("docs/design/06-constitution.md", "config/governance.yaml"),)
    out = a18.check_versions(tmp_path, pairs)
    assert len(out) == 1
    assert out[0]["doc_version"] == "0.3" and out[0]["config_version"] == "0.2"


def test_version_missing_file_is_mismatch(tmp_path: Path):
    pairs = (("docs/design/80-ips.md", "config/ips.yaml"),)
    out = a18.check_versions(tmp_path, pairs)
    assert len(out) == 1 and out[0]["reason"] == "バージョン表記が取得できない"


def test_real_repo_versions_match():
    """実リポジトリの 80-ips.md⇔ips.yaml / 06-constitution.md⇔governance.yaml が一致している。"""
    root = Path(__file__).resolve().parents[2]
    assert a18.check_versions(root) == []


# ────────────────────────────────────────────────────────────────────────────
# A-18-3 宣言棚卸し・注記
# ────────────────────────────────────────────────────────────────────────────
def test_declarations_inventory(repo):
    r, _ = repo
    gov = a18.load_governance(r)
    decls = a18.list_declarations(gov)
    assert [d["rule"] for d in decls] == ["宣言その1(異議経路の保護)", "宣言その2(反対意見書)"]


def test_run_a18_end_to_end_on_tmp_repo(repo):
    r, since = repo
    _commit(r, "docs/protected.md", "v2\n", "docs: 無承認変更")
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert len(result["violations"]) == 1
    assert result["mismatches"] != []  # 80-ips.md 等が無い一時リポジトリでは不整合になる
    assert len(result["declarations"]) == 2
    # 上の無承認変更は直 push でもあるので A-18-4 でも検出される。
    assert len(result["direct_pushes"]) == 1
    assert result["checked_first_parent"] == 1
    # 監査部門コードのパス未登録の注記(governance.yaml のコメントで予告された検査)。
    assert any("src/ryza/audit" in n for n in result["notes"])
    assert a18.has_findings(result)


def test_standard_disclosures_always_in_notes(repo):
    """既知の限界(PR 件名未照合・トレーラ実在未照合・evil merge 検査方式)は毎回開示される。"""
    r, since = repo
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    for disclosure in a18.STANDARD_DISCLOSURES:
        assert disclosure in result["notes"]
    # 開示は embed の注記フィールドにも常に載る。
    embed = a18.build_alert_embed(result)
    assert any(f["name"] == "注記" for f in embed["fields"])


def test_stale_checkout_warning(repo):
    """HEAD が origin/main を含まない場合は stale checkout を警告する(fetch はしない)。"""
    r, since = repo
    # origin/main が HEAD に無いコミットを指す状況を作る。
    _git(r, "checkout", "-q", "-b", "newer")
    newer = _commit(r, "README.md", "newer\n", "docs: newer")
    _git(r, "checkout", "-q", "main")
    _git(r, "update-ref", "refs/remotes/origin/main", newer)
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert any("stale checkout" in n for n in result["notes"])
    # origin/main が HEAD の祖先なら警告しない。
    _git(r, "update-ref", "refs/remotes/origin/main", since)
    result2 = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert not any("stale checkout" in n for n in result2["notes"])


# ────────────────────────────────────────────────────────────────────────────
# embed・outbox 投入(テスト専用 DB)
# ────────────────────────────────────────────────────────────────────────────
def _result(violations: list, mismatches: list, direct_pushes: list | None = None) -> dict:
    return {
        "as_of": "2026-08-03T00:00:00+00:00",
        "since_commit": a18.RATIFICATION_COMMIT,
        "checked_commits": 3,
        "violations": violations,
        "mismatches": mismatches,
        "declarations": [{"rule": "宣言その1", "verification": "棚卸し"}],
        "pr_since_commit": a18.PR_RULE_BASELINE_COMMIT,
        "checked_first_parent": 3,
        "direct_pushes": direct_pushes or [],
        "notes": [],
    }


def test_embed_alert_on_violation():
    v = {"commit": "abc123def456", "subject": "s", "files": ["CLAUDE.md"], "reason": "r"}
    embed = a18.build_alert_embed(_result([v], []))
    assert "要対応" in embed["title"]
    assert any("abc123def456" in f["value"] for f in embed["fields"])


def test_embed_clean_when_no_findings():
    embed = a18.build_alert_embed(_result([], []))
    assert "所見なし" in embed["title"]
    assert any(f["name"] == "A-18-4 全変更 PR 化" for f in embed["fields"])


def test_embed_author_is_audit_character():
    """監査報告の発信者は監査部門のキャラクター(config/org.yaml — 代表指示 2026-08-03)。"""
    from ryza import org

    embed = a18.build_alert_embed(_result([], []))
    assert embed["author"] == org.author_for_role("audit")


def test_embed_alert_on_direct_push_only():
    """直 push のみでも要対応(has_findings)になり、A-18-4 節に載る。"""
    d = {"commit": "beef00beef00", "subject": "s", "files": ["README.md"], "reason": "r"}
    result = _result([], [], [d])
    assert a18.has_findings(result)
    embed = a18.build_alert_embed(result)
    assert "要対応" in embed["title"]
    field = next(f for f in embed["fields"] if "A-18-4" in f["name"] and "⚠️" in f["name"])
    assert "beef00beef00" in field["value"]


def test_enqueue_alert_writes_ops_outbox(conn, run_id):
    v = {"commit": "abc123def456", "subject": "s", "files": ["CLAUDE.md"], "reason": "r"}
    oid = a18.enqueue_alert(conn, _result([v], []), run_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, urgent, embed_json->>'title' FROM press.outbox WHERE id = %s",
            (oid,),
        )
        channel, urgent, title = cur.fetchone()
    assert channel == "ops"
    assert urgent is True  # 保護領域違反は urgent
    assert "A-18" in title


def test_enqueue_alert_direct_push_is_urgent(conn, run_id):
    d = {"commit": "beef00beef00", "subject": "s", "files": ["README.md"], "reason": "r"}
    oid = a18.enqueue_alert(conn, _result([], [], [d]), run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT urgent FROM press.outbox WHERE id = %s", (oid,))
        (urgent,) = cur.fetchone()
    assert urgent is True  # 直 push も統制違反として urgent
