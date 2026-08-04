"""A-18 規則⇔実装トレーサビリティ監査のテスト。

一時 git リポジトリを作り、保護領域突合(A-18-1)を承認あり/なし/PR マージ/発効日前の
4 象限で検証する。バージョン整合(A-18-2)・宣言棚卸し(A-18-3)はフィクスチャファイルで、
outbox 投入はテスト専用 DB で検証する。全変更 PR 化(A-18-4)は PR マージのみ/直 push/
非 PR マージ/基準以前の4象限+例外なし(Approved トレーラ付き直 push も違反)を検証する。

一時リポジトリでは実リポジトリの基準コミット(``PR_RULE_BASELINE_COMMIT``)が存在しない
ため、``run_a18`` には ``pr_since_commit`` を明示的に渡す。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def _merge_pr_with_evil_merge(
    r: Path, merge_message: str | Callable[[str], str]
) -> tuple[str, str]:
    """ブランチ内で「origin/main 統合の evil merge」を作り、PR マージで main へ取り込む。

    実運用の worktree フロー(ブランチ作業中に origin/main を統合してコンフリクトを解消する)
    を再現する。戻り値は (ブランチ内 evil merge の sha, main 側 PR マージの sha)。
    ``merge_message`` が callable なら evil merge の sha を渡して件名を組み立てる
    (様式 v2 の ``reviewed=<sha40>`` を書くため)。
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
    message = merge_message(evil) if callable(merge_message) else merge_message
    _git(r, "merge", "--no-ff", "-q", "prfeature", "-m", message)
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
# Approved トレーラ様式 v2(reviewed=<sha40> — 独立役員審査 2026-08-04 重大-2)
#
# 承継は「マージ時点のブランチ全体」に及ぶため、独立審査・#承認 通知の**後**に積んだ
# コミットも同じトレーラで承認扱いになる(審査後 push の吸収)。v2 の reviewed は審査対象を
# 固定し、承継をその祖先に限る。様式不備は「制限なし」ではなく「起点にしない」(fail-safe)。
# ────────────────────────────────────────────────────────────────────────────
def _pr_with_post_review_commit(r: Path, trailer_for) -> tuple[str, str]:
    """審査対象コミットと『審査後に積んだコミット』を持つ PR を main へマージする。

    戻り値は (審査対象コミット, 審査後コミット)。``trailer_for`` は審査対象 sha を受け取り
    マージ件名の本文(トレーラ行)を返す。
    """
    _git(r, "checkout", "-q", "-b", "prfeature")
    reviewed = _commit(r, "docs/protected.md", "reviewed\n", "docs: 審査を受けた保護領域変更")
    after = _commit(
        r, "src/prot/ks.py", "LIMIT = 999999\n", "feat: 審査後に積んだ Kill Switch 改変"
    )
    _git(r, "checkout", "-q", "main")
    _git(
        r, "merge", "--no-ff", "-q", "prfeature",
        "-m", f"Merge pull request #9 from k/prfeature\n\n{trailer_for(reviewed)}",
    )
    return reviewed, after


def test_reviewed_trailer_blocks_post_review_commits(repo):
    """**重大-2**: reviewed 付きなら、審査後に積まれたコミットは承継されず違反として出る。"""
    r, since = repo
    reviewed, after = _pr_with_post_review_commit(
        r, lambda sha: f"{APPROVED} reviewed={sha}"
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert [v["commit"] for v in violations] == [after[:12]]
    assert "reviewed" in violations[0]["reason"] and "審査後" in violations[0]["reason"]
    # 審査対象コミット自身は従来どおり PR 経由(附則 b)で承認される。
    assert reviewed[:12] not in [v["commit"] for v in violations]
    assert inherited == []


def test_v1_trailer_still_covers_post_review_commits(repo):
    """v1(reviewed 無し)は経過措置として従来どおり有効 — 審査後 push も吸収する。"""
    r, since = repo
    _reviewed, after = _pr_with_post_review_commit(r, lambda _sha: APPROVED)
    violations, _inherited, _checked, _findings = _run_a181_full(r, since)
    assert violations == []
    assert after  # 承継(附則 b)で通っていることの対比


def test_reviewed_scoped_inheritance_is_marked(repo):
    """reviewed が evil merge を含むなら承継は成立し、reviewed 限定として記録される。"""
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r, lambda ev: f"Merge pull request #9 from k/prfeature\n\n{APPROVED} reviewed={ev}"
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert violations == []
    assert [i["commit"] for i in inherited] == [evil[:12]]
    assert inherited[0]["reviewed_scoped"] is True and inherited[0]["reviewed"] == evil


def test_reviewed_before_the_commit_blocks_inheritance(repo):
    """reviewed が evil merge の**前**を指すなら、その evil merge は承継されない。"""
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r,
        lambda ev: "Merge pull request #9 from k/prfeature\n\n"
        f"{APPROVED} reviewed={_git(r, 'rev-parse', ev + '^1').strip()}",
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert inherited == []
    assert [v["commit"] for v in violations] == [evil[:12]]
    assert "審査後" in violations[0]["reason"]


def test_malformed_reviewed_is_not_an_inheritance_origin(repo):
    """40 桁 hex でない reviewed は様式不備 — 承継の起点にしない(fail-safe)。

    不備を「制限なし(= v1)」に読み替えると、``reviewed=x`` と書くだけで v2 の制限を外せる
    抜け道になる。承継((c))だけでなく附則(b)も止め、PR 配下は全て検査対象に戻す。
    """
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r, f"Merge pull request #9 from k/prfeature\n\n{APPROVED} reviewed=abc123"
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert inherited == []
    assert evil[:12] in [v["commit"] for v in violations]
    assert all("様式不備" in v["reason"] for v in violations)
    assert len(violations) == 2  # ブランチ内の通常コミットも(b)で救済されない


def test_unknown_reviewed_sha_is_not_an_inheritance_origin(repo):
    """リポジトリに存在しない reviewed は祖先判定ができない — 起点にしない(fail-safe)。"""
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r, f"Merge pull request #9 from k/prfeature\n\n{APPROVED} reviewed={'0' * 40}"
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert inherited == []
    assert evil[:12] in [v["commit"] for v in violations]
    assert all("存在せず" in v["reason"] for v in violations)


def test_v1_inheritance_count_is_disclosed(repo):
    """v1 の承継は違反にしないが、件数を notes と embed に必ず開示する(移行期の可視化)。"""
    r, since = repo
    _evil, _merge = _merge_pr_with_evil_merge(
        r, f"Merge pull request #9 from k/prfeature\n\n{APPROVED}"
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since, verify_prs=False)
    assert result["inherited"][0]["reviewed_scoped"] is False
    assert any("reviewed 無し(様式 v1)の承継 1 件" in n for n in result["notes"])
    field = next(
        f for f in a18.build_alert_embed(result)["fields"] if f["name"].startswith("PR 承継で承認")
    )
    assert "様式 v1" in field["value"]


def test_approval_trailers_parses_v2_attributes():
    """v2 は参照+``key=value`` の並び。参照の読み口(v1)は変わらない。"""
    sha = "a" * 40
    msg = (
        "docs: 保護領域変更\n\n"
        f"Approved: https://github.com/k/y/pull/9 reviewed={sha} mode=deemed\n"
    )
    lines = a18.approval_trailers(msg)
    assert len(lines) == 1
    assert lines[0].ref == "https://github.com/k/y/pull/9"
    assert lines[0].attrs == {"reviewed": sha, "mode": "deemed"}
    assert a18.approval_trailer_refs(msg) == ["https://github.com/k/y/pull/9"]
    assert a18.reviewed_shas(msg) == ((sha,), None)


def test_reviewed_shas_rejects_short_sha():
    shas, problem = a18.reviewed_shas("docs: x\n\nApproved: https://x/1 reviewed=abc123\n")
    assert shas == () and "40 桁" in problem
    # reviewed が無いトレーラは v1 として扱う(不備ではない)。
    assert a18.reviewed_shas("docs: x\n\nApproved: https://x/1\n") == ((), None)


def test_duplicate_reviewed_is_a_format_problem():
    """**低-7**: 同一行の reviewed 重複は後勝ちにせず様式不備にする。

    後勝ちだと `reviewed=zzz reviewed=<valid>` が不備検出を無言で回避できる。
    """
    msg = f"docs: x\n\nApproved: https://x/1 reviewed=zzz reviewed={'a' * 40}\n"
    shas, problem = a18.reviewed_shas(msg)
    assert shas == () and "複数" in problem


def test_unknown_and_ignored_trailer_keys_are_warned():
    """**低-10**: 綴り誤り・解釈されないキーは黙って v1 扱いにせず警告する。"""
    warnings = a18.trailer_format_warnings(
        f"docs: x\n\nApproved: https://x/1 reviewd={'a' * 40} mode=deemed おまけ\n"
    )
    assert any("未知キー 'reviewd='" in w for w in warnings)
    assert any("'mode=' は A-18 では解釈されない" in w for w in warnings)
    assert any("解釈できない語 'おまけ'" in w for w in warnings)
    clean = f"docs: x\n\nApproved: https://x/1 reviewed={'a' * 40}\n"
    assert a18.trailer_format_warnings(clean) == []


def test_trailer_format_warnings_reach_notes(repo):
    """様式警告は報告 notes に出る(コミット SHA 付き)。"""
    r, since = repo
    sha = _commit(
        r, "docs/protected.md", "v2\n",
        f"docs: 変更\n\nApproved: https://x/1 reviewd={'a' * 40}",
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since, verify_prs=False)
    assert any("未知キー 'reviewd='" in n and sha[:12] in n for n in result["notes"])


def test_reviewed_from_another_branch_is_a_format_problem(repo):
    """**重要-3 PoC**: reviewed が当該 PR のブランチ(第2親)の祖先でなければ起点にしない。

    他ブランチの SHA を書くと「reviewed 限定」と表示したまま実際には何も限定しない偽装に
    なる。帰属を確認できない reviewed は様式不備として fail-safe で扱う。
    """
    r, since = repo
    _git(r, "checkout", "-q", "-b", "other")
    outside = _commit(r, "README.md", "other\n", "docs: 無関係ブランチのコミット")
    _git(r, "checkout", "-q", "main")
    evil, _merge = _merge_pr_with_evil_merge(
        r, f"Merge pull request #9 from k/prfeature\n\n{APPROVED} reviewed={outside}"
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since)
    assert inherited == []
    assert evil[:12] in [v["commit"] for v in violations]
    assert all("第2親" in v["reason"] for v in violations)


# ────────────────────────────────────────────────────────────────────────────
# GitHub PR 実在照合(独立役員審査 2026-08-04 重大-1)
#
# 件名もトレーラ URL も自己申告であり、実在しない PR 番号を書けば承認を装える。承継は
# その偽造1件の爆風半径をブランチ全体へ拡大するため、起点の実在照合が前提条件になる。
# テストは api_get を注入してネットワークに触れない。
# ────────────────────────────────────────────────────────────────────────────
SLUG = "k/y"
MERGED_PR = {"merged_at": "2026-08-04T00:00:00Z"}


def _merged(merge_sha: str) -> dict:
    """マージ済み PR の API 応答(``merge_commit_sha`` = 当該マージ)。"""
    return {"merged_at": "2026-08-04T00:00:00Z", "merge_commit_sha": merge_sha}


def _fake_api(prs: dict[int, dict], *, repo_ok: bool = True, error: str | None = None):
    """``repos/<slug>`` と ``repos/<slug>/pulls/<N>`` に応答する擬似 API。"""
    def api_get(path: str) -> tuple[str, object]:
        if error is not None:
            return "error", error
        if "/pulls/" not in path:
            return ("ok", {"full_name": SLUG}) if repo_ok else ("not_found", None)
        number = int(path.rsplit("/", 1)[1])
        payload = prs.get(number)
        return ("ok", payload) if payload is not None else ("not_found", None)
    return api_get


def _verifier(prs: dict[int, dict], **kwargs) -> a18.PRVerifier:
    return a18.PRVerifier(slug=SLUG, api_get=_fake_api(prs, **kwargs))


def _run_a181_pr(r: Path, since: str | None, verifier: a18.PRVerifier):
    gov = a18.load_governance(r)
    return a18.check_protected_commits(r, gov, since_commit=since, pr_verifier=verifier)


def test_nonexistent_pr_cannot_be_an_inheritance_origin(repo):
    """**重大-1 の PoC**: 実在しない PR 番号の件名+架空 URL のトレーラでは承継しない。"""
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r,
        "Merge pull request #999999 from k/prfeature\n\n"
        f"Approved: https://github.com/{SLUG}/pull/999999",
    )
    violations, inherited, _checked, _findings = _run_a181_pr(r, since, _verifier({}))
    assert inherited == []
    assert evil[:12] in [v["commit"] for v in violations]
    assert any("存在しない" in v["reason"] for v in violations)


