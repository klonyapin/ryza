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
import yaml

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
    """A-18-1 を DB 無しで実行し ``(違反, 検査数)`` を返す(承継・所見は別ヘルパで見る)。"""
    violations, _inherited, checked, _findings = _run_a181_full(repo_path, since)
    return violations, checked


def _run_a181_full(repo_path: Path, since: str | None, conn=None):
    """A-18-1 の全戻り値 ``(違反, 承継, 検査数, トレーラ所見)``。"""
    gov = a18.load_governance(repo_path)
    return a18.check_protected_commits(repo_path, gov, since_commit=since, conn=conn)


def _run_a181_db(repo_path: Path, since: str | None, conn):
    """DB 接続つきの A-18-1(トレーラ実在照合あり)。``(違反, トレーラ所見)`` を返す。"""
    violations, _inherited, _checked, findings = _run_a181_full(repo_path, since, conn)
    return violations, findings


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
# A-18-1 PR 承継(2026-08-04 設計リード裁定)
# ────────────────────────────────────────────────────────────────────────────
APPROVED = "Approved: https://github.com/x/y/pull/9"


def _merge_pr_with_evil_merge(r: Path, merge_message: str) -> tuple[str, str]:
    """ブランチ内で「origin/main 統合の evil merge」を作り、PR マージで main へ取り込む。

    実運用の worktree フロー(ブランチ作業中に origin/main を統合してコンフリクトを解消する)
    を再現する。戻り値は (ブランチ内 evil merge の sha, main 側 PR マージの sha)。
    """
    base = _git(r, "rev-parse", "HEAD").strip()
    _git(r, "checkout", "-q", "-b", "prfeature")
    _commit(r, "docs/protected.md", "branch side\n", "docs: ブランチ側の保護領域変更")
    _git(r, "checkout", "-q", "main")
    _commit(r, "docs/protected.md", "main side\n", "docs: main 側\n\n" + APPROVED)
    main_tip = _git(r, "rev-parse", "HEAD").strip()
    _git(r, "checkout", "-q", "prfeature")
    conflict = subprocess.run(
        ["git", "-C", str(r), "merge", main_tip], capture_output=True, text=True, check=False
    )
    assert conflict.returncode != 0  # コンフリクトが起きていること
    (r / "docs/protected.md").write_text("resolved: neither side\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "merge: origin/main をブランチへ統合(コンフリクト解消)")
    evil = _git(r, "rev-parse", "HEAD").strip()
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "prfeature", "-m", merge_message)
    assert base  # 起点の記録(可読性のため)
    return evil, _git(r, "rev-parse", "HEAD").strip()


