"""A-13 規則⇔実装トレーサビリティ監査のテスト。

一時 git リポジトリを作り、保護領域突合(A-13-1)を承認あり/なし/PR マージ/発効日前の
4 象限で検証する。バージョン整合(A-13-2)・宣言棚卸し(A-13-3)はフィクスチャファイルで、
outbox 投入はテスト専用 DB で検証する。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ryza.audit import a13

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


def _run_a131(repo_path: Path, since: str | None):
    gov = a13.load_governance(repo_path)
    return a13.check_protected_commits(repo_path, gov, since_commit=since)


# ────────────────────────────────────────────────────────────────────────────
# A-13-1 保護領域突合
# ────────────────────────────────────────────────────────────────────────────
def test_unapproved_direct_commit_is_violation(repo):
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: 無承認の保護領域変更")
    violations, checked = _run_a131(r, since)
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
    violations, _ = _run_a131(r, since)
    assert violations == []


def test_trailer_without_reference_is_violation(repo):
    r, since = repo
    _commit(r, "docs/protected.md", "v2\n", "docs: 空トレーラ\n\nApproved:")
    violations, _ = _run_a131(r, since)
    assert len(violations) == 1


def test_pr_merge_commit_path_is_ok(repo):
    r, since = repo
    _git(r, "checkout", "-q", "-b", "feature")
    _commit(r, "src/prot/ks.py", "x = 1\n", "feat: 保護コード変更(ブランチ)")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "feature", "-m", "Merge pull request #5 from k/feature")
    violations, _ = _run_a131(r, since)
    assert violations == []


def test_non_pr_merge_path_is_violation(repo):
    """PR でないただの --no-ff マージ経由は承認と見なさない。"""
    r, since = repo
    _git(r, "checkout", "-q", "-b", "feature")
    sha = _commit(r, "src/prot/ks.py", "x = 1\n", "feat: 保護コード変更(ブランチ)")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "feature", "-m", "ローカルマージ(PR でない)")
    violations, _ = _run_a131(r, since)
    assert [v["commit"] for v in violations] == [sha[:12]]


def test_pre_ratification_commits_are_excluded(repo):
    """発効日前(批准コミットの祖先)の無承認変更は対象外。since=None だと対象になる。"""
    r, since = repo
    violations, checked = _run_a131(r, since)
    assert violations == [] and checked == 0
    # since=None なら全履歴が対象になり、発効前の無承認変更が検出される。
    violations_all, _ = _run_a131(r, None)
    assert len(violations_all) == 1
    assert violations_all[0]["subject"].startswith("pre:")


def test_unprotected_change_is_ignored(repo):
    r, since = repo
    _commit(r, "README.md", "hello\n", "docs: 保護外の変更")
    _commit(
        r, "migrations/sub/0001_x.sql", "-- sub\n",
        "sql: サブディレクトリは migrations/*.sql の対象外",
    )
    violations, checked = _run_a131(r, since)
    assert violations == [] and checked == 2


def test_unknown_since_commit_raises(repo):
    r, _ = repo
    with pytest.raises(ValueError):
        _run_a131(r, "deadbeef" * 5)


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
    violations, _ = _run_a131(r, since)
    assert len(violations) == 1
    assert "evil merge" in violations[0]["reason"]
    assert violations[0]["files"] == ["docs/protected.md"]


def test_evil_merge_with_trailer_is_ok(repo):
    r, since = repo
    _make_evil_merge(
        r, "Merge pull request #9 from k/feature\n\nApproved: https://x/3"
    )
    violations, _ = _run_a131(r, since)
    assert violations == []


# ────────────────────────────────────────────────────────────────────────────
# glob 変換
# ────────────────────────────────────────────────────────────────────────────
def test_glob_to_regex_semantics():
    assert a13.glob_to_regex("migrations/*.sql").match("migrations/0001_a.sql")
    assert not a13.glob_to_regex("migrations/*.sql").match("migrations/sub/a.sql")
    assert a13.glob_to_regex("src/ryza/ledger/**").match("src/ryza/ledger/a/b.py")
    assert a13.glob_to_regex("CLAUDE.md").match("CLAUDE.md")
    assert not a13.glob_to_regex("CLAUDE.md").match("docs/CLAUDE.md")


# ────────────────────────────────────────────────────────────────────────────
# A-13-2 文書⇔config 整合
# ────────────────────────────────────────────────────────────────────────────
def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_version_match_ok(tmp_path: Path):
    _write(tmp_path, "docs/design/80-ips.md", "# Ryza 投資方針書(IPS)v1.3\n本文\n")
    _write(tmp_path, "config/ips.yaml", 'version: "1.3"\n')
    pairs = (("docs/design/80-ips.md", "config/ips.yaml"),)
    assert a13.check_versions(tmp_path, pairs) == []


def test_version_mismatch_detected(tmp_path: Path):
    _write(tmp_path, "docs/design/06-constitution.md", "# Ryza 定款(Constitution)v0.3\n")
    _write(tmp_path, "config/governance.yaml", 'version: "0.2"\n')
    pairs = (("docs/design/06-constitution.md", "config/governance.yaml"),)
    out = a13.check_versions(tmp_path, pairs)
    assert len(out) == 1
    assert out[0]["doc_version"] == "0.3" and out[0]["config_version"] == "0.2"


def test_version_missing_file_is_mismatch(tmp_path: Path):
    pairs = (("docs/design/80-ips.md", "config/ips.yaml"),)
    out = a13.check_versions(tmp_path, pairs)
    assert len(out) == 1 and out[0]["reason"] == "バージョン表記が取得できない"


def test_real_repo_versions_match():
    """実リポジトリの 80-ips.md⇔ips.yaml / 06-constitution.md⇔governance.yaml が一致している。"""
    root = Path(__file__).resolve().parents[2]
    assert a13.check_versions(root) == []


# ────────────────────────────────────────────────────────────────────────────
# A-13-3 宣言棚卸し・注記
# ────────────────────────────────────────────────────────────────────────────
def test_declarations_inventory(repo):
    r, _ = repo
    gov = a13.load_governance(r)
    decls = a13.list_declarations(gov)
    assert [d["rule"] for d in decls] == ["宣言その1(異議経路の保護)", "宣言その2(反対意見書)"]


def test_run_a13_end_to_end_on_tmp_repo(repo):
    r, since = repo
    _commit(r, "docs/protected.md", "v2\n", "docs: 無承認変更")
    result = a13.run_a13(r, since_commit=since)
    assert len(result["violations"]) == 1
    assert result["mismatches"] != []  # 80-ips.md 等が無い一時リポジトリでは不整合になる
    assert len(result["declarations"]) == 2
    # 監査部門コードのパス未登録の注記(governance.yaml のコメントで予告された検査)。
    assert any("src/ryza/audit" in n for n in result["notes"])
    assert a13.has_findings(result)


def test_standard_disclosures_always_in_notes(repo):
    """既知の限界(PR 件名未照合・トレーラ実在未照合・evil merge 検査方式)は毎回開示される。"""
    r, since = repo
    result = a13.run_a13(r, since_commit=since)
    for disclosure in a13.STANDARD_DISCLOSURES:
        assert disclosure in result["notes"]
    # 開示は embed の注記フィールドにも常に載る。
    embed = a13.build_alert_embed(result)
    assert any(f["name"] == "注記" for f in embed["fields"])


def test_stale_checkout_warning(repo):
    """HEAD が origin/main を含まない場合は stale checkout を警告する(fetch はしない)。"""
    r, since = repo
    # origin/main が HEAD に無いコミットを指す状況を作る。
    _git(r, "checkout", "-q", "-b", "newer")
    newer = _commit(r, "README.md", "newer\n", "docs: newer")
    _git(r, "checkout", "-q", "main")
    _git(r, "update-ref", "refs/remotes/origin/main", newer)
    result = a13.run_a13(r, since_commit=since)
    assert any("stale checkout" in n for n in result["notes"])
    # origin/main が HEAD の祖先なら警告しない。
    _git(r, "update-ref", "refs/remotes/origin/main", since)
    result2 = a13.run_a13(r, since_commit=since)
    assert not any("stale checkout" in n for n in result2["notes"])


# ────────────────────────────────────────────────────────────────────────────
# embed・outbox 投入(テスト専用 DB)
# ────────────────────────────────────────────────────────────────────────────
def _result(violations: list, mismatches: list) -> dict:
    return {
        "as_of": "2026-08-03T00:00:00+00:00",
        "since_commit": a13.RATIFICATION_COMMIT,
        "checked_commits": 3,
        "violations": violations,
        "mismatches": mismatches,
        "declarations": [{"rule": "宣言その1", "verification": "棚卸し"}],
        "notes": [],
    }


def test_embed_alert_on_violation():
    v = {"commit": "abc123def456", "subject": "s", "files": ["CLAUDE.md"], "reason": "r"}
    embed = a13.build_alert_embed(_result([v], []))
    assert "要対応" in embed["title"]
    assert any("abc123def456" in f["value"] for f in embed["fields"])


def test_embed_clean_when_no_findings():
    embed = a13.build_alert_embed(_result([], []))
    assert "所見なし" in embed["title"]


def test_enqueue_alert_writes_ops_outbox(conn, run_id):
    v = {"commit": "abc123def456", "subject": "s", "files": ["CLAUDE.md"], "reason": "r"}
    oid = a13.enqueue_alert(conn, _result([v], []), run_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, urgent, embed_json->>'title' FROM press.outbox WHERE id = %s",
            (oid,),
        )
        channel, urgent, title = cur.fetchone()
    assert channel == "ops"
    assert urgent is True  # 保護領域違反は urgent
    assert "A-13" in title