def test_existing_pr_still_inherits(repo):
    """実在しマージ済みで **merge_commit_sha が一致する** PR は従来どおり承継の起点になる。"""
    r, since = repo
    evil, merge = _merge_pr_with_evil_merge(
        r,
        f"Merge pull request #9 from k/prfeature\n\nApproved: https://github.com/{SLUG}/pull/9",
    )
    verifier = _verifier({9: _merged(merge)})
    violations, inherited, _checked, _findings = _run_a181_pr(r, since, verifier)
    assert violations == []
    assert [i["commit"] for i in inherited] == [evil[:12]]
    assert verifier.verified_count >= 1 and verifier.failed_open_count == 0


def test_borrowed_pr_number_cannot_be_an_inheritance_origin(repo):
    """**重大-1 PoC(番号流用)**: 実在しマージ済みの PR 番号を件名に流用した自作マージは
    起点にならない。GitHub の merge_commit_sha は別のコミットを指すため帰属が破れる。
    """
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r,
        f"Merge pull request #9 from k/prfeature\n\nApproved: https://github.com/{SLUG}/pull/9",
    )
    # PR #9 は実在しマージ済みだが、そのマージコミットは別物(= 番号を借りただけ)。
    verifier = _verifier({9: _merged("f" * 40)})
    violations, inherited, _checked, _findings = _run_a181_pr(r, since, verifier)
    assert inherited == []
    assert evil[:12] in [v["commit"] for v in violations]
    assert any("流用" in v["reason"] for v in violations)


def test_missing_merge_commit_sha_fails_open(repo):
    """API 応答に merge_commit_sha が無い場合は帰属を主張せず fail-open + 開示。"""
    r, since = repo
    _evil, _merge = _merge_pr_with_evil_merge(
        r,
        f"Merge pull request #9 from k/prfeature\n\nApproved: https://github.com/{SLUG}/pull/9",
    )
    verifier = _verifier({9: MERGED_PR})  # merged_at のみ
    violations, inherited, _checked, _findings = _run_a181_pr(r, since, verifier)
    assert violations == [] and len(inherited) == 1
    assert verifier.failed_open_count >= 1
    assert any("merge_commit_sha" in d for d in verifier.disclosures())


def test_unmerged_pr_is_not_an_inheritance_origin(repo):
    """PR が実在しても未マージなら起点にしない(番号だけ借りた件名を弾く)。"""
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r,
        f"Merge pull request #9 from k/prfeature\n\nApproved: https://github.com/{SLUG}/pull/9",
    )
    verifier = _verifier({9: {"merged_at": None}})
    violations, inherited, _checked, _findings = _run_a181_pr(r, since, verifier)
    assert inherited == []
    assert any("マージされていない" in v["reason"] for v in violations)


def test_nonexistent_pr_url_in_trailer_is_violation(repo):
    """トレーラの PR URL が実在しなければ、DB 接続が無くても承認と見なさない。"""
    r, since = repo
    sha = _commit_with_trailer(r, f"https://github.com/{SLUG}/pull/999999")
    violations, _inherited, _checked, _findings = _run_a181_pr(r, since, _verifier({}))
    assert [v["commit"] for v in violations] == [sha[:12]]
    assert "存在しない" in violations[0]["reason"]


def test_api_error_fails_open_and_is_disclosed(repo):
    """API 不達では従来挙動(件名を信用)へ縮退し、縮退の件数と理由を notes に出す。"""
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r,
        f"Merge pull request #9 from k/prfeature\n\nApproved: https://github.com/{SLUG}/pull/9",
    )
    verifier = _verifier({}, error="GitHub API 不達: URLError")
    result = a18.run_a18(
        r, since_commit=since, pr_since_commit=since, pr_verifier=verifier
    )
    assert result["violations"] == []
    assert [i["commit"] for i in result["inherited"]] == [evil[:12]]
    assert any("fail-open した参照" in n for n in result["notes"])
    # **重要-4**: 縮退した週は緑にしない。embed に「照合不能 N 件(要手動確認)」を出す。
    assert result["pr_verification"]["failed_open"] >= 1
    assert a18.has_findings(result)
    embed = a18.build_alert_embed(result)
    field = next(f for f in embed["fields"] if "PR 実在照合が成立していない" in f["name"])
    assert "要手動確認" in field["value"]
    assert "要対応" in embed["title"]


def test_unreachable_repo_does_not_turn_404_into_violations(repo):
    """私有リポジトリ+認証不備で全件 404 になる状況を「不在」と読まない(fail-open)。

    この防御が無いと、トークンを失った週に全 PR が「実在しない」と判定され、監査が違反を
    大量生成して信用を失う(赤の恒常化 = 05-governance §6-5 の形骸化)。
    """
    r, since = repo
    evil, _merge = _merge_pr_with_evil_merge(
        r,
        f"Merge pull request #9 from k/prfeature\n\nApproved: https://github.com/{SLUG}/pull/9",
    )
    verifier = _verifier({}, repo_ok=False)
    violations, inherited, _checked, _findings = _run_a181_pr(r, since, verifier)
    assert violations == []
    assert [i["commit"] for i in inherited] == [evil[:12]]
    assert any("アクセスできない" in d for d in verifier.disclosures())


def test_a18_4_fake_pr_merge_subject_is_violation(repo):
    """A-18-4 も同じ照合を通す(件名だけの偽 PR マージを非 PR マージとして検出)。"""
    r, since = repo
    _git(r, "checkout", "-q", "-b", "f1")
    _commit(r, "README.md", "a\n", "feat: ブランチ作業")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "f1", "-m", "Merge pull request #999999 from k/f1")
    merge = _git(r, "rev-parse", "HEAD").strip()
    violations, checked = a18.check_direct_pushes(
        r, since_commit=since, pr_verifier=_verifier({})
    )
    assert checked == 1 and len(violations) == 1
    assert "存在しない" in violations[0]["reason"]
    # 実在しマージ SHA も一致すれば従来どおり違反にしない。
    ok, _checked = a18.check_direct_pushes(
        r, since_commit=since, pr_verifier=_verifier({999999: _merged(merge)})
    )
    assert ok == []
    # 番号を流用しただけ(merge_commit_sha が別)なら違反として出る。
    borrowed, _checked = a18.check_direct_pushes(
        r, since_commit=since, pr_verifier=_verifier({999999: _merged("e" * 40)})
    )
    assert len(borrowed) == 1 and "流用" in borrowed[0]["reason"]


def test_a18_4_octopus_merge_is_violation(repo):
    """**中-5**: 実在 PR 件名を付けた octopus マージ(親3)は A-18-4 でも違反。

    GitHub の PR マージは常に親2。件名だけで通すと、複数ブランチの内容を1つの PR 番号で
    main へ入れる経路が全変更 PR 化ルールを素通りする。
    """
    r, since = repo
    _git(r, "checkout", "-q", "-b", "o1")
    _commit(r, "docs/a.md", "a\n", "feat: o1")
    _git(r, "checkout", "-q", "main")
    _git(r, "checkout", "-q", "-b", "o2")
    _commit(r, "docs/b.md", "b\n", "feat: o2")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "o1", "o2", "-m", "Merge pull request #9 from k/octopus")
    merge = _git(r, "rev-parse", "HEAD").strip()
    violations, _checked = a18.check_direct_pushes(
        r, since_commit=since, pr_verifier=_verifier({9: _merged(merge)})
    )
    assert len(violations) == 1 and "octopus" in violations[0]["reason"]


def test_pr_verifier_caches_and_counts():
    """同じ PR 番号は1回しか問い合わせない(週次で API を叩きすぎない)。"""
    calls: list[str] = []

    def api_get(path: str) -> tuple[str, object]:
        calls.append(path)
        return ("ok", _merged("a" * 40)) if "/pulls/" in path else ("ok", {"full_name": SLUG})

    verifier = a18.PRVerifier(slug=SLUG, api_get=api_get)
    assert verifier.check(9) == ("ok", None)
    assert verifier.check(9, "A" * 40) == ("ok", None)  # SHA 帰属は大文字小文字を問わない
    assert calls == [f"repos/{SLUG}", f"repos/{SLUG}/pulls/9"]
    assert any("実在+マージ済み" in d for d in verifier.disclosures())
    # エラーもキャッシュする(レート制限時に同じ番号を叩き直さない — 低-9)。
    err_calls: list[str] = []

    def failing(path: str) -> tuple[str, object]:
        err_calls.append(path)
        return ("ok", {"full_name": SLUG}) if "/pulls/" not in path else ("error", "HTTP 429")

    v2 = a18.PRVerifier(slug=SLUG, api_get=failing)
    assert v2.check(9)[0] == "unverifiable" and v2.check(9)[0] == "unverifiable"
    assert err_calls.count(f"repos/{SLUG}/pulls/9") == 1


def test_pr_verifier_scope_and_disabled_states():
    """他リポジトリの URL・PR でない URL は照合対象外。無効化時は unverifiable。"""
    verifier = _verifier({9: _merged("a" * 40)})
    assert verifier.check_ref("https://github.com/other/repo/pull/9") == ("skip", None)
    assert verifier.check_ref("https://github.com/k/y/issues/9") == ("skip", None)
    assert verifier.check_ref(f"https://github.com/{SLUG}/pull/9") == ("ok", None)
    # **中-6**: 自リポジトリ外の PR URL は「照合していない」ことを開示する(黙って通さない)。
    assert any("自リポジトリ外の PR URL" in d for d in verifier.disclosures())
    assert any("other/repo" in d for d in verifier.disclosures())
    disabled = a18.PRVerifier(slug=SLUG, api_get=_fake_api({}), enabled=False)
    assert disabled.check(9)[0] == "unverifiable"
    # slug が取れない(origin が GitHub でない)ときも fail-open。
    no_slug = a18.PRVerifier(slug=None, api_get=_fake_api({}))
    assert no_slug.check(9)[0] == "unverifiable"
    assert no_slug.check_ref(f"https://github.com/{SLUG}/pull/9") == ("skip", None)