def test_pr_inheritance_covers_in_branch_evil_merge(repo):
    """トレーラ有効な PR マージが持ち込んだブランチ内 evil merge は違反にならない。"""
    r, since = repo
    evil, merge = _merge_pr_with_evil_merge(
        r, "Merge pull request #9 from k/feature\n\n" + APPROVED
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert violations == []
    assert [i["commit"] for i in inherited] == [evil[:12]]
    assert inherited[0]["merge"] == merge[:12]
    assert inherited[0]["files"] == ["docs/protected.md"]


def test_pr_inheritance_is_visible_in_report(repo):
    """承継は黙って消さず、起点 PR ごとの集計行として報告に出す。"""
    r, since = repo
    evil, merge = _merge_pr_with_evil_merge(
        r, "Merge pull request #9 from k/feature\n\n" + APPROVED
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [i["commit"] for i in result["inherited"]] == [evil[:12]]
    field = next(
        f for f in a18.build_alert_embed(result)["fields"] if f["name"].startswith("PR 承継で承認")
    )
    assert "1 コミット" in field["name"]
    assert merge[:12] in field["value"] and "1 コミット" in field["value"]
    # 件数だけでなく「何を免除したか」= 保護パスも出す(独立役員審査 2026-08-04 中-3)。
    assert "docs/protected.md" in field["value"]


def test_pr_inheritance_requires_trailer_on_the_merge(repo):
    """トレーラの無い PR マージ配下の evil merge は従来どおり違反(初期 PR #56 型)。"""
    r, since = repo
    evil, _ = _merge_pr_with_evil_merge(r, "Merge pull request #9 from k/feature")
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert [v["commit"] for v in violations] == [evil[:12]]
    assert "evil merge" in violations[0]["reason"]
    assert inherited == []


def test_pr_inheritance_requires_pr_merge_subject(repo):
    """件名がマージ形式でない(= PR でない)統合は、トレーラがあっても承継の起点にしない。"""
    r, since = repo
    evil, _ = _merge_pr_with_evil_merge(r, "ローカルマージ(PR でない)\n\n" + APPROVED)
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert inherited == []
    # evil merge に加え、ブランチ内の通常コミットも附則(b)の対象外になり違反として残る。
    assert evil[:12] in [v["commit"] for v in violations]


def test_first_parent_merge_itself_is_not_inherited(repo):
    """first-parent 上のマージ自身の解消差分は承継の対象外(件名偽装の防御を維持)。

    main 側で直接コンフリクト解消したマージ(= first-parent 上)は、トレーラを持つ別の PR の
    配下に見えても承継しない。起点になれるのは自分より前の first-parent マージだけである。
    """
    r, since = repo
    _merge_pr_with_evil_merge(r, "Merge pull request #9 from k/feature\n\n" + APPROVED)
    _make_evil_merge(r, "Merge pull request #10 from k/other")  # トレーラなし・main 上
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert len(violations) == 1
    assert "evil merge" in violations[0]["reason"]
    assert len(inherited) == 1  # 先の PR #9 配下の承継は維持される


def test_inheritance_does_not_cover_direct_main_commits(repo):
    """main への直コミットは、後続 PR にトレーラがあっても承継されない。"""
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: main への直コミット")
    _merge_pr(r, "f1", "src/prot/ks.py", "x = 1\n", 11)
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert [v["commit"] for v in violations] == [sha[:12]]
    assert inherited == []


def test_octopus_merge_cannot_be_an_inheritance_origin(repo):
    """octopus マージ(親3以上)は PR 件名+トレーラを付けても承継・附則(b)の起点にしない。

    GitHub の PR マージは常に親2。octopus に PR 件名を付けると複数ブランチの内容を1つの
    承認で通せてしまう(独立役員審査 2026-08-04 中-3 の PoC)。
    """
    r, since = repo
    _git(r, "checkout", "-q", "-b", "o1")
    a = _commit(r, "docs/protected.md", "a\n", "docs: o1 の保護領域変更")
    _git(r, "checkout", "-q", "main")
    _git(r, "checkout", "-q", "-b", "o2")
    b = _commit(r, "src/prot/ks.py", "x = 1\n", "feat: o2 の保護領域変更")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "o1", "o2",
         "-m", "Merge pull request #9 from k/octopus\n\n" + APPROVED)
    assert len(_git(r, "log", "-1", "--format=%P").split()) == 3  # octopus であること
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert inherited == []
    assert sorted(v["commit"] for v in violations) == sorted([a[:12], b[:12]])
    assert all("親2" in v["reason"] for v in violations)


def test_plain_branch_commits_are_not_counted_as_inherited(repo):
    """従来どおり附則(b)で承認されるブランチ内の通常コミットは承継集計に載せない。

    集計「PR 承継で承認: N」は『承継が無ければ違反になっていたコミット』だけを数える
    (毎週の集計を実質的な件数に保つため)。
    """
    r, since = repo
    _merge_pr(r, "f1", "src/prot/ks.py", "x = 1\n", 12)
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert violations == [] and inherited == []


# ────────────────────────────────────────────────────────────────────────────
# A-18-1 既知違反の受容(acknowledged_findings — 独立役員審査 C-3)
# ────────────────────────────────────────────────────────────────────────────
def _acknowledge(r: Path, commit: str, paths: list[str]) -> None:
    """一時リポジトリの governance.yaml に受容エントリを1件書き込む(コミットはしない)。"""
    gov = yaml.safe_load(GOV_YAML)
    gov["acknowledged_findings"] = [
        {
            "commit": commit,
            "paths": paths,
            "reason": "是正不能な歴史的 evil merge(テスト)",
            "approval_ref": "https://github.com/x/y/pull/1",
            "acknowledged_on": "2026-08-03",
        }
    ]
    (r / "config" / "governance.yaml").write_text(
        yaml.safe_dump(gov, allow_unicode=True), encoding="utf-8"
    )


def test_acknowledged_finding_is_reported_separately_not_counted(repo):
    """受容済みは violations から外れるが、acknowledged として必ず残る(黙って消さない)。"""
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: 是正不能な無承認変更")
    _acknowledge(r, sha, ["docs/protected.md"])
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert result["violations"] == []
    assert [a["commit"] for a in result["acknowledged"]] == [sha[:12]]
    assert result["acknowledged"][0]["approval_ref"] == "https://github.com/x/y/pull/1"
    # 別枠フィールドに件数と SHA が必ず載る。
    embed = a18.build_alert_embed(result)
    field = next(f for f in embed["fields"] if f["name"].startswith("受容済み既知違反"))
    assert "1 件" in field["name"] and sha[:12] in field["value"]
    # A-18-1 の警告フィールドは出ない(受容済みだけでは要対応にしない)。
    assert not any("A-18-1 保護領域の無承認変更" in f["name"] for f in embed["fields"])


def test_unacknowledged_finding_still_violation(repo):
    """受容リストに載っていない違反は従来どおり違反として出る。"""
    r, since = repo
    acked = _commit(r, "docs/protected.md", "v2\n", "docs: 受容する変更")
    other = _commit(r, "src/prot/ks.py", "x = 1\n", "feat: 受容しない変更")
    _acknowledge(r, acked, ["docs/protected.md"])
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [v["commit"] for v in result["violations"]] == [other[:12]]
    assert [a["commit"] for a in result["acknowledged"]] == [acked[:12]]


def test_acknowledgement_with_wrong_sha_has_no_effect(repo):
    """SHA が一致しない受容エントリは効かない(違反のまま)+ 陳腐化を notes で開示する。"""
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: 無承認変更")
    _acknowledge(r, "0" * 40, ["docs/protected.md"])
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [v["commit"] for v in result["violations"]] == [sha[:12]]
    assert result["acknowledged"] == []
    assert any("acknowledged_findings のエントリが一致する違反を持たない" in n
               for n in result["notes"])


def test_acknowledgement_with_wrong_paths_has_no_effect(repo):
    """SHA が合っていてもパス集合が違えば効かない(将来の別の違反を巻き込まない)。"""
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: 無承認変更")
    _acknowledge(r, sha, ["src/prot/ks.py"])
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [v["commit"] for v in result["violations"]] == [sha[:12]]
    assert result["acknowledged"] == []


def test_acknowledgement_requires_full_path_set(repo):
    """複数の保護パスに触れた違反は、その全パスを列挙した受容エントリでのみ受容される。"""
    r, since = repo
    (r / "docs").mkdir(exist_ok=True)
    (r / "docs" / "protected.md").write_text("v2\n", encoding="utf-8")
    sha = _commit(r, "src/prot/ks.py", "x = 1\n", "chore: 2つの保護パスに触れる無承認変更")
    _acknowledge(r, sha, ["docs/protected.md"])  # 片方だけ → 効かない
    partial = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [v["commit"] for v in partial["violations"]] == [sha[:12]]
    _acknowledge(r, sha, ["src/prot/ks.py", "docs/protected.md"])  # 全部(順序は不問)
    full = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert full["violations"] == []
    assert len(full["acknowledged"]) == 1


def test_incomplete_acknowledgement_entry_is_ignored(repo):
    """commit か paths を欠くエントリは受容として効かせない(fail-safe = 違反のまま)。"""
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: 無承認変更")
    gov = yaml.safe_load(GOV_YAML)
    gov["acknowledged_findings"] = [{"commit": sha}, {"paths": ["docs/protected.md"]}]
    (r / "config" / "governance.yaml").write_text(
        yaml.safe_dump(gov, allow_unicode=True), encoding="utf-8"
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [v["commit"] for v in result["violations"]] == [sha[:12]]


def test_acknowledged_only_result_is_not_alerting():
    """受容済みだけなら has_findings は False(週次の恒久的な赤を作らない)。"""
    result = _result([], [])
    result["acknowledged"] = [
        {"commit": "abc123def456", "subject": "s", "files": ["CLAUDE.md"],
         "reason": "r", "approval_ref": "https://x/1"}
    ]
    assert not a18.has_findings(result)
    embed = a18.build_alert_embed(result)
    assert "所見なし" in embed["title"]
    # 所見なしでも受容の存在は隠さない。
    assert any(f["name"].startswith("受容済み既知違反") for f in embed["fields"])


def test_partition_acknowledged_is_pure():
    """分割関数は単体でも使える(完全 SHA の一致・commit_full が無い場合は commit で突合)。"""
    sha = "a" * 40
    gov = {"acknowledged_findings": [{"commit": sha, "paths": ["CLAUDE.md"]}]}
    v = {"commit": sha[:12], "commit_full": sha, "subject": "s",
         "files": ["CLAUDE.md"], "reason": "r"}
    unack, ack, notes = a18.partition_acknowledged([v], gov)
    assert unack == [] and len(ack) == 1 and notes == []


def test_short_sha_acknowledgement_is_invalid_and_disclosed():
    """短縮 SHA のエントリは索引に入れず「無効」として notes 開示する(低-7)。"""
    gov = {"acknowledged_findings": [{"commit": "abc123def456", "paths": ["CLAUDE.md"]}]}
    v = {"commit": "abc123def456", "commit_full": "abc123def456" + "0" * 28,
         "subject": "s", "files": ["CLAUDE.md"], "reason": "r"}
    unack, ack, notes = a18.partition_acknowledged([v], gov)
    assert len(unack) == 1 and ack == []
    assert any("40 桁 hex の完全 SHA が必要" in n for n in notes)


def test_incomplete_acknowledgement_entry_is_disclosed():
    """commit / paths 欠落のエントリも黙って落とさず notes に出す(低-7)。"""
    gov = {"acknowledged_findings": [{"commit": "b" * 40}, {"paths": ["CLAUDE.md"]}]}
    _unack, _ack, notes = a18.partition_acknowledged([], gov)
    assert sum("commit / paths のいずれかが欠落" in n for n in notes) == 2


# ── 実リポジトリの受容記録(承認手続の固定)────────────────────────────────────
def _real_governance() -> dict:
    root = Path(__file__).resolve().parents[2]
    return a18.load_governance(root)


def test_real_acknowledged_findings_are_well_formed():
    """受容エントリは commit(完全 SHA)・paths・理由・承認記録の参照・受容日を必ず持つ。"""
    entries = _real_governance().get("acknowledged_findings") or []
    assert entries, "既知違反の受容記録が空(機構の導線が消えていないか確認すること)"
    for e in entries:
        assert len(str(e["commit"])) == 40, f"完全 SHA でない受容エントリ: {e['commit']}"
        assert e["paths"], f"paths が空: {e['commit']}"
        assert str(e.get("reason", "")).strip(), f"受容理由が無い: {e['commit']}"
        assert str(e.get("approval_ref", "")).strip(), f"承認記録の参照が無い: {e['commit']}"
        assert str(e.get("acknowledged_on", "")).strip(), f"受容日が無い: {e['commit']}"


def test_acknowledgement_registration_requires_approval():
    """受容エントリの追加自体が承認手続を要する = 置き場所が保護領域であることの固定。

    acknowledged_findings は config/governance.yaml にのみ置く。同ファイルが protected_areas に
    登録されている限り、エントリ追加のコミットは A-18-1 の突合対象となり承認記録を要求される。
    """
    gov = _real_governance()
    paths = [str(e["path"]) for e in gov["protected_areas"]]
    assert a18.GOVERNANCE_PATH in paths
    assert "src/ryza/audit/**" in paths  # 受容を実装する側のコードも保護領域


def test_real_repo_acknowledged_findings_are_matched():
    """実リポジトリの受容エントリが実在の違反に一致している(陳腐化していない)。"""
    root = Path(__file__).resolve().parents[2]
    result = a18.run_a18(root)
    assert len(result["acknowledged"]) == len(_real_governance()["acknowledged_findings"])
    assert not any("acknowledged_findings のエントリが一致する違反を持たない" in n
                   for n in result["notes"])


# ────────────────────────────────────────────────────────────────────────────
# A-18-1 Approved トレーラの実在照合(governance.current_decisions との突合)
#
# 「トレーラがある」で受理すると、代表が否認した承認を A-18 が承認として受理し、
# 取消義務が生じている変更が無承認変更として検出されない(独立役員審査 0021 C-5)。
# 照合は必ず現決定 view 経由で行い、否認済みは受理しない。
# ────────────────────────────────────────────────────────────────────────────
OWNER = "424242"
OWNERS = (OWNER,)


def test_decision_ref_id_requires_prefix():
    """裸の数字は決定 ID として解釈しない(Issue 番号との偶然一致が fail-open — 重要-2)。"""
    assert a18.decision_ref_id("decision:45") == 45
    assert a18.decision_ref_id("123") is None
    assert a18.decision_ref_id("https://github.com/x/y/issues/1") is None
    assert a18.decision_ref_id("#12") is None


def test_approval_trailer_refs_collects_all():
    msg = "fix: x\n\nApproved: decision:12\nApproved: https://github.com/x/y/issues/3\n"
    assert a18.approval_trailer_refs(msg) == ["decision:12", "https://github.com/x/y/issues/3"]


def _deemed(conn, run_id, proposal_ref: str) -> int:
    """みなし承認を1件記録し decision id を返す(通知と同一トランザクション)。"""
    from ryza.governance import notices

    return notices.announce_deemed_approval(
        conn, proposal_ref, "pr", "保護領域の変更", run_id
    ).decision.id


def _veto(conn, run_id, proposal_ref: str) -> None:
    from ryza.governance import notices

    notices.apply_veto(
        conn, proposal_ref, "リスク上限を緩める方向のため",
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id,
    )


def _commit_with_trailer(r: Path, ref: str) -> str:
    return _commit(
        r, "docs/protected.md", f"v-{ref}\n", f"docs: 保護領域変更\n\nApproved: {ref}"
    )


def test_deemed_decision_trailer_is_accepted(repo, conn, run_id):
    """みなし承認(deemed)を指すトレーラは承認記録として受理される(0019 C-3 の⑤)。"""
    r, since = repo
    decision_id = _deemed(conn, run_id, "https://github.com/x/y/pull/101")
    _commit_with_trailer(r, f"decision:{decision_id}")
    violations, findings = _run_a181_db(r, since, conn)
    assert violations == [] and findings == []
    conn.rollback()


def test_pr_url_trailer_is_matched_by_proposal_ref(repo, conn, run_id):
    """**重大-1**: PR URL のトレーラは proposal_ref 一致で解決される。

    本リポジトリの承認記録は全件 PR URL であり、deemed 記録の proposal_ref も同じ URL。
    ID 形式だけを照合していた実装では、否認済み承認がそのまま受理されていた。
    """
    r, since = repo
    url = "https://github.com/x/y/pull/201"
    _deemed(conn, run_id, url)
    _commit_with_trailer(r, url)
    violations, _ = _run_a181_db(r, since, conn)
    assert violations == []
    # 否認すると同じトレーラが受理されなくなる。
    _veto(conn, run_id, url)
    violations_after, _ = _run_a181_db(r, since, conn)
    assert len(violations_after) == 1
    assert "否認済み" in violations_after[0]["reason"]
    conn.rollback()


def test_vetoed_decision_trailer_is_violation(repo, conn, run_id):
    """否認された承認を指すトレーラは受理しない(取消されるまで無承認変更)。"""
    r, since = repo
    decision_id = _deemed(conn, run_id, "https://github.com/x/y/pull/102")
    _veto(conn, run_id, "https://github.com/x/y/pull/102")
    _commit_with_trailer(r, f"decision:{decision_id}")
    violations, _ = _run_a181_db(r, since, conn)
    assert len(violations) == 1
    assert "否認済み" in violations[0]["reason"]
    conn.rollback()


def _merge_pr_with_evil_merge_trailer(r: Path, ref: str) -> tuple[str, str]:
    """PR マージのトレーラに ``ref`` を書いた「ブランチ内 evil merge 付き PR」を作る。"""
    return _merge_pr_with_evil_merge(
        r, f"Merge pull request #9 from k/prfeature\n\nApproved: {ref}"
    )


def test_vetoed_pr_trailer_cannot_be_an_inheritance_origin(repo, conn, run_id):
    """**承継 × 否認**: 否認済みの PR トレーラは承継の起点にならず、配下は違反のまま。

    承継の起点判定を素の has_approval_trailer で行うと、この経路だけ否認照合を迂回して
    「否認された PR の内容がブランチ丸ごと承認扱い」になる(2026-08-04 設計リード追達1)。
    """
    r, since = repo
    url = "https://github.com/x/y/pull/301"
    _deemed(conn, run_id, url)
    evil, _merge = _merge_pr_with_evil_merge_trailer(r, url)
    # 否認前: 承継が効き違反ゼロ。
    violations, inherited, _checked, _f = _run_a181_full(r, since, conn)
    assert violations == [] and [i["commit"] for i in inherited] == [evil[:12]]
    assert inherited[0]["decision_verified"] is True
    # 否認後: 起点が無効になり、配下の evil merge が違反として復活する。
    _veto(conn, run_id, url)
    violations_after, inherited_after, _c, _f2 = _run_a181_full(r, since, conn)
    assert inherited_after == []
    assert evil[:12] in [v["commit"] for v in violations_after]
    conn.rollback()


def test_inheritance_without_conn_is_disclosed(repo):
    """conn なし(照合不能)では形式的有効性のみで承継し、notes に件数を開示する。"""
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge_trailer(r, "https://github.com/x/y/pull/302")
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [i["commit"] for i in result["inherited"]] == [evil[:12]]
    assert result["inherited"][0]["decision_verified"] is False
    assert any("decisions 照合なしの承継 1 件" in n for n in result["notes"])


def test_pr_merge_does_not_rescue_a_vetoed_trailer(repo, conn, run_id):
    """PR マージ経由でも、トレーラが否認済みの承認を指すなら違反のまま。"""
    r, since = repo
    url = "https://github.com/x/y/pull/103"
    decision_id = _deemed(conn, run_id, url)
    _veto(conn, run_id, url)
    _git(r, "checkout", "-q", "-b", "feature-vetoed")
    sha = _commit(
        r, "src/prot/ks.py", "x = 1\n", f"feat: 保護コード\n\nApproved: decision:{decision_id}"
    )
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "feature-vetoed", "-m", "Merge pull request #7 from k/f")
    violations, _ = _run_a181_db(r, since, conn)
    assert [v["commit"] for v in violations] == [sha[:12]]
    assert "否認済み" in violations[0]["reason"]
    conn.rollback()


def test_missing_decision_record_is_violation(repo, conn):
    """存在しない決定 ID を指すトレーラは承認と見なさない(自己申告の空手形)。"""
    r, since = repo
    _commit_with_trailer(r, "decision:999999999")
    violations, _ = _run_a181_db(r, since, conn)
    assert len(violations) == 1
    assert "存在しない" in violations[0]["reason"]
    conn.rollback()


def test_rejected_decision_trailer_is_violation(repo, conn):
    """却下された決定を指すトレーラも承認ではない。"""
    from ryza.bot.approvals import record_decision

    r, since = repo
    got = record_decision(conn, "rejected-proposal", "reject", OWNER, OWNERS, kind="pr")
    _commit_with_trailer(r, f"decision:{got.id}")
    violations, _ = _run_a181_db(r, since, conn)
    assert len(violations) == 1
    assert "承認ではない" in violations[0]["reason"]
    conn.rollback()


def test_unknown_reference_is_accepted_and_disclosed(repo, conn):
    """DB に対応行が無い参照(Issue 決議)は従来どおり受理し、所見にも残さない。"""
    r, since = repo
    _commit_with_trailer(r, "https://github.com/x/y/issues/1")
    violations, findings = _run_a181_db(r, since, conn)
    assert violations == [] and findings == []
    conn.rollback()


def test_bare_number_trailer_is_unverifiable_and_noted(repo, conn):
    """裸の数字は照合せず受理し、「照合できない参照」として notes に開示する(重要-2)。"""
    r, since = repo
    _commit_with_trailer(r, "42")
    violations, findings = _run_a181_db(r, since, conn)
    assert violations == []
    assert len(findings) == 1
    assert findings[0]["problems"] == []
    assert any("decision:42" in u for u in findings[0]["unverifiable"])
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since, conn=conn)
    assert any("照合できない Approved 参照" in n for n in result["notes"])
    # 様式の不備であって統制違反ではない — 報告の要否を左右しない(⚠️ は点けない)。
    assert a18.vetoed_trailer_findings(result) == []
    conn.rollback()


def test_vetoed_reference_alongside_valid_one_is_listed(repo, conn, run_id):
    """有効な承認があっても否認済み参照は所見に残す(軽微-10)。"""
    r, since = repo
    ok_url = "https://github.com/x/y/pull/301"
    bad_url = "https://github.com/x/y/pull/302"
    _deemed(conn, run_id, ok_url)
    _deemed(conn, run_id, bad_url)
    _veto(conn, run_id, bad_url)
    _commit(
        r, "docs/protected.md", "v2\n",
        f"docs: 変更\n\nApproved: {ok_url}\nApproved: {bad_url}",
    )
    violations, findings = _run_a181_db(r, since, conn)
    assert violations == []  # 有効な承認があるので受理
    assert len(findings) == 1 and "否認済み" in findings[0]["problems"][0]
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since, conn=conn)
    assert a18.has_findings(result)  # 取消義務の検討対象として報告する
    embed = a18.build_alert_embed(result)
    assert any("否認済みの承認記録を参照" in f["name"] for f in embed["fields"])
    conn.rollback()


def test_without_conn_vetoed_trailer_is_not_detected(repo, conn, run_id):
    """conn 無しでは照合できない。従来動作を保ちつつ、その限界を notes で開示する。"""
    r, since = repo
    url = "https://github.com/x/y/pull/104"
    _deemed(conn, run_id, url)
    _veto(conn, run_id, url)
    _commit_with_trailer(r, url)
    violations, _ = _run_a181(r, since)
    assert violations == []  # 検出できない(= conn を渡さない実行の限界)
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert result["decision_refs_verified"] is False
    assert any("未照合" in n for n in result["notes"])
    conn.rollback()