def test_origin_slug_from_remote(repo):
    """origin remote から owner/repo を取り出す(GitHub 以外は None)。"""
    r, _since = repo
    assert a18.origin_slug(r) is None  # remote 未設定
    _git(r, "remote", "add", "origin", "https://github.com/klonyapin/ryza.git")
    assert a18.origin_slug(r) == "klonyapin/ryza"
    _git(r, "remote", "set-url", "origin", "git@github.com:klonyapin/ryza.git")
    assert a18.origin_slug(r) == "klonyapin/ryza"
    _git(r, "remote", "set-url", "origin", "https://example.com/x/y.git")
    assert a18.origin_slug(r) is None


def test_pr_verification_is_enabled_by_default():
    """既定で照合を行う(統制を「既定で無効」に静かに戻せないことの固定)。

    テストは verify_prs=False を明示して API に触れないが、その運用が既定になると本番でも
    照合が止まる。既定値そのものを不変条件として固定する。
    """
    import inspect

    assert inspect.signature(a18.run_a18).parameters["verify_prs"].default is True
    assert inspect.signature(a18.run_and_report).parameters["verify_prs"].default is True


def test_run_a18_without_pr_verification_is_disclosed(repo):
    """照合を止めた実行は「無効化されている」ことを notes に必ず出し、緑にもしない。"""
    r, since = repo
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since, verify_prs=False)
    assert result["prs_verified"] is False
    assert any("GitHub PR 実在照合は無効化されている" in n for n in result["notes"])
    # 緑は「全照合が成立した週」に限る(重要-4・反対意見書③)。
    assert a18.pr_verification_degraded(result) and a18.has_findings(result)
    embed = a18.build_alert_embed(result)
    assert any("PR 実在照合が成立していない" in f["name"] for f in embed["fields"])


def test_notes_are_chunked_not_truncated():
    """注記は 1024 文字上限で切り捨てず複数 field に分割する(開示の無言消失を防ぐ — 低-8)。"""
    notes = [f"注記その{i}: " + "あ" * 80 for i in range(20)]
    chunks = a18._chunk_notes(notes)
    assert len(chunks) > 1
    assert all(len(c) <= 1024 for c in chunks)
    for note in notes:
        assert any(note in c for c in chunks)
    result = _result([], [])
    result["notes"] = notes
    fields = a18.build_alert_embed(result)["fields"]
    names = [f["name"] for f in fields if f["name"].startswith("注記")]
    assert names[0] == "注記" and any("続き" in n for n in names[1:])


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


# ── 受容の承継(supersedes — reminder ack-supersede-mechanism)────────────────
#
# 保護領域を後から足すと違反のパス集合が変わり、完全一致キーの受容が自動的に外れる。
# 受容記録は追記オンリーなので paths の書換もできず、「受容済み evil merge が触れたファイルを
# 含む tree は以後 protected_areas に追加できない」ラチェットになっていた。承継はこれを外す。
# ────────────────────────────────────────────────────────────────────────────
def _ack_entries(r: Path, entries: list[dict]) -> None:
    """一時リポジトリの governance.yaml に受容エントリ列をそのまま書き込む。"""
    gov = yaml.safe_load(GOV_YAML)
    gov["acknowledged_findings"] = entries
    (r / "config" / "governance.yaml").write_text(
        yaml.safe_dump(gov, allow_unicode=True), encoding="utf-8"
    )


def _ack_entry(commit: str, paths: list[str], **extra) -> dict:
    return {
        "commit": commit,
        "paths": paths,
        "reason": "是正不能な歴史的 evil merge(テスト)",
        "approval_ref": "https://github.com/x/y/pull/1",
        "acknowledged_on": "2026-08-04",
        **extra,
    }


def _two_path_violation(r: Path) -> str:
    """2つの保護パス(docs/protected.md・src/prot/ks.py)に触れる無承認コミットを作る。"""
    (r / "docs").mkdir(exist_ok=True)
    (r / "docs" / "protected.md").write_text("v2\n", encoding="utf-8")
    return _commit(r, "src/prot/ks.py", "x = 1\n", "chore: 2つの保護パスに触れる無承認変更")


PATHS_2 = ["docs/protected.md", "src/prot/ks.py"]