# ────────────────────────────────────────────────────────────────────────────
# A-18-5 通知なき発効(未配送のみなし承認)
#
# 記録と outbox 投入が同一トランザクションでも、**投入は配送ではない**。配送が止まれば
# 「発効したが誰も知らない」状態が続き、定款第3条の発効要件が満たされない(重要-3)。
# ────────────────────────────────────────────────────────────────────────────
def _age_outbox(conn, outbox_id: int, minutes: int) -> None:
    """outbox 行の created_at を過去にずらす(滞留の再現)。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE press.outbox SET created_at = now() - make_interval(mins => %s) WHERE id = %s",
            (minutes, outbox_id),
        )


def test_fresh_unsent_notice_is_not_a_finding(conn, run_id):
    """投入直後の未配送は正常(配送ループは 5 秒間隔)。"""
    from ryza.governance import notices

    notices.announce_deemed_approval(conn, "a185-fresh", "pr", "要旨", run_id)
    findings, _ = a18.check_unnotified_deemed(conn)
    assert [f for f in findings if f["proposal_ref"] == "a185-fresh"] == []
    conn.rollback()


def test_stale_unsent_notice_is_a_violation(conn, run_id):
    """60 分を超えて未配送なら「通知なき発効」として違反。"""
    from ryza.governance import notices

    result = notices.announce_deemed_approval(conn, "a185-stale", "pr", "要旨", run_id)
    _age_outbox(conn, result.outbox_id, 120)
    findings, _ = a18.check_unnotified_deemed(conn)
    mine = [f for f in findings if f["proposal_ref"] == "a185-stale"]
    assert len(mine) == 1
    assert mine[0]["decision_id"] == result.decision.id
    assert mine[0]["notice_ref"] == result.notice_ref
    assert "未配送" in mine[0]["reason"]
    conn.rollback()


def test_delivered_notice_is_not_a_finding(conn, run_id):
    """配送済み(sent_at あり)なら滞留していても所見にしない。"""
    from ryza.bot.outbox import mark_sent
    from ryza.governance import notices

    result = notices.announce_deemed_approval(conn, "a185-sent", "pr", "要旨", run_id)
    _age_outbox(conn, result.outbox_id, 120)
    mark_sent(conn, result.outbox_id, "123456")
    findings, _ = a18.check_unnotified_deemed(conn)
    assert [f for f in findings if f["proposal_ref"] == "a185-sent"] == []
    conn.rollback()


def test_manual_notice_ref_is_reported_as_untracked(conn):
    """``outbox:`` 形式でない通知参照(手作業の記録)は追跡不能として数える。"""
    from ryza.governance.decisions import record_deemed_approval

    record_deemed_approval(conn, "a185-manual", "pr", "discord://承認/12345")
    _findings, untracked = a18.check_unnotified_deemed(conn)
    assert untracked >= 1
    conn.rollback()


def test_unnotified_deemed_reaches_report_and_is_urgent(conn, run_id, repo):
    """A-18-5 の所見は報告 embed に載り、urgent として投入される。"""
    from ryza.governance import notices

    r, since = repo
    result_notice = notices.announce_deemed_approval(conn, "a185-report", "pr", "要旨", run_id)
    _age_outbox(conn, result_notice.outbox_id, 120)
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since, conn=conn)
    assert result["unnotified_deemed"]
    assert a18.has_findings(result)
    embed = a18.build_alert_embed(result)
    assert any("A-18-5" in f["name"] and "⚠️" in f["name"] for f in embed["fields"])
    oid = a18.enqueue_alert(conn, result, run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT urgent FROM press.outbox WHERE id = %s", (oid,))
        assert cur.fetchone()[0] is True
    conn.rollback()


def test_run_a18_with_conn_marks_refs_verified(repo, conn):
    r, since = repo
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since, conn=conn)
    assert result["decision_refs_verified"] is True
    conn.rollback()


def test_real_repo_trailers_resolve_without_conn():
    """実リポジトリの履歴で A-18-1 が壊れていない(conn 無しの従来経路)。

    #78 は浅い clone(fetch-depth 1)で ValueError になる本テストを skip で回避していたが、
    本ラインが ci.yml に ``fetch-depth: 0`` を入れて根本原因を消したため skip を外す
    (skip のままだと CI が浅くなったとき実リポジトリ検査が黙って行われなくなる —
    「沈黙を作らない」原則。tests/test_ips.py が fetch-depth: 0 の存在自体を固定している)。
    """
    root = Path(__file__).resolve().parents[2]
    gov = a18.load_governance(root)
    violations, inherited, checked, findings = a18.check_protected_commits(root, gov)
    assert isinstance(violations, list) and checked >= 0 and isinstance(findings, list)
    assert isinstance(inherited, list)


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


# ────────────────────────────────────────────────────────────────────────────
# A-18-6 決議の批判経由(形骸化の監査)
#
# 決議精緻化審査(2026-08-03)の新設統制。当初は週次ジョブ ops-weekly のダイジェストに
# 載せる設計だったが、ops-weekly は Cloud Run Job で VM 内 PostgreSQL に届かず、配線には
# 実行基盤の移設が要った。移設案は「監査が可変の稼働コードから走る」「env の1行削除が
# 『未配線』= 移設前と同一表示に化ける」穴を生んだため、既に監査専用 clone から週次で走り
# #運営 へ届く A-18 側へ載せた(VM 移設審査 2026-08-04 代替案(d)・設計リード裁定)。
#
# 走査窓・閾値は boardroom 側の定義を再利用する(監査側で再定義すると二重定義が静かにずれる)。
# テスト DB は他ワークツリーと共有しうるが、連続は**最新から**数えるため直下の k 件で決まる。
# ────────────────────────────────────────────────────────────────────────────
def _seed_bypassed_resolutions(conn, run_id: int, count: int) -> None:
    """「批判を経ない決議」(confirmed_without_critic=true)を count 件積む。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO governance.minutes (meeting, held_at, attendees, body_md, run_id)"
            " VALUES ('investment_committee', now(), %s, '自由記述の議事録', %s)"
            " RETURNING minute_id",
            (["representative"], run_id),
        )
        minute_id = cur.fetchone()[0]
        for i in range(count):
            cur.execute(
                "INSERT INTO governance.minute_resolutions"
                " (minute_id, seq, title, resolution_md, resolved_by,"
                "  confirmed_without_critic)"
                " VALUES (%s, %s, %s, '本文', 'representative', true)",
                (minute_id, i + 1, f"確認付き決議{i}"),
            )


def test_resolution_bypass_alerts_on_streak(conn, run_id):
    """批判を経ない決議が連続すると alert になり、1行に内訳が出る。"""
    from ryza.governance.boardroom import CONFIRMATION_STREAK_ALERT

    _seed_bypassed_resolutions(conn, run_id, CONFIRMATION_STREAK_ALERT)
    bypass = a18.check_resolution_bypass(conn)
    assert bypass["alert"] is True
    assert bypass["streak"] >= CONFIRMATION_STREAK_ALERT
    assert bypass["line"].startswith("⚠ 形骸化の疑い")
    conn.rollback()


def test_resolution_bypass_reports_line_even_without_alert(conn, run_id):
    """閾値未満でも行は返す(「アラートが無い」と「見ていない」を同一視させない)。"""
    _seed_bypassed_resolutions(conn, run_id, 1)
    bypass = a18.check_resolution_bypass(conn)
    assert bypass["line"]
    assert bypass["scanned"] >= 1
    conn.rollback()


def test_run_a18_includes_resolution_bypass_with_conn(repo, conn):
    """conn 付き実行では A-18-6 が結果に入る(conn 無しでは None + 未照合の注記)。"""
    r, since = repo
    with_conn = a18.run_a18(r, since_commit=since, pr_since_commit=since, conn=conn)
    assert with_conn["resolution_bypass"] is not None
    assert "line" in with_conn["resolution_bypass"]

    without = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert without["resolution_bypass"] is None
    assert any("A-18-6" in n for n in without["notes"])
    conn.rollback()