def test_supersede_inherits_acknowledgement_and_suppresses_stale_note(repo):
    """拡張された新エントリが旧エントリを承継する: 違反は受容され、陳腐化警告は出ない。"""
    r, since = repo
    sha = _two_path_violation(r)
    # 旧エントリ(保護領域追加の前に登録されたもの)はパス1件のまま残す(追記オンリー)。
    _ack_entries(
        r,
        [
            _ack_entry(sha, ["docs/protected.md"]),
            _ack_entry(
                sha, PATHS_2,
                supersedes={"commit": sha, "paths": ["docs/protected.md"]},
            ),
        ],
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert result["violations"] == []
    assert [a["commit"] for a in result["acknowledged"]] == [sha[:12]]
    # 承継された旧エントリは陳腐化として鳴らさない。
    assert not any("エントリが一致する違反を持たない" in n for n in result["notes"])
    # ただし承継の事実は必ず notes に出る(履歴を残す)。
    note = next(n for n in result["notes"] if n.startswith("受容の承継"))
    assert "src/prot/ks.py" in note and sha[:12] in note


def test_supersede_with_shrinking_paths_is_rejected(repo):
    """パス集合の縮小は承継として認めない(受容の隠蔽と区別できない)。"""
    r, since = repo
    sha = _two_path_violation(r)
    _ack_entries(
        r,
        [
            _ack_entry(sha, PATHS_2),
            # 「src/prot/ks.py への言及だけ落とす」形の差し替え。
            _ack_entry(
                sha, ["docs/protected.md"], supersedes={"commit": sha, "paths": PATHS_2}
            ),
        ],
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    # 旧エントリは有効なままなので違反自体は受容されるが、承継は成立していない。
    assert [a["commit"] for a in result["acknowledged"]] == [sha[:12]]
    assert result["acknowledged"][0]["files"] == PATHS_2
    assert any("supersedes が無効" in n and "拡張になっていない" in n for n in result["notes"])
    assert not any(n.startswith("受容の承継") for n in result["notes"])


def test_rejected_supersede_entry_does_not_acknowledge(repo):
    """不当な承継宣言を持つエントリは受容として効かない(fail-safe: 違反は出たまま)。"""
    r, since = repo
    sha = _two_path_violation(r)
    _ack_entries(
        r,
        [
            # 承継先が存在しない(旧エントリを書かずに承継を主張する)。
            _ack_entry(sha, PATHS_2, supersedes={"commit": sha, "paths": ["migrations/x.sql"]}),
        ],
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [v["commit"] for v in result["violations"]] == [sha[:12]]
    assert result["acknowledged"] == []
    assert any("承継先の受容エントリが" in n for n in result["notes"])


def test_supersede_of_other_commit_is_rejected(repo):
    """別コミットの受容は承継できない(差し替えによる別違反の巻き込みを塞ぐ)。"""
    r, since = repo
    other = _commit(r, "docs/protected.md", "v2\n", "docs: 別の無承認変更")
    sha = _commit(r, "src/prot/ks.py", "x = 1\n", "feat: 承継したい側の無承認変更")
    _ack_entries(
        r,
        [
            _ack_entry(other, ["docs/protected.md"]),
            _ack_entry(
                sha, ["src/prot/ks.py"],
                supersedes={"commit": other, "paths": ["docs/protected.md"]},
            ),
        ],
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [v["commit"] for v in result["violations"]] == [sha[:12]]
    assert any("同一コミットの受容に限る" in n for n in result["notes"])


def test_forward_supersede_is_rejected(repo):
    """承継先は自分より前になければならない(追記オンリーの順序 = 循環の封鎖)。"""
    r, since = repo
    sha = _two_path_violation(r)
    _ack_entries(
        r,
        [
            _ack_entry(sha, PATHS_2, supersedes={"commit": sha, "paths": ["docs/protected.md"]}),
            _ack_entry(sha, ["docs/protected.md"]),  # 承継先が後ろにある
        ],
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert [v["commit"] for v in result["violations"]] == [sha[:12]]
    assert any("承継先の受容エントリが" in n for n in result["notes"])


def test_supersede_without_reason_is_rejected():
    """理由なき差し替えは承継として扱わない。"""
    sha = "a" * 40
    old = _ack_entry(sha, ["CLAUDE.md"])
    new = _ack_entry(sha, ["CLAUDE.md", "config/ips.yaml"],
                     supersedes={"commit": sha, "paths": ["CLAUDE.md"]})
    new["reason"] = "  "
    _index, notes, superseded = a18.acknowledged_index({"acknowledged_findings": [old, new]})
    assert superseded == set()
    assert any("reason が無い" in n for n in notes)


def test_malformed_supersede_declaration_is_disclosed():
    """スカラの supersedes は旧エントリを一意に指せないので無効(黙って落とさない)。"""
    sha = "a" * 40
    entries = [
        _ack_entry(sha, ["CLAUDE.md"]),
        _ack_entry(sha, ["CLAUDE.md", "config/ips.yaml"], supersedes=sha),
    ]
    index, notes, superseded = a18.acknowledged_index({"acknowledged_findings": entries})
    assert superseded == set() and len(index) == 1
    assert any("commit / paths を持つマップで書く" in n for n in notes)


def test_supersede_chain_is_transitive(repo):
    """三世代の承継(A ← B ← C)でも中間・初代とも陳腐化として鳴らない。"""
    r, since = repo
    (r / "docs").mkdir(exist_ok=True)
    (r / "docs" / "protected.md").write_text("v2\n", encoding="utf-8")
    (r / "migrations").mkdir(exist_ok=True)
    (r / "migrations" / "0001_x.sql").write_text("-- x\n", encoding="utf-8")
    sha = _commit(r, "src/prot/ks.py", "x = 1\n", "chore: 3つの保護パスに触れる無承認変更")
    three = ["docs/protected.md", "migrations/0001_x.sql", "src/prot/ks.py"]
    _ack_entries(
        r,
        [
            _ack_entry(sha, ["docs/protected.md"]),
            _ack_entry(sha, PATHS_2, supersedes={"commit": sha, "paths": ["docs/protected.md"]}),
            _ack_entry(sha, three, supersedes={"commit": sha, "paths": PATHS_2}),
        ],
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert result["violations"] == []
    assert result["acknowledged"][0]["files"] == three
    assert not any("エントリが一致する違反を持たない" in n for n in result["notes"])
    assert sum(n.startswith("受容の承継") for n in result["notes"]) == 2


# ── 受容エントリの重複追記・型不備(独立役員審査 2026-08-04 低-1 / 低-3)──────
def test_duplicate_acknowledgement_key_keeps_first_and_discloses(repo):
    """同一キーの重複追記は**後のエントリが無効**。両者の内容を notes に開示する。

    後勝ち上書きを許すと、報告に出る「誰の・どの承認で受容されたか」が追記だけで無開示に
    差し替わる(追記オンリー規則の禁止列挙は削除・書換のみだった)。
    """
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: 無承認変更")
    first = _ack_entry(sha, ["docs/protected.md"])
    first["approval_ref"] = "https://github.com/x/y/pull/1"
    second = _ack_entry(sha, ["docs/protected.md"])
    second["approval_ref"] = "https://github.com/x/y/pull/999"
    second["reason"] = "後から差し替えた理由"
    _ack_entries(r, [first, second])
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    # 受容自体は先のエントリで成立する(違反は鳴らない)。
    assert result["violations"] == []
    assert result["acknowledged"][0]["approval_ref"] == "https://github.com/x/y/pull/1"
    note = next(n for n in result["notes"] if "同一キーの重複エントリ" in n)
    assert "pull/1" in note and "pull/999" in note and "後から差し替えた理由" in note


def test_duplicate_key_does_not_leave_stale_note(repo):
    """重複で無効化した後のエントリは「陳腐化」として二重に鳴らさない(索引に入らないため)。"""
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v2\n", "docs: 無承認変更")
    _ack_entries(r, [_ack_entry(sha, ["docs/protected.md"])] * 2)
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert len(result["acknowledged"]) == 1
    assert not any("一致する違反を持たない" in n for n in result["notes"])


def test_scalar_paths_entry_is_rejected_with_clear_note():
    """paths がスカラ文字列のエントリは明示的に無効(1文字分解のまま通さない)。"""
    sha = "a" * 40
    gov = {"acknowledged_findings": [{"commit": sha, "paths": "CLAUDE.md", "reason": "r"}]}
    index, notes, _superseded = a18.acknowledged_index(gov)
    assert index == {}
    assert any("paths はリストであること" in n for n in notes)


def test_scalar_supersede_paths_is_rejected_with_clear_note():
    """supersedes.paths のスカラ文字列も同様に型として弾く(開示文言を読める形にする)。"""
    sha = "a" * 40
    entries = [
        _ack_entry(sha, ["CLAUDE.md"]),
        _ack_entry(sha, ["CLAUDE.md", "config/ips.yaml"],
                   supersedes={"commit": sha, "paths": "CLAUDE.md"}),
    ]
    _index, notes, superseded = a18.acknowledged_index({"acknowledged_findings": entries})
    assert superseded == set()
    assert any("paths はリストであること" in n for n in notes)


def test_diamond_supersede_is_allowed_and_mismatch_rings(repo):
    """ダイヤモンド承継(2エントリが同一の旧エントリを承継)は許容。隠蔽には使えない。"""
    r, since = repo
    sha = _two_path_violation(r)
    old_paths = ["docs/protected.md"]
    _ack_entries(
        r,
        [
            _ack_entry(sha, old_paths),
            _ack_entry(sha, PATHS_2, supersedes={"commit": sha, "paths": old_paths}),
            # 同じ旧エントリを承継するが、実在の違反には当たらない側。
            _ack_entry(sha, [*old_paths, "migrations/9999_x.sql"],
                       supersedes={"commit": sha, "paths": old_paths}),
        ],
    )
    result = a18.run_a18(r, since_commit=since, pr_since_commit=since)
    assert result["violations"] == []  # 当たる側で受容が成立
    # 当たらない側は陳腐化として鳴る(先回り受容・水増しの封じ込め)。
    assert any("一致する違反を持たない" in n and "9999" in n for n in result["notes"])


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
    """実リポジトリの受容エントリが実在の違反に一致している(陳腐化していない)。

    承継(supersedes)された旧エントリは一致する違反を持たないのが正常なので分母から外す。
    分母を「承継されていないエントリ」に取ることで、承継を口実にした受容の空振り
    (新エントリが実在の違反に当たっていない)は従来どおり落ちる。
    """
    root = Path(__file__).resolve().parents[2]
    # verify_prs=False: テストは GitHub API に触れない(ネットワーク・トークンに依存させない)。
    # PR 実在照合そのものは注入した api_get で下の専用テスト群が検証する。
    result = a18.run_a18(root, verify_prs=False)
    index, _notes, superseded = a18.acknowledged_index(_real_governance())
    live = [k for k in index if k not in superseded]
    assert len(result["acknowledged"]) == len(live)
    assert not any("acknowledged_findings のエントリが一致する違反を持たない" in n
                   for n in result["notes"])


def test_real_repo_execution_engine_is_protected():
    """執行層(発注 → 記帳の唯一の経路・コストモデルの実体)が保護領域である。

    受容ラチェット(supersedes 機構で解消)のために phase-2 が見送った登録。
    ここが外れると config/execution.yaml のコスト率を無視する改変が無審査で通る。
    """
    gov = _real_governance()
    by_path = {str(e["path"]): str(e["area"]) for e in gov["protected_areas"]}
    assert by_path.get("src/ryza/execution/**") == "execution_engine"
    assert by_path.get("config/execution.yaml") == "execution_engine"


def test_real_repo_supersede_is_recorded_not_rewritten():
    """承継は旧エントリを残したまま行われている(追記オンリーの維持)。"""
    entries = _real_governance()["acknowledged_findings"]
    chained = [e for e in entries if e.get("supersedes")]
    assert chained, "承継エントリが消えている(supersedes 機構の導線の確認)"
    keys = {a18._ack_key(str(e["commit"]), e["paths"]) for e in entries}
    for e in chained:
        target = a18._ack_key(str(e["supersedes"]["commit"]), e["supersedes"]["paths"])
        assert target in keys, f"承継先の旧エントリが削除されている: {target[0][:12]}"
        assert set(e["paths"]) > set(e["supersedes"]["paths"])


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
        vetoed_by=OWNER, owner_ids=OWNERS, run_id=run_id, origin="discord_button",
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
    result = a18.run_a18(
        r, since_commit=since, pr_since_commit=since, deemed_since_commit=since, conn=conn
    )
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
    result = a18.run_a18(
        r, since_commit=since, pr_since_commit=since, deemed_since_commit=since, conn=conn
    )
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
    result = a18.run_a18(
        r, since_commit=since, pr_since_commit=since, deemed_since_commit=since, conn=conn
    )
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
    result = a18.run_a18(
        r, since_commit=since, pr_since_commit=since, deemed_since_commit=since, conn=conn
    )
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
    with_conn = a18.run_a18(
        r, since_commit=since, pr_since_commit=since, deemed_since_commit=since, conn=conn
    )
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


# ────────────────────────────────────────────────────────────────────────────
# A-18-7 保護領域 PR の承認記録漏れ(--deemed の叩き忘れ検出)
#
# みなし承認の発効通知は人が CLI を叩くことでしか出ない(自動起票は未実装 — 審査 中-7)。
# 叩き忘れると保護領域の変更が #承認 への通知なしにマージされる。A-18-5 は「記録はあるが
# 未配送」を見るので、**記録そのものが無い**この経路はどの検査にも掛かっていなかった。
# ────────────────────────────────────────────────────────────────────────────
#: A-18-7 テストの「自リポジトリ」。承認記録の帰属は自リポの PR URL 完全一致で判定する。
SELF_SLUG = "klonyapin/ryza"


def _self_pr_url(pr_no: int) -> str:
    return f"https://github.com/{SELF_SLUG}/pull/{pr_no}"


def _merge_protected_pr(r: Path, pr_no: int, *, trailer: str | None = None) -> str:
    """保護ファイルに触れるブランチを PR マージで main に取り込み、マージ sha を返す。"""
    branch = f"prot{pr_no}"
    _git(r, "checkout", "-q", "-b", branch)
    _commit(r, "docs/protected.md", f"pr-{pr_no}\n", f"docs: PR #{pr_no} の保護領域変更")
    _git(r, "checkout", "-q", "main")
    message = f"Merge pull request #{pr_no} from k/{branch}"
    if trailer:
        message += f"\n\nApproved: {trailer}"
    _git(r, "merge", "--no-ff", "-q", branch, "-m", message)
    return _git(r, "rev-parse", "HEAD").strip()


def _scan_a187(r: Path, since: str, conn, *, slug: str | None = SELF_SLUG):
    """A-18-7 の走査結果(findings / checked / repo_slug)。

    ``slug`` を明示するのは、一時リポジトリに ``origin`` を足すと :class:`a18.PRVerifier` が
    実 API を叩きうるためである(テストはネットワークに触れない)。既定は自リポ相当。
    """
    return a18.check_unrecorded_protected_prs(
        r, a18.load_governance(r), conn, since_commit=since, repo_slug=slug
    )


def _run_a187(r: Path, since: str, conn, *, slug: str | None = SELF_SLUG):
    return _scan_a187(r, since, conn, slug=slug).findings


def _run_a18_deemed(r: Path, since: str, conn=None):
    """A-18-7 込みの run_a18(PR 実在照合は無効 — ネットワークに触れない)。"""
    return a18.run_a18(
        r, since_commit=since, pr_since_commit=since, deemed_since_commit=since,
        conn=conn, verify_prs=False,
    )


def test_pr_number_from_subject_parses_only_pr_merges():
    """A-18-7 は A-18-1/4 と同じ件名解析を使う(番号の解釈を二重定義しない)。"""
    assert a18.pr_number_from_subject("Merge pull request #82 from k/x") == 82
    assert a18.pr_number_from_subject("merge: ブランチ統合") is None
    assert a18.pr_number_from_subject("docs: PR #82 について") is None


def test_a18_7_protected_pr_without_any_record_is_a_finding(repo, conn):
    """トレーラも承認記録も無い保護領域 PR = 発効通知が出ていない疑い。"""
    r, since = repo
    merge = _merge_protected_pr(r, 501)
    findings = _run_a187(r, since, conn)
    assert [f["merge"] for f in findings] == [merge[:12]]
    assert findings[0]["pr_number"] == 501
    assert findings[0]["files"] == ["docs/protected.md"]
    assert findings[0]["expected_ref"] == _self_pr_url(501)
    assert "当該 PR を指す承認記録も無い" in findings[0]["reason"]
    conn.rollback()


def test_a18_7_deemed_record_for_the_pr_url_clears_it(repo, conn, run_id):
    """PR URL で記録された deemed があれば、トレーラが無くても記録漏れではない。"""
    r, since = repo
    _merge_protected_pr(r, 502)
    assert len(_run_a187(r, since, conn)) == 1
    _deemed(conn, run_id, _self_pr_url(502))
    assert _run_a187(r, since, conn) == []
    conn.rollback()


def test_a18_7_pr_number_match_is_anchored(repo, conn, run_id):
    """``/pull/50`` の記録は PR #5 を救済しない(末尾一致で誤一致させない)。"""
    r, since = repo
    _merge_protected_pr(r, 5)
    _deemed(conn, run_id, _self_pr_url(50))
    assert [f["pr_number"] for f in _run_a187(r, since, conn)] == [5]
    conn.rollback()


# ── 帰属の検査(後続配線審査 後-3・後-5)──────────────────────────────────────
def test_a18_7_trailer_copied_from_another_pr_does_not_clear_it(repo, conn, run_id):
    """**後-3 の実証ケース**: #601 の記録を #602 にトレーラ複写しても #602 は緑にならない。

    「参照先の決定が実在するか」だけを見ると、追い PR にトレーラを複写しただけで所見が
    消える。検査の意味は「承認記録がある」ではなく「**この変更の**承認記録がある」。
    """
    r, since = repo
    _deemed(conn, run_id, _self_pr_url(601))
    _merge_protected_pr(r, 601, trailer=_self_pr_url(601))
    _merge_protected_pr(r, 602, trailer=_self_pr_url(601))  # 同じトレーラを複写した追い PR
    findings = _run_a187(r, since, conn)
    assert [f["pr_number"] for f in findings] == [602]
    assert "別提案の承認記録を指している" in findings[0]["reason"]
    conn.rollback()


def test_a18_7_non_pr_proposal_ref_is_not_attribution(repo, conn, run_id):
    """PR URL 以外(IPS 改訂など)の記録は、その PR に帰属する承認記録ではない。

    トレーラから引ける決定が実在しても、``proposal_ref`` がこの PR を指していなければ
    「この PR の発効通知が出た」証跡にはならない。理由に参照先を出して切り分けられるようにする。
    """
    r, since = repo
    _deemed(conn, run_id, "ips-2026-09-revision")
    _merge_protected_pr(r, 503, trailer="ips-2026-09-revision")
    findings = _run_a187(r, since, conn)
    assert [f["pr_number"] for f in findings] == [503]
    assert "ips-2026-09-revision" in findings[0]["reason"]
    conn.rollback()


def test_a18_7_other_repository_record_does_not_clear_it(repo, conn, run_id):
    """**後-5 の実証ケース**: 他リポの ``/pull/610`` の記録が自リポ #610 を救済しない。"""
    r, since = repo
    _deemed(conn, run_id, "https://github.com/other/repo/pull/610")
    _merge_protected_pr(r, 610)
    findings = _run_a187(r, since, conn)
    assert [f["pr_number"] for f in findings] == [610]
    assert "別リポジトリの記録" in findings[0]["reason"]
    # 自リポの記録を足せば緑になる(検査が「帰属」だけを見ていることの対照)。
    _deemed(conn, run_id, _self_pr_url(610))
    assert _run_a187(r, since, conn) == []
    conn.rollback()


def test_a18_7_without_slug_falls_back_to_suffix_and_is_disclosed(repo, conn, run_id):
    """origin を解決できない実行は末尾一致まで。緑にする代わりに未照合を開示する。"""
    r, since = repo
    _deemed(conn, run_id, "https://github.com/other/repo/pull/611")
    _merge_protected_pr(r, 611)
    scan = _scan_a187(r, since, conn, slug=None)
    assert scan.findings == []  # 末尾一致で救済されてしまう(だから開示が要る)
    assert scan.repo_slug is None
    conn.rollback()


def test_a18_7_trailer_pointing_nowhere_is_a_finding(repo, conn):
    """トレーラはあるが参照先の記録が無い = CLI を叩かずにトレーラだけ書いた状態。"""
    r, since = repo
    _merge_protected_pr(r, 504, trailer="https://github.com/x/y/pull/999")
    findings = _run_a187(r, since, conn)
    assert len(findings) == 1
    assert findings[0]["trailer_refs"] == ["https://github.com/x/y/pull/999"]
    assert "対応する承認記録が無い" in findings[0]["reason"]
    conn.rollback()


def test_a18_7_bare_number_trailer_does_not_clear_it(repo, conn, run_id):
    """裸の数字は決定 ID として解釈しない(重要-2)ので記録漏れのまま残る。"""
    r, since = repo
    decision_id = _deemed(conn, run_id, _self_pr_url(520))
    _merge_protected_pr(r, 521, trailer=str(decision_id))
    assert [f["pr_number"] for f in _run_a187(r, since, conn)] == [521]
    conn.rollback()


def test_a18_7_decision_id_trailer_clears_it_when_it_points_at_this_pr(repo, conn, run_id):
    """``decision:<id>`` でも、その決定の proposal_ref が当該 PR なら帰属と認める。"""
    r, since = repo
    decision_id = _deemed(conn, run_id, _self_pr_url(522))
    _merge_protected_pr(r, 522, trailer=f"decision:{decision_id}")
    assert _run_a187(r, since, conn) == []
    conn.rollback()


def test_a18_7_vetoed_record_is_not_a_record_gap(repo, conn, run_id):
    """否認済みでも「記録はある」。取消義務の指摘は A-18-1 の担当で、ここでは鳴らさない。"""
    r, since = repo
    url = _self_pr_url(507)
    _deemed(conn, run_id, url)
    _veto(conn, run_id, url)
    _merge_protected_pr(r, 507)
    assert _run_a187(r, since, conn) == []
    conn.rollback()


def test_a18_7_ignores_prs_that_do_not_touch_protected_areas(repo, conn):
    r, since = repo
    _merge_pr(r, "docsonly", "README.md", "x\n", 508)
    scan = _scan_a187(r, since, conn)
    assert scan.findings == [] and scan.checked == 0  # 分母にも数えない
    conn.rollback()


def test_a18_7_ignores_direct_pushes_and_non_pr_merges(repo, conn):
    """直 push・非 PR マージは A-18-4 の担当(A-18-7 は PR マージだけを見る)。"""
    r, since = repo
    _commit(r, "docs/protected.md", "direct\n", "docs: 直 push")
    _git(r, "checkout", "-q", "-b", "sidebranch")
    _commit(r, "docs/protected.md", "side\n", "docs: ブランチ側")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-q", "sidebranch", "-m", "merge: 非 PR マージ")
    assert _run_a187(r, since, conn) == []
    conn.rollback()


def test_a18_7_unknown_baseline_raises(repo, conn):
    r, _since = repo
    with pytest.raises(ValueError, match="基準コミット"):
        _run_a187(r, "0" * 40, conn)
    conn.rollback()


def test_a18_7_reaches_result_and_report(repo, conn):
    """結果 dict・警告 embed・報告要否(has_findings)まで通る。"""
    r, since = repo
    _merge_protected_pr(r, 509)
    result = _run_a18_deemed(r, since, conn)
    assert [f["pr_number"] for f in result["unrecorded_prs"]] == [509]
    assert result["checked_protected_prs"] == 1
    assert a18.has_findings(result) is True
    field = next(f for f in a18.build_alert_embed(result)["fields"] if "A-18-7" in f["name"])
    assert "⚠️" in field["name"] and "PR #509" in field["value"]
    assert "1/1 件" in field["name"]  # 分母つきで出す
    conn.rollback()


# ── 緑の分母(後続配線審査 後-4)───────────────────────────────────────────────
def test_a18_7_green_line_carries_the_denominator(repo, conn, run_id):
    """記録漏れ 0 の緑には**検査対象数**を書く(「漏れが無い」と「見ていない」の区別)。"""
    r, since = repo
    _merge_protected_pr(r, 510)
    _deemed(conn, run_id, _self_pr_url(510))
    result = _run_a18_deemed(r, since, conn)
    assert result["unrecorded_prs"] == [] and result["checked_protected_prs"] == 1
    field = next(f for f in a18.build_alert_embed(result)["fields"] if "A-18-7" in f["name"])
    assert "⚠️" not in field["name"]
    assert "✅ 記録漏れなし(検査対象 1 件)" in field["value"]
    conn.rollback()


def test_a18_7_zero_target_is_stated_explicitly(repo, conn):
    """対象 0 件は ✅ にせず「対象 PR なし」と書く(squash 移行時の沈黙を防ぐ)。"""
    r, since = repo
    _merge_pr(r, "unprotected", "README.md", "x\n", 512)  # 保護領域に触れない PR のみ
    result = _run_a18_deemed(r, since, conn)
    assert result["checked_protected_prs"] == 0
    field = next(f for f in a18.build_alert_embed(result)["fields"] if "A-18-7" in f["name"])
    assert "対象 PR なし" in field["value"] and "✅" not in field["value"]
    assert "squash" in field["value"]
    conn.rollback()


def test_a18_7_is_skipped_and_disclosed_without_conn(repo):
    """DB 接続なしでは照合できない — 黙って ✅ にせず、未照合を注記に出す。"""
    r, since = repo
    _merge_protected_pr(r, 511)
    result = _run_a18_deemed(r, since)
    assert result["unrecorded_prs"] == [] and result["checked_protected_prs"] == 0
    assert any("A-18-7" in n for n in result["notes"])
    assert all("A-18-7" not in f["name"] for f in a18.build_alert_embed(result)["fields"])


def test_a18_7_unresolvable_slug_is_disclosed_in_notes(repo, conn):
    """一時リポジトリ(origin なし)では帰属照合が末尾一致に落ちることを注記に出す。"""
    r, since = repo
    _merge_protected_pr(r, 513)
    result = _run_a18_deemed(r, since, conn)  # repo に origin remote は無い
    assert result["deemed_repo_slug"] is None
    assert any("後-5" in n or "末尾一致" in n for n in result["notes"])
    conn.rollback()


# ────────────────────────────────────────────────────────────────────────────
# 接続の分離(独立役員審査 軽微-11)
#
# 照合を報告投入と同じトランザクションで行うと、git 走査(履歴の長さに比例)の間ずっと
# idle-in-transaction のセッションが残る。検査は autocommit・read-only の別接続で完結させ、
# 閉じてから書込接続を開く。
# ────────────────────────────────────────────────────────────────────────────
def test_run_and_report_verifies_on_a_separate_readonly_connection(repo, migrated_db, monkeypatch):
    import ryza.db.conn as db_conn

    r, since = repo
    real_connect = db_conn.connect
    opened: list = []

    def spy_connect(autocommit: bool = False):
        c = real_connect(autocommit=autocommit)
        opened.append(c)
        return c

    # a18 は connect を関数内で import するため、モジュール属性の差し替えが効く。
    # provenance.runs は先頭で import しており、Run 自身の接続は素通しになる。
    monkeypatch.setattr(db_conn, "connect", spy_connect)

    seen: dict = {}
    real_run_a18 = a18.run_a18

    def spy_run_a18(repo_path, **kwargs):
        conn = kwargs["conn"]
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            seen["read_only"] = cur.fetchone()[0]
        seen["autocommit"] = conn.autocommit
        seen["verify_conn"] = conn
        return real_run_a18(repo_path, **kwargs)

    monkeypatch.setattr(a18, "run_a18", spy_run_a18)

    report: dict = {}

    def spy_enqueue_alert(conn, result, run_id, **kwargs):
        report["write_autocommit"] = conn.autocommit
        report["verify_closed"] = seen["verify_conn"].closed
        report["same_conn"] = conn is seen["verify_conn"]
        report["run_id"] = run_id  # 後始末で消す行を特定する(他セッションの行に触れない)
        return 0

    monkeypatch.setattr(a18, "enqueue_alert", spy_enqueue_alert)

    try:
        a18.run_and_report(
            r, since_commit=since, pr_since_commit=since, deemed_since_commit=since,
            always_report=True,
        )
        # 照合接続: autocommit(= 文ごとに完結)かつ read-only(監査の read-only 原則)。
        assert seen["autocommit"] is True
        assert seen["read_only"] == "on"
        # 報告時点で照合接続は既に閉じている = git 走査中の idle-in-transaction が無い。
        assert report["verify_closed"] is True
        assert report["same_conn"] is False
        assert report["write_autocommit"] is False
        assert len(opened) == 2  # 照合用と書込用の2本だけ
    finally:
        # run_and_report の Run は自前接続(autocommit)で確定するので消しておく。
        if report.get("run_id") is not None:
            with real_connect() as post, post.cursor() as cur:
                cur.execute("DELETE FROM meta.runs WHERE run_id = %s", (report["run_id"],))
                post.commit()


def test_run_a18_readonly_refuses_writes(repo, migrated_db, monkeypatch):
    """照合接続への書込は静かに通らず失敗する(**うっかり書込の検出点** — 後-8)。

    ``default_transaction_read_only`` はセッション既定であって権限境界ではない
    (``SET TRANSACTION READ WRITE`` で上書きでき、ロールの書込権限も残る)。ここで
    確かめるのは「意図しない書込が黙って通らない」ことであり、悪意ある書込の阻止ではない。
    """
    import psycopg

    r, since = repo

    def writing_run_a18(repo_path, **kwargs):
        with kwargs["conn"].cursor() as cur:
            cur.execute(
                "INSERT INTO press.outbox (channel, embed_json, urgent, run_id) "
                "VALUES ('ops', '{}', false, 1)"
            )
        return {}

    monkeypatch.setattr(a18, "run_a18", writing_run_a18)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        a18.run_a18_readonly(r, since_commit=since, pr_since_commit=since)


# ────────────────────────────────────────────────────────────────────────────
# A-18-8 審査対象 SHA の突合(トレーラの reviewed= ⇔ 承認記録の reviewed_sha)
#
# reviewed= は書き手の申告でしかなく、0029 以前は照合先が存在しなかった(審査 重要-3)。
# 本検査が捕まえるのは**片側だけの改変**である。両方に同じ嘘を書けば一致する点は
# docstring・notes で開示しており、テストでもその意味の限界を固定する。
# ────────────────────────────────────────────────────────────────────────────
REVIEWED_A = "1" * 40
REVIEWED_B = "2" * 40


def _deemed_reviewed(conn, run_id, proposal_ref: str, reviewed_sha: str | None) -> int:
    """審査対象 SHA つきのみなし承認を1件記録し decision id を返す。"""
    from ryza.governance import notices

    return notices.announce_deemed_approval(
        conn, proposal_ref, "pr", "保護領域の変更", run_id,
        reviewed_sha=reviewed_sha, review_ref="docs/reviews/x-review.md",
    ).decision.id


def _scan_a188(r: Path, since: str, conn):
    return a18.check_reviewed_sha_agreement(r, a18.load_governance(r), conn, since_commit=since)


def _commit_reviewed_trailer(r: Path, ref: str, reviewed: str) -> str:
    return _commit(
        r, "docs/protected.md", f"v-{reviewed[:6]}\n",
        f"docs: 保護領域変更\n\nApproved: {ref} reviewed={reviewed}",
    )


def test_a18_8_matching_reviewed_sha_is_clean(repo, conn, run_id):
    """2経路の申告が一致すれば所見なし。分母(突合できた件数)は必ず数える。"""
    r, since = repo
    url = "https://github.com/x/y/pull/801"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.findings == [] and scan.compared == 1 and scan.trailer_only == 0
    conn.rollback()


def test_a18_8_mismatched_reviewed_sha_is_a_finding(repo, conn, run_id):
    """**本検査の実証ケース**: トレーラだけ別 SHA に差し替えると不一致で出る。"""
    r, since = repo
    url = "https://github.com/x/y/pull/802"
    decision_id = _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_reviewed_trailer(r, url, REVIEWED_B)
    scan = _scan_a188(r, since, conn)
    assert len(scan.findings) == 1 and scan.compared == 1
    finding = scan.findings[0]
    assert finding["ref"] == url and finding["decision_id"] == decision_id
    assert finding["trailer_reviewed"] == REVIEWED_B
    assert finding["recorded_reviewed"] == REVIEWED_A
    assert "一致しない" in finding["reason"]
    conn.rollback()


def test_a18_8_decision_id_reference_is_also_compared(repo, conn, run_id):
    """``decision:<id>`` 形式の参照でも突合する(参照の書き方で検査が抜けない)。"""
    r, since = repo
    decision_id = _deemed_reviewed(conn, run_id, "https://github.com/x/y/pull/803", REVIEWED_A)
    _commit_reviewed_trailer(r, f"decision:{decision_id}", REVIEWED_B)
    assert len(_scan_a188(r, since, conn).findings) == 1
    conn.rollback()


def test_a18_8_case_difference_is_not_a_mismatch(repo, conn, run_id):
    """表記揺れ(大文字)で不一致を誤検出しない — 両側とも小文字へ正規化して比べる。"""
    r, since = repo
    url = "https://github.com/x/y/pull/804"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_reviewed_trailer(r, url, REVIEWED_A.upper())
    scan = _scan_a188(r, since, conn)
    assert scan.findings == [] and scan.compared == 1
    conn.rollback()


def test_a18_8_record_without_reviewed_sha_is_disclosed_not_alerted(repo, conn, run_id):
    """記録側が NULL(0029 以前・別経路の発効)は所見にせず件数で開示する。

    移行期に全件鳴らすと本物の不一致が埋もれる。ただし沈黙もさせない —— 「突合が働いて
    いない記録が何件あるか」は緑の意味を左右するため notes に出す。
    """
    r, since = repo
    url = "https://github.com/x/y/pull/805"
    _deemed_reviewed(conn, run_id, url, None)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.findings == [] and scan.compared == 0 and scan.trailer_only == 1
    result = _run_a18_deemed(r, since, conn)
    assert any("reviewed_sha が無い決定 1 件" in n for n in result["notes"])
    conn.rollback()


def test_a18_8_v1_trailer_is_out_of_scope(repo, conn, run_id):
    """様式 v1(reviewed 無し)は本検査の対象外(承継範囲の問題は A-18-1 の担当)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/806"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_with_trailer(r, url)
    scan = _scan_a188(r, since, conn)
    assert scan.findings == [] and scan.compared == 0 and scan.trailer_only == 0
    conn.rollback()


def test_a18_8_unresolvable_reference_is_skipped(repo, conn):
    """参照が解決できないこと自体は A-18-1/7 の担当(ここで二重に鳴らさない)。"""
    r, since = repo
    _commit_reviewed_trailer(r, "decision:999999999", REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.findings == [] and scan.compared == 0
    conn.rollback()


def test_a18_8_vetoed_decision_is_still_compared(repo, conn, run_id):
    """否認済みでも突合する — 本検査が見るのは申告の一致であって決定の有効性ではない。"""
    r, since = repo
    url = "https://github.com/x/y/pull/807"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _veto(conn, run_id, url)
    _commit_reviewed_trailer(r, url, REVIEWED_B)
    assert len(_scan_a188(r, since, conn).findings) == 1
    conn.rollback()


def test_a18_8_unknown_baseline_raises(repo, conn):
    r, _since = repo
    with pytest.raises(ValueError, match="基準コミット"):
        _scan_a188(r, "0" * 40, conn)
    conn.rollback()


def test_a18_8_mismatch_reaches_result_and_report(repo, conn, run_id):
    """不一致は run_a18 の結果・所見判定・報告 embed まで到達する。"""
    r, since = repo
    url = "https://github.com/x/y/pull/808"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_reviewed_trailer(r, url, REVIEWED_B)
    result = _run_a18_deemed(r, since, conn)
    assert len(result["reviewed_sha_mismatches"]) == 1
    assert result["compared_reviewed_shas"] == 1
    assert a18.has_findings(result)
    embed = a18.build_alert_embed(result)
    field = next(f for f in embed["fields"] if "A-18-8" in f["name"])
    # 分母は**決定**単位で表示する(トレーラ行数ではない — SHA-5)。
    assert "⚠️" in field["name"] and "1/1 決定" in field["name"]
    conn.rollback()


def test_a18_8_green_line_carries_the_denominator(repo, conn, run_id):
    """緑には必ず分母を書く(移行期の「不一致 0」は「まだ突合していない」ことが多い)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/809"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    embed = a18.build_alert_embed(_run_a18_deemed(r, since, conn))
    field = next(f for f in embed["fields"] if "A-18-8" in f["name"])
    assert "突合できた決定 1 件" in field["value"] and "⚠️" not in field["name"]
    conn.rollback()


def test_a18_8_zero_target_is_stated_explicitly(repo, conn):
    """突合 0 件を「一致の確認」と読ませない(沈黙で緑にしない)。"""
    r, since = repo
    embed = a18.build_alert_embed(_run_a18_deemed(r, since, conn))
    field = next(f for f in embed["fields"] if "A-18-8" in f["name"])
    assert "突合対象なし" in field["value"] and "一致の確認ではない" in field["value"]
    conn.rollback()


def test_a18_8_is_skipped_and_disclosed_without_conn(repo):
    """DB 接続なしの実行では突合できない —— その事実を notes に出す。"""
    r, since = repo
    result = _run_a18_deemed(r, since)
    assert result["reviewed_sha_mismatches"] == []
    assert any("A-18-8" in n for n in result["notes"])


def test_a18_8_limitation_is_always_disclosed(repo):
    """「同じ値を両方に書けば一致する」限界は毎回開示する(強い保証に見せない)。"""
    r, since = repo
    result = _run_a18_deemed(r, since)
    assert any("審査エージェント自身の署名は無い" in n for n in result["notes"])


# ── SHA-5: 集計は決定単位(トレーラ行数で水増ししない)────────────────────────
def test_a18_8_counts_are_per_decision_not_per_trailer(repo, conn, run_id):
    """同じ決定を参照するコミットが N 個あっても分母は 1・所見も 1 件にまとまる。

    A-18-8 は A-18-1 と違い全コミットの本文を読むため、行数で数えると同じ事実が N 回
    数えられ、緑の「突合できた決定 N 件」も将来判断の材料も水増しされる(審査 SHA-5)。
    """
    r, since = repo
    url = "https://github.com/x/y/pull/810"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_reviewed_trailer(r, url, REVIEWED_B)
    _commit(
        r, "docs/protected.md", "again\n",
        f"docs: 同じ決定を参照する2つ目のコミット\n\nApproved: {url} reviewed={REVIEWED_B}",
    )
    scan = _scan_a188(r, since, conn)
    assert scan.compared == 1
    assert len(scan.findings) == 1
    assert len(scan.findings[0]["commits"]) == 2  # どのコミットで起きたかは全部残す
    conn.rollback()


def test_a18_8_different_declared_shas_are_separate_findings(repo, conn, run_id):
    """同一決定に別々の SHA を申告するコミットは別の所見(食い違いの種類が違う)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/811"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_reviewed_trailer(r, url, REVIEWED_B)
    _commit(
        r, "docs/protected.md", "third\n",
        f"docs: 別の SHA を申告\n\nApproved: {url} reviewed={'3' * 40}",
    )
    assert len(_scan_a188(r, since, conn).findings) == 2
    conn.rollback()


# ── SHA-2: 記録側にあるがトレーラ v1(reviewed= を落とすと無音になる経路)──────
def test_a18_8_record_only_is_counted_and_disclosed(repo, conn, run_id):
    """**SHA-2 の実証ケース**: `reviewed=` を落としても無音にならない。

    CLI が head SHA を自動格納する以上、今後の記録側はほぼ常に埋まる。攻撃でなく横着で
    トレーラから reviewed= を落とすだけで、承継は無制限のまま突合も働かなくなる。
    旧実装は declared が無い行を早期 continue しており、この側が計測すらされなかった。
    """
    r, since = repo
    url = "https://github.com/x/y/pull/812"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_with_trailer(r, url)  # 様式 v1(reviewed= なし)
    scan = _scan_a188(r, since, conn)
    assert scan.record_only == 1 and scan.compared == 0 and scan.trailer_only == 0
    result = _run_a18_deemed(r, since, conn)
    assert result["record_only_reviewed"] == 1
    assert any("トレーラが様式 v1 の決定 1 件" in n for n in result["notes"])
    conn.rollback()


def test_a18_8_record_only_without_record_sha_is_not_counted(repo, conn, run_id):
    """記録側も NULL なら record_only ではない(v1 のままの決定を鳴らさない)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/813"
    _deemed_reviewed(conn, run_id, url, None)
    _commit_with_trailer(r, url)
    scan = _scan_a188(r, since, conn)
    assert scan.record_only == 0 and scan.trailer_only == 0 and scan.compared == 0
    conn.rollback()


# ── SHA-1: 不一致時の承継範囲は記録側(発効時点で固定)を採用する ──────────────
#
# 既存の reviewed 承継テストと同じ構成(ブランチ内 evil merge を PR マージで取り込む)を使う。
# 承継の対象になるのは「PR マージが持ち込んだブランチ内マージ」であり、素のブランチ内
# コミットは附則(b)で承認されて inherited には現れない — 検査対象を既存の流儀に揃える。
def _pr_with_recorded_sha(
    r: Path,
    conn,
    run_id,
    pr_url: str,
    *,
    recorded: Callable[[str], str | None],
    declared: Callable[[str], str],
) -> str:
    """evil merge つき PR を作り、承認記録の reviewed_sha を ``recorded`` で決める。

    ``recorded`` / ``declared`` は evil merge の sha を受け取り、それぞれ記録側・トレーラ側に
    入れる SHA を返す。戻り値は evil merge の sha。
    """
    evil, _merge = _merge_pr_with_evil_merge(
        r, lambda ev: f"Merge pull request #9 from k/prfeature\n\nApproved: {pr_url} "
                      f"reviewed={declared(ev)}",
    )
    _deemed_reviewed(conn, run_id, pr_url, recorded(evil))
    return evil


def test_a18_1_inheritance_uses_the_recorded_sha_on_mismatch(repo, conn, run_id):
    """**SHA-1 の実証ケース**: トレーラが head を指しても、記録側 SHA より後は承継しない。

    記録側 ``reviewed_sha`` は発効通知の時点に固定され追記オンリーで改変困難であるのに対し、
    トレーラはマージ時に書ける。両方あって食い違うなら記録側が「48h の異議期間が実際に
    係属した内容」であり、承継はそこまでに縮める(通知後に積んだ変更は違反として現れる)。
    """
    r, since = repo
    url = _self_pr_url(821)
    # 記録側は evil merge の**親**(= 通知時点)、トレーラはブランチ head(= evil merge)。
    evil = _pr_with_recorded_sha(
        r, conn, run_id, url,
        recorded=lambda ev: _git(r, "rev-parse", ev + "^1").strip(),
        declared=lambda ev: ev,
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since, conn)
    assert [v["commit"] for v in violations] == [evil[:12]]
    assert "承認記録の reviewed_sha" in violations[0]["reason"]
    assert inherited == []
    conn.rollback()


def test_a18_1_matching_shas_inherit_as_before(repo, conn, run_id):
    """一致していれば従来どおり承継する(記録側採用は不一致のときだけ働く)。"""
    r, since = repo
    url = _self_pr_url(822)
    evil = _pr_with_recorded_sha(
        r, conn, run_id, url, recorded=lambda ev: ev, declared=lambda ev: ev
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since, conn)
    assert violations == []
    assert [i["commit"] for i in inherited] == [evil[:12]]
    assert inherited[0]["reviewed_from_record"] is False
    conn.rollback()


def test_a18_1_record_side_can_widen_the_scope_and_is_marked(repo, conn, run_id):
    """記録側が head を指しトレーラが手前を指す場合も**記録側**を採る(常に記録側が正)。

    「縮む方向だけ採用する」ようにすると、どちらを正とするかが所見の向きで変わる恣意的な
    規則になる。記録側が発効時点で固定されているという理由は方向に依らないので、
    採用規則も方向に依らせない。承継した事実には記録側由来の印を残す。
    """
    r, since = repo
    url = _self_pr_url(823)
    evil = _pr_with_recorded_sha(
        r, conn, run_id, url,
        recorded=lambda ev: ev,
        declared=lambda ev: _git(r, "rev-parse", ev + "^1").strip(),
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since, conn)
    assert violations == []
    assert [i["commit"] for i in inherited] == [evil[:12]]
    assert inherited[0]["reviewed_from_record"] is True
    conn.rollback()


def test_a18_1_record_side_override_is_disclosed_in_notes(repo, conn, run_id):
    """承継範囲を記録側で決めた事実は注記に出す(黙って範囲を変えない)。"""
    r, since = repo
    url = _self_pr_url(824)
    _pr_with_recorded_sha(
        r, conn, run_id, url,
        recorded=lambda ev: _git(r, "rev-parse", ev + "^1").strip(),
        declared=lambda ev: ev,
    )
    result = _run_a18_deemed(r, since, conn)
    assert any("承認記録側の reviewed_sha" in n for n in result["notes"])
    conn.rollback()


def test_a18_1_trailer_only_keeps_the_declared_scope(repo, conn, run_id):
    """記録側が NULL なら従来どおりトレーラの値で範囲を決める(移行期を壊さない)。"""
    r, since = repo
    url = _self_pr_url(825)
    evil = _pr_with_recorded_sha(
        r, conn, run_id, url, recorded=lambda _ev: None, declared=lambda ev: ev
    )
    violations, inherited, _checked, _findings = _run_a181_full(r, since, conn)
    assert violations == []
    assert [i["commit"] for i in inherited] == [evil[:12]]
    conn.rollback()


# ── SHA-3: A-18-8 の不一致に解消経路(acknowledged_findings kind: a18-8)──────
def _ack_reviewed_gov(r: Path, commit: str, ref: str, declared: str) -> dict:
    """一時リポジトリの governance.yaml に A-18-8 の受容エントリを足した dict を返す。"""
    gov = a18.load_governance(r)
    gov["acknowledged_findings"] = [
        {
            "kind": "a18-8",
            "commit": commit,
            "ref": ref,
            "trailer_reviewed": declared,
            "reason": "手入力の打ち間違い。記録は追記オンリーで訂正できない",
            "approval_ref": "https://github.com/klonyapin/ryza/pull/999",
            "acknowledged_on": "2026-08-04",
        }
    ]
    return gov


def _only_reviewed_findings(result: dict) -> bool:
    """A-18-8 **だけ**が所見判定に効いているかを見る(他の検査の信号を落として評価する)。

    一時リポジトリの実行は直 push(A-18-4)と PR 実在照合の無効化で常に所見ありになるため、
    ``has_findings`` をそのまま見ても A-18-8 の寄与を判定できない。
    """
    masked = {
        **result,
        "violations": [], "mismatches": [], "direct_pushes": [],
        "unnotified_deemed": [], "unrecorded_prs": [], "trailer_findings": [],
        "resolution_bypass": None, "prs_verified": True, "pr_verification": {},
    }
    return a18.has_findings(masked)


def test_a18_8_acknowledged_mismatch_is_shown_but_not_alerted(repo, conn, run_id):
    """**SHA-3 の実証ケース**: 訂正不能な不一致を受容でき、恒常 ⚠️ 化しない。"""
    r, since = repo
    url = "https://github.com/x/y/pull/830"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    sha = _commit_reviewed_trailer(r, url, REVIEWED_B)
    # 受容前は A-18-8 だけで所見が立つ(受容の効果を対比で示す)。
    assert _only_reviewed_findings(_run_a18_deemed(r, since, conn))
    gov = _ack_reviewed_gov(r, sha, url, REVIEWED_B)
    (r / "config" / "governance.yaml").write_text(
        yaml.safe_dump(gov, allow_unicode=True), encoding="utf-8"
    )
    result = _run_a18_deemed(r, since, conn)
    assert result["reviewed_sha_mismatches"] == []
    assert len(result["acknowledged_reviewed"]) == 1
    assert not _only_reviewed_findings(result)
    embed = a18.build_alert_embed(result)
    assert any("受容済みの審査対象 SHA 不一致" in f["name"] for f in embed["fields"])
    assert not any("⚠️ A-18-8" in f["name"] for f in embed["fields"])
    conn.rollback()


def test_a18_8_acknowledgement_does_not_cover_a_different_sha(repo, conn, run_id):
    """申告値が変われば受容は外れる(古い受容が新しい不一致を覆い隠さない)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/831"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    sha = _commit_reviewed_trailer(r, url, REVIEWED_B)
    gov = _ack_reviewed_gov(r, sha, url, "9" * 40)  # 別の申告値を受容している
    (r / "config" / "governance.yaml").write_text(
        yaml.safe_dump(gov, allow_unicode=True), encoding="utf-8"
    )
    result = _run_a18_deemed(r, since, conn)
    assert len(result["reviewed_sha_mismatches"]) == 1
    assert _only_reviewed_findings(result)
    assert any("一致する所見を持たない" in n for n in result["notes"])
    conn.rollback()


def test_a18_8_invalid_acknowledgement_entry_is_disclosed(repo, conn):
    """欠落エントリは受容として効かせず注記に出す(受容できたと誤認させない)。"""
    r, since = repo
    gov = a18.load_governance(r)
    gov["acknowledged_findings"] = [{"kind": "a18-8", "commit": "z" * 40}]
    (r / "config" / "governance.yaml").write_text(
        yaml.safe_dump(gov, allow_unicode=True), encoding="utf-8"
    )
    result = _run_a18_deemed(r, since, conn)
    assert any("kind: a18-8)のエントリが無効" in n for n in result["notes"])
    conn.rollback()


def test_a18_1_acknowledgement_ignores_a18_8_entries(repo, conn):
    """kind で対象検査が分かれる(A-18-8 の受容が A-18-1 の違反を消さない)。"""
    r, since = repo
    sha = _commit(r, "docs/protected.md", "v9\n", "docs: 無承認の保護領域変更")
    gov = a18.load_governance(r)
    gov["acknowledged_findings"] = [
        {"kind": "a18-8", "commit": sha, "ref": "x", "trailer_reviewed": REVIEWED_A}
    ]
    (r / "config" / "governance.yaml").write_text(
        yaml.safe_dump(gov, allow_unicode=True), encoding="utf-8"
    )
    result = _run_a18_deemed(r, since, conn)
    assert [v["commit"] for v in result["violations"]] == [sha[:12]]
    conn.rollback()


# ── ③ 由来の開示: 突合済みのうち審査記録(意見書 front matter)に由来する件数 ─────
#
# reminder ``reviewed-sha-from-review-agent``: 一致件数だけでは「起票者が両側に同じ値を
# 書いた」のか「独立審査の記録に裏打ちされている」のかを読み分けられない。緑の意味を
# 割合で限定する。
def _deemed_with_review(conn, run_id, proposal_ref: str, reviewed_sha: str | None, review_ref: str):
    from ryza.governance import notices

    return notices.announce_deemed_approval(
        conn, proposal_ref, "pr", "保護領域の変更", run_id,
        reviewed_sha=reviewed_sha, review_ref=review_ref,
    ).decision.id


def _write_review(r: Path, path: str, sha: str | None) -> str:
    """意見書(新様式 = front matter 付き)を一時リポジトリに置く。``sha=None`` は旧様式。"""
    body = "# 独立役員意見書\n\n判定: 条件付き承認\n"
    text = body if sha is None else (
        f"---\nreviewed_sha: {sha}\nreview_date: 2026-08-04\nverdict: conditional_approve\n---\n\n"
        + body
    )
    _commit(r, path, text, f"docs(reviews): 意見書 {path}")
    return path


def test_a18_8_counts_the_shas_that_come_from_the_review_record(repo, conn, run_id):
    """審査記録に由来する reviewed_sha は分子に入る(独立審査の裏付けがある一致)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/820"
    ref = _write_review(r, "docs/reviews/a-review.md", REVIEWED_A)
    _deemed_with_review(conn, run_id, url, REVIEWED_A, ref)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.compared == 1 and scan.from_review_artifact == 1 and scan.findings == []
    conn.rollback()


def test_a18_8_declaration_without_a_review_record_is_not_counted(repo, conn, run_id):
    """意見書が実在しない参照は由来ゼロ(突合は成立しても裏付けは無い)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/821"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)  # review_ref は実在しないパス
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.compared == 1 and scan.from_review_artifact == 0
    conn.rollback()


def test_a18_8_old_style_review_is_not_counted_as_provenance(repo, conn, run_id):
    """front matter の無い旧様式の意見書は由来にならない(遡及改変しない方針の裏返し)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/822"
    ref = _write_review(r, "docs/reviews/old-review.md", None)
    _deemed_with_review(conn, run_id, url, REVIEWED_A, ref)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.compared == 1 and scan.from_review_artifact == 0
    conn.rollback()


def test_a18_8_review_record_with_a_different_sha_is_not_provenance(repo, conn, run_id):
    """意見書が別の SHA を宣言しているなら、その記録は当該 reviewed_sha の裏付けではない。"""
    r, since = repo
    url = "https://github.com/x/y/pull/823"
    ref = _write_review(r, "docs/reviews/b-review.md", REVIEWED_B)
    _deemed_with_review(conn, run_id, url, REVIEWED_A, ref)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.compared == 1 and scan.from_review_artifact == 0
    conn.rollback()


def test_a18_8_broken_front_matter_is_not_counted_as_provenance(repo, conn, run_id):
    """様式不備の意見書は由来に数えない(監査は楽観に倒さない — 止めるのは CLI の責務)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/824"
    _commit(
        r, "docs/reviews/broken-review.md",
        f"---\nreviewed_sha: {REVIEWED_A}\n\n閉じフェンスが無い\n",
        "docs(reviews): 様式不備の意見書",
    )
    _deemed_with_review(conn, run_id, url, REVIEWED_A, "docs/reviews/broken-review.md")
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.compared == 1 and scan.from_review_artifact == 0
    conn.rollback()


def test_a18_8_provenance_reaches_the_result_and_the_green_line(repo, conn, run_id):
    """**緑の行に割合を出す**(注記だけに置くと ✅ が独立審査の証明として読まれる)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/825"
    ref = _write_review(r, "docs/reviews/c-review.md", REVIEWED_A)
    _deemed_with_review(conn, run_id, url, REVIEWED_A, ref)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    result = _run_a18_deemed(r, since, conn)
    assert result["compared_reviewed_shas"] == 1 and result["reviewed_from_artifact"] == 1
    field = next(f for f in a18.build_alert_embed(result)["fields"] if "A-18-8" in f["name"])
    assert "うち審査記録由来 1 件" in field["value"]
    assert any("全件が審査記録に由来" in n for n in result["notes"])
    conn.rollback()


def test_a18_8_unbacked_agreement_is_disclosed_in_the_notes(repo, conn, run_id):
    """裏付けの無い一致が残る限り「独立審査が見た SHA の証明」ではないと毎回書く。"""
    r, since = repo
    url = "https://github.com/x/y/pull/826"
    _deemed_reviewed(conn, run_id, url, REVIEWED_A)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    result = _run_a18_deemed(r, since, conn)
    assert result["reviewed_from_artifact"] == 0
    assert any("証明ではない" in n for n in result["notes"])
    field = next(f for f in a18.build_alert_embed(result)["fields"] if "A-18-8" in f["name"])
    assert "うち審査記録由来 0 件" in field["value"]
    conn.rollback()


def test_a18_8_provenance_is_shown_on_the_mismatch_line_too(repo, conn, run_id):
    """不一致の見出しにも分母・由来を出す(⚠️ の読み手が突合の実効性を測れるように)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/827"
    ref = _write_review(r, "docs/reviews/d-review.md", REVIEWED_A)
    _deemed_with_review(conn, run_id, url, REVIEWED_A, ref)
    _commit_reviewed_trailer(r, url, REVIEWED_B)
    result = _run_a18_deemed(r, since, conn)
    field = next(f for f in a18.build_alert_embed(result)["fields"] if "A-18-8" in f["name"])
    assert "1/1 決定 / うち審査記録由来 1 件" in field["name"]
    conn.rollback()


# ── C-3: 意見書が決定より後に出現した場合は由来にしない ──────────────────────
def _post_hoc_commit_review(r: Path, path: str, sha: str, decided_at: Any) -> str:
    """意見書を ``decided_at`` より後の committer date で追加する(C-3 の post_hoc 実証用)。

    ``git commit --date`` は author date しか変えず ``%cI`` は変わらないため、
    ``GIT_COMMITTER_DATE`` を明示的に決定時刻 + 1 分に設定する。
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    if isinstance(decided_at, _dt):
        after = decided_at + _td(minutes=1)
    else:
        after = _dt.fromisoformat(str(decided_at)) + _td(minutes=1)
    p = r / path
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\nreviewed_sha: {sha}\nreview_date: 2026-08-04\nverdict: conditional_approve\n---\n\n"
        "# 独立役員意見書\n\n判定: 条件付き承認\n"
    )
    p.write_text(body, encoding="utf-8")
    _git(r, "add", "-A")
    env_iso = after.isoformat()
    subprocess.run(
        ["git", "-C", str(r), "commit", "-m", f"docs(reviews): 事後 {path}"],
        check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_COMMITTER_DATE": env_iso, "GIT_AUTHOR_DATE": env_iso},
    )
    return path


def test_a18_8_post_hoc_review_is_not_counted_as_provenance(repo, conn, run_id):
    """意見書 commit が決定 ``decided_at`` より後なら独立審査の裏付けにしない(C-3)。

    旧実装は監査時点で意見書が実在するかだけを見て、決定が先に確定していても由来に数えていた。
    決定と ``Approved:`` を作った後で意見書を commit するだけで由来率 100% にできる経路を塞ぐ。
    """
    from ryza.governance.decisions import current_decision

    r, since = repo
    url = "https://github.com/x/y/pull/828"
    _deemed_with_review(conn, run_id, url, REVIEWED_A, "docs/reviews/post-hoc.md")
    decided_at = current_decision(conn, url)["decided_at"]
    _post_hoc_commit_review(r, "docs/reviews/post-hoc.md", REVIEWED_A, decided_at)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    scan = _scan_a188(r, since, conn)
    assert scan.compared == 1 and scan.from_review_artifact == 0 and scan.post_hoc == 1
    conn.rollback()


def test_a18_8_post_hoc_review_appears_in_breakdown_notes(repo, conn, run_id):
    """C-4 の内訳開示に「事後製造の疑い」が件数で出る(旧実装は 4 種類を 1 つに潰していた)。"""
    from ryza.governance.decisions import current_decision

    r, since = repo
    url = "https://github.com/x/y/pull/829"
    _deemed_with_review(conn, run_id, url, REVIEWED_A, "docs/reviews/post-hoc-2.md")
    decided_at = current_decision(conn, url)["decided_at"]
    _post_hoc_commit_review(r, "docs/reviews/post-hoc-2.md", REVIEWED_A, decided_at)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    result = _run_a18_deemed(r, since, conn)
    assert result["reviewed_post_hoc"] == 1
    assert any("事後製造" in n and "1 件" in n for n in result["notes"])
    conn.rollback()


# ── C-4: 由来なしの内訳をカテゴリ別に開示する ──────────────────────────────
def test_a18_8_breakdown_covers_multiple_categories(repo, conn, run_id):
    """複数カテゴリが混在するとき、内訳注記に各件数が個別に現れる(C-4)。

    旧実装は「compared - from_review_artifact」の 1 数字だったため、旧様式が多い移行期は
    (b) 新様式で SHA 欠落・(c) 参照迂回・(e) 事後製造 がその陰に隠れて検出できなかった。
    """
    r, since = repo
    # (a) 旧様式
    old_url = "https://github.com/x/y/pull/830"
    old_ref = _write_review(r, "docs/reviews/old.md", None)
    _deemed_with_review(conn, run_id, old_url, REVIEWED_A, old_ref)
    _commit(
        r, "docs/protected.md", "breakdown-1\n",
        f"docs: 保護領域変更 1\n\nApproved: {old_url} reviewed={REVIEWED_A}",
    )
    # (b) 参照が読めない
    missing_url = "https://github.com/x/y/pull/831"
    _deemed_reviewed(conn, run_id, missing_url, REVIEWED_A)  # review_ref なし
    _commit(
        r, "docs/protected.md", "breakdown-2\n",
        f"docs: 保護領域変更 2\n\nApproved: {missing_url} reviewed={REVIEWED_A}",
    )
    result = _run_a18_deemed(r, since, conn)
    assert result["reviewed_old_style"] == 1
    assert result["reviewed_unreadable"] == 1
    breakdown = next((n for n in result["notes"] if "由来なしの内訳" in n), None)
    assert breakdown is not None
    assert "旧様式 1 件" in breakdown
    assert "参照が読めない 1 件" in breakdown
    conn.rollback()


def test_a18_8_breakdown_is_omitted_when_all_provenance_is_backed(repo, conn, run_id):
    """全件が由来ありなら内訳注記は出さない(緑の意味を弱める余計な注記を避ける)。"""
    r, since = repo
    url = "https://github.com/x/y/pull/832"
    ref = _write_review(r, "docs/reviews/all-backed.md", REVIEWED_A)
    _deemed_with_review(conn, run_id, url, REVIEWED_A, ref)
    _commit_reviewed_trailer(r, url, REVIEWED_A)
    result = _run_a18_deemed(r, since, conn)
    assert result["reviewed_from_artifact"] == 1
    assert not any("由来なしの内訳" in n for n in result["notes"])
    conn.rollback()