def test_embed_carries_resolution_bypass_line(conn, run_id):
    """⚠ 行が #運営 の embed に載り、報告そのものの要否(has_findings)も立てる。"""
    from ryza.governance.boardroom import CONFIRMATION_STREAK_ALERT

    _seed_bypassed_resolutions(conn, run_id, CONFIRMATION_STREAK_ALERT)
    result = _result([], [])
    result["resolution_bypass"] = a18.check_resolution_bypass(conn)
    assert a18.has_findings(result) is True

    embed = a18.build_alert_embed(result)
    field = next(f for f in embed["fields"] if "A-18-6" in f["name"])
    assert "⚠️" in field["name"]
    assert "批判を経ない決議" in field["value"]
    assert "要対応" in embed["title"]
    conn.rollback()


def test_embed_shows_resolution_line_when_not_alerting():
    """閾値未満では ⚠️ を点けず、行だけ載せる(毎回 ⚠️ にして本物を埋もれさせない)。"""
    result = _result([], [])
    result["resolution_bypass"] = {
        "scanned": 4, "confirmed": 1, "undetermined": 0, "bypassed": 1,
        "streak": 0, "alert": False,
        "line": "直近 4 件中 1 件が批判を経ない決議(確認付き 1 / 判定不能 0)/ 連続 0 件",
    }
    assert a18.has_findings(result) is False
    embed = a18.build_alert_embed(result)
    field = next(f for f in embed["fields"] if "A-18-6" in f["name"])
    assert "⚠️" not in field["name"]
    assert "所見なし" in embed["title"]
