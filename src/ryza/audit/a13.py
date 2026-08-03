"""A-13 規則⇔実装トレーサビリティ監査(定款第6条・config/governance.yaml controls)。

4つの検査を実行し、構造化 dict を返す:

  A-13-1 保護領域突合   … `protected_areas` の glob に触れた発効日以後のコミットを列挙し、
                          (a) ``Approved:`` トレーラ (b) GitHub マージ PR 経由(Merge pull request
                          マージコミットの配下)のいずれも無いものを違反として列挙する
  A-13-2 文書⇔config    … 80-ips.md ⇔ config/ips.yaml、06-constitution.md ⇔ config/governance.yaml
                          のバージョン文字列一致を検査する
  A-13-3 宣言棚卸し     … controls のうち ``enforcement: declaration`` を列挙する(検査ではなく
                          可視化 — 四半期ごとの執行点実装可否の再評価対象)
  A-13-4 全変更 PR 化   … 基準コミット(``PR_RULE_BASELINE_COMMIT``)以降の first-parent 履歴で、
                          マージコミットでないコミット(= main への直 push)を保護領域か否かに
                          かかわらず違反として列挙する。例外なし(``Approved:`` トレーラ付き
                          直 push も違反 — 2026-08-03 代表指示)

**read-only 原則**: 本モジュールは検査と警告(``press.outbox`` の ops チャンネルへの embed 投入)
のみを行い、修正・巻き戻し・コミットは一切行わない。

**対象範囲**: 発効日(2026-08-03 の定款批准コミット ``RATIFICATION_COMMIT``)より後のコミットのみ。
``git rev-list <批准>..HEAD`` は批准コミット自身とその祖先を除外する。

**既知の限界(独立役員審査 2026-08-03 指摘により報告 notes へ毎回開示する)**:

- PR 件名(``Merge pull request``)は自己申告であり GitHub API と未照合。件名偽装で承認を
  装える(実弾移行前提条件として API 照合を実装する — ops/reminders.yaml 登録済み)
- 承認記録(Issue / governance.decisions)の実在までは照会しない(トレーラの存在検査まで)。
  実在照合は governance.decisions 実装の拡充後に追加する
- GitHub の squash マージ(``... (#N)`` 形式の単独コミット)は「マージ PR」と判定しない。
  本リポジトリの承認手続はマージコミット(``Merge pull request``)で行われている(批准 PR #32 が
  実例)。squash 併用を始める場合は判定の拡張が必要

**evil merge 対策**: マージコミット自身のコンフリクト解消差分は ``git diff-tree --cc``
(全親と異なるファイルのみ列挙)で検査する。保護パスに触れる場合は **マージコミット自身の**
``Approved:`` トレーラを必須とし、PR マージ件名だけでは承認と見なさない(レビュー承認は
ブランチ内容に対するもので、マージ時に持ち込まれた差分をカバーしないため)。クリーンな
マージは ``--cc`` に現れないので誤検知しない。

git 操作は subprocess で行い、リポジトリパスは引数化してテスト可能にしている。
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ryza.bot import COLOR_FLASH, COLOR_NORMAL, DISCLAIMER
from ryza.bot.outbox import enqueue

log = logging.getLogger("ryza.audit.a13")

# 定款批准コミット(2026-08-03 発効・Merge pull request #32)。これ以前は監査対象外。
RATIFICATION_COMMIT = "c7af81ef85cc9f45bb7881ffc45769abfbc771dc"

# 全変更 PR 化ルール(2026-08-03 代表指示: 保護領域に限らずリポジトリへの全変更を PR 経由と
# する)の基準コミット = ルール採用日(A-13-4 実装時点)の origin/main HEAD。
#   4c7f6e9 "docs(tasks): T-017 FM エージェント第一陣(Ben・Jim)の実装指示書"
# これ以前の直 push は対象外(遡及しない)。GitHub 無料プラン(私有リポ)ではブランチ保護が
# 使えないため、本監査(A-13-4)がこのルールの執行点になる。
PR_RULE_BASELINE_COMMIT = "4c7f6e9daded18a3e9e903a80c87feba3576b52c"

GOVERNANCE_PATH = "config/governance.yaml"

# 既知の限界の常時開示(独立役員審査条件)。報告 embed の notes に毎回載せる。
STANDARD_DISCLOSURES: tuple[str, ...] = (
    "PR 件名(Merge pull request)は自己申告で GitHub API 未照合(照合実装は実弾移行前提条件)",
    "Approved トレーラの参照先(Issue / governance.decisions)の実在は未照合",
    "マージのコンフリクト解消差分(evil merge)は --cc で検査し、保護パスに触れる場合は"
    "マージ自身の Approved トレーラを要求",
    "A-13-4 はコミットの親数のみで直 push を判定 — ローカルで作った非 PR マージの直 push は"
    "検出できない(保護パスに触れる場合は A-13-1 が PR 件名で検出)",
)

# 文書⇔config のバージョン突合ペア(A-13-2)。(文書, config, config 内の version キー)
VERSION_PAIRS: tuple[tuple[str, str], ...] = (
    ("docs/design/80-ips.md", "config/ips.yaml"),
    ("docs/design/06-constitution.md", "config/governance.yaml"),
)

# GitHub マージ PR のマージコミット件名。
_PR_MERGE_RE = re.compile(r"^Merge pull request #\d+")

# 見出し行のバージョン表記(例: 「# Ryza 投資方針書(IPS)v1.3」)。
_DOC_VERSION_RE = re.compile(r"v(\d+(?:\.\d+)+)")


# ────────────────────────────────────────────────────────────────────────────
# git ヘルパ(subprocess・リポジトリパス引数化)
# ────────────────────────────────────────────────────────────────────────────
def _git(repo: str | Path, *args: str) -> str:
    """git コマンドを実行し stdout を返す(失敗は CalledProcessError)。"""
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _git_ok(repo: str | Path, *args: str) -> bool:
    """git コマンドの成否のみ返す(``merge-base --is-ancestor`` 用)。"""
    res = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return res.returncode == 0


def _rev_list(repo: str | Path, since: str | None, *flags: str) -> list[str]:
    """``since..HEAD``(since=None なら全履歴)のコミット列を古い順に返す。"""
    rng = f"{since}..HEAD" if since else "HEAD"
    out = _git(repo, "rev-list", "--reverse", *flags, rng)
    return [ln for ln in out.splitlines() if ln]


# ────────────────────────────────────────────────────────────────────────────
# glob マッチ(protected_areas のパターン)
# ────────────────────────────────────────────────────────────────────────────
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """protected_areas の glob を正規表現へ変換する。

    ``**`` は任意(``/`` を含む)、``*``/``?`` はパス区切りを跨がない。fnmatch は ``*`` が
    ``/`` を跨いでしまい ``migrations/*.sql`` が過剰マッチするため自前で変換する。
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1  # "**/" は "**" と同義に丸める
        elif c == "*":
            parts.append("[^/]*")
            i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def match_protected(files: list[str], patterns: list[re.Pattern[str]]) -> list[str]:
    """protected glob のいずれかに一致するファイルだけ返す。"""
    return [f for f in files if any(p.match(f) for p in patterns)]


# ────────────────────────────────────────────────────────────────────────────
# governance.yaml の読取
# ────────────────────────────────────────────────────────────────────────────
def load_governance(
    repo_path: str | Path, governance_path: str = GOVERNANCE_PATH
) -> dict[str, Any]:
    """governance.yaml を読み込む(A-13 の検査仕様はこのファイルが定義する)。"""
    text = (Path(repo_path) / governance_path).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def protected_patterns(gov: dict[str, Any]) -> list[re.Pattern[str]]:
    return [glob_to_regex(str(e["path"])) for e in gov.get("protected_areas", [])]


# ────────────────────────────────────────────────────────────────────────────
# A-13-1 保護領域突合
# ────────────────────────────────────────────────────────────────────────────
def has_approval_trailer(message: str, trailer: str = "Approved:") -> bool:
    """コミット本文に ``Approved: <参照>`` トレーラ行があるか(定款第5条 C-5 様式)。"""
    pat = re.compile(rf"^{re.escape(trailer)}\s*\S+", re.MULTILINE)
    return bool(pat.search(message))


def _find_introducing_merge(
    repo: str | Path, sha: str, first_parent_merges: list[str]
) -> str | None:
    """コミット ``sha`` を main に持ち込んだ first-parent マージコミットを返す(古い順走査)。"""
    for m in first_parent_merges:
        if _git_ok(repo, "merge-base", "--is-ancestor", sha, m):
            return m
    return None


def check_protected_commits(
    repo_path: str | Path,
    gov: dict[str, Any],
    *,
    since_commit: str | None = RATIFICATION_COMMIT,
) -> tuple[list[dict[str, Any]], int]:
    """A-13-1: 保護領域に触れた無承認コミットの一覧と、検査したコミット数を返す。

    承認とみなす条件(定款附則):
      (a) コミット本文の ``Approved:`` トレーラ
      (b) GitHub マージ PR 経由 = ``Merge pull request`` マージコミットの配下で main に到達
    ``since_commit``(批准コミット)以前のコミットは ``rev-list since..HEAD`` により対象外。
    """
    repo = str(repo_path)
    if since_commit and not _git_ok(repo, "cat-file", "-e", f"{since_commit}^{{commit}}"):
        raise ValueError(f"発効基準コミットがリポジトリに存在しない: {since_commit}")

    patterns = protected_patterns(gov)
    trailer = str(gov.get("approval_trailer") or "Approved:")
    commits = _rev_list(repo, since_commit)
    first_parent = set(_rev_list(repo, since_commit, "--first-parent"))
    fp_merges = _rev_list(repo, since_commit, "--first-parent", "--merges")

    violations: list[dict[str, Any]] = []
    for sha in commits:
        parents = _git(repo, "log", "-1", "--format=%P", sha).split()
        is_merge = len(parents) > 1
        if is_merge:
            # evil merge 対策: マージ自身のコンフリクト解消差分(全親と異なるファイルのみ)。
            # クリーンなマージは --cc に現れない。
            diff_args = ("diff-tree", "--cc", "--no-commit-id", "--name-only", sha)
        else:
            diff_args = ("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
        files = [ln for ln in _git(repo, *diff_args).splitlines() if ln]
        touched = match_protected(files, patterns)
        if not touched:
            continue

        message = _git(repo, "log", "-1", "--format=%B", sha)
        if has_approval_trailer(message, trailer):
            continue
        if is_merge:
            # マージ自身の差分は PR 件名では承認と見なさない(レビューはブランチ内容に対する
            # もので、マージ時に持ち込まれた差分をカバーしない)。トレーラ必須。
            reason = "マージ自身のコンフリクト解消差分(evil merge)で Approved トレーラなし"
        elif sha not in first_parent:
            merge = _find_introducing_merge(repo, sha, fp_merges)
            if merge and _PR_MERGE_RE.match(_git(repo, "log", "-1", "--format=%s", merge)):
                continue  # マージ PR 経由 = 代表承認(附則)
            reason = "マージ経由だが PR マージコミットが確認できない"
        else:
            reason = "main への直接コミットで Approved トレーラなし"
        violations.append(
            {
                "commit": sha[:12],
                "subject": _git(repo, "log", "-1", "--format=%s", sha).strip(),
                "files": touched,
                "reason": reason,
            }
        )
    return violations, len(commits)


# ────────────────────────────────────────────────────────────────────────────
# A-13-2 文書⇔config 整合
# ────────────────────────────────────────────────────────────────────────────
def doc_version(path: Path) -> str | None:
    """文書先頭の見出し行から ``vX.Y`` を抽出する(無ければ None)。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("#"):
            m = _DOC_VERSION_RE.search(line)
            return m.group(1) if m else None
    return None


def config_version(path: Path) -> str | None:
    """機械可読 config の ``version`` キーを返す(無ければ None)。"""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return None
    v = doc.get("version")
    return None if v is None else str(v).lstrip("v")


def check_versions(
    repo_path: str | Path,
    pairs: tuple[tuple[str, str], ...] = VERSION_PAIRS,
) -> list[dict[str, Any]]:
    """A-13-2: 発効文書と機械可読 config のバージョン不一致を列挙する。"""
    root = Path(repo_path)
    mismatches: list[dict[str, Any]] = []
    for doc_rel, cfg_rel in pairs:
        dv = doc_version(root / doc_rel)
        cv = config_version(root / cfg_rel)
        if dv is None or cv is None or dv != cv:
            reason = "バージョン表記が取得できない" if None in (dv, cv) else "バージョン不一致"
            mismatches.append(
                {
                    "doc": doc_rel,
                    "config": cfg_rel,
                    "doc_version": dv,
                    "config_version": cv,
                    "reason": reason,
                }
            )
    return mismatches


# ────────────────────────────────────────────────────────────────────────────
# A-13-3 宣言棚卸し
# ────────────────────────────────────────────────────────────────────────────
def list_declarations(gov: dict[str, Any]) -> list[dict[str, Any]]:
    """controls のうち enforcement: declaration の項目(執行点なし)を列挙する。"""
    return [
        {"rule": c.get("rule"), "verification": c.get("verification")}
        for c in gov.get("controls", [])
        if c.get("enforcement") == "declaration"
    ]


def _coverage_notes(gov: dict[str, Any]) -> list[str]:
    """protected_areas の登録漏れ(governance.yaml のコメントで予告された項目)を注記する。"""
    notes: list[str] = []
    paths = [str(e.get("path", "")) for e in gov.get("protected_areas", [])]
    if not any(p.startswith("src/ryza/audit") for p in paths):
        notes.append(
            "protected_areas に監査部門コード(src/ryza/audit)が未登録(定款第5条。統合時に追記)"
        )
    return notes


def _staleness_note(repo_path: str | Path) -> list[str]:
    """検査対象 checkout の鮮度検査(read-only: fetch はしない)。

    ``origin/main`` の追跡 ref が存在し、HEAD がそれを含まない(= 手元の追跡情報より古い
    履歴を監査している)場合に警告する。追跡 ref 自体が古い可能性は検出できないことも含めて
    注記する。追跡 ref が無い環境(一時リポジトリ等)は注記なし。
    """
    if not _git_ok(repo_path, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"):
        return []
    if _git_ok(repo_path, "merge-base", "--is-ancestor", "origin/main", "HEAD"):
        return []
    return [
        "stale checkout: HEAD が origin/main を含まない — 最新でない履歴を監査している可能性"
        "(read-only 原則により fetch はしない。checkout の更新は運用側で)"
    ]


# ────────────────────────────────────────────────────────────────────────────
# A-13-4 全変更 PR 化(直 push 検査)
# ────────────────────────────────────────────────────────────────────────────
def check_direct_pushes(
    repo_path: str | Path,
    *,
    since_commit: str | None = PR_RULE_BASELINE_COMMIT,
) -> tuple[list[dict[str, Any]], int]:
    """A-13-4: main への直 push の一覧と、検査した first-parent コミット数を返す。

    基準コミット(全変更 PR 化ルール採用日の main HEAD)以降の first-parent 履歴で、
    マージコミットでないコミット = 直 push を違反とする。保護領域か否かは問わず、
    例外も設けない(``Approved:`` トレーラ付き直 push も違反 — 全 PR 化ルールに例外なし)。
    基準コミット以前は ``rev-list since..HEAD`` により対象外。
    """
    repo = str(repo_path)
    if since_commit and not _git_ok(repo, "cat-file", "-e", f"{since_commit}^{{commit}}"):
        raise ValueError(f"全変更 PR 化の基準コミットがリポジトリに存在しない: {since_commit}")

    fp_commits = _rev_list(repo, since_commit, "--first-parent")
    violations: list[dict[str, Any]] = []
    for sha in fp_commits:
        parents = _git(repo, "log", "-1", "--format=%P", sha).split()
        if len(parents) > 1:
            continue  # マージコミット = PR マージ経由(件名検査は A-13-1 の責務)
        files = [
            ln
            for ln in _git(
                repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha
            ).splitlines()
            if ln
        ]
        violations.append(
            {
                "commit": sha[:12],
                "subject": _git(repo, "log", "-1", "--format=%s", sha).strip(),
                "files": files,
                "reason": "main への直 push(全変更 PR 化ルール違反 — 例外なし)",
            }
        )
    return violations, len(fp_commits)


# ────────────────────────────────────────────────────────────────────────────
# 本体・報告
# ────────────────────────────────────────────────────────────────────────────
def run_a13(
    repo_path: str | Path,
    *,
    governance_path: str = GOVERNANCE_PATH,
    since_commit: str | None = RATIFICATION_COMMIT,
    pr_since_commit: str | None = PR_RULE_BASELINE_COMMIT,
    version_pairs: tuple[tuple[str, str], ...] = VERSION_PAIRS,
) -> dict[str, Any]:
    """A-13 の4検査を実行して構造化 dict を返す(DB・Discord に依存しない純検査)。"""
    gov = load_governance(repo_path, governance_path)
    violations, checked = check_protected_commits(repo_path, gov, since_commit=since_commit)
    direct_pushes, fp_checked = check_direct_pushes(repo_path, since_commit=pr_since_commit)
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "since_commit": since_commit,
        "checked_commits": checked,
        "violations": violations,
        "mismatches": check_versions(repo_path, version_pairs),
        "declarations": list_declarations(gov),
        "pr_since_commit": pr_since_commit,
        "checked_first_parent": fp_checked,
        "direct_pushes": direct_pushes,
        # 既知の限界は毎回開示する(独立役員審査条件)+ 個別の注記(登録漏れ・鮮度)。
        "notes": [
            *_coverage_notes(gov),
            *_staleness_note(repo_path),
            *STANDARD_DISCLOSURES,
        ],
    }


def has_findings(result: dict[str, Any]) -> bool:
    """警告(embed 投入)を要する所見があるか。"""
    return bool(result["violations"] or result["mismatches"] or result["direct_pushes"])


def build_alert_embed(result: dict[str, Any]) -> dict[str, Any]:
    """#運営 向けの警告/報告 embed(daily の実行サマリと同じ流儀)。"""
    fields: list[dict[str, Any]] = []

    if result["violations"]:
        lines = [
            f"- `{v['commit']}` {v['subject']}({v['reason']}: {', '.join(v['files'])})"
            for v in result["violations"]
        ]
        fields.append(
            {
                "name": "⚠️ A-13-1 保護領域の無承認変更",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append(
            {
                "name": "A-13-1 保護領域突合",
                "value": f"✅ 違反なし(検査 {result['checked_commits']} コミット)",
                "inline": False,
            }
        )

    if result["mismatches"]:
        lines = [
            f"- {m['doc']}(v{m['doc_version']})⇔ {m['config']}"
            f"(v{m['config_version']}): {m['reason']}"
            for m in result["mismatches"]
        ]
        fields.append(
            {
                "name": "⚠️ A-13-2 文書⇔config 不整合",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append({"name": "A-13-2 文書⇔config 整合", "value": "✅ 一致", "inline": False})

    decls = result["declarations"]
    decl_lines = [f"- {d['rule']}" for d in decls] or ["なし"]
    fields.append(
        {
            "name": f"A-13-3 宣言のみ条文(執行点なし): {len(decls)} 件",
            "value": "\n".join(decl_lines)[:1024],
            "inline": False,
        }
    )

    if result["direct_pushes"]:
        lines = [
            f"- `{v['commit']}` {v['subject']}({', '.join(v['files'])})"
            for v in result["direct_pushes"]
        ]
        fields.append(
            {
                "name": "⚠️ A-13-4 main への直 push(全変更 PR 化ルール違反)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append(
            {
                "name": "A-13-4 全変更 PR 化",
                "value": f"✅ 直 push なし(検査 {result['checked_first_parent']} コミット)",
                "inline": False,
            }
        )
    if result["notes"]:
        notes_value = "\n".join(f"- {n}" for n in result["notes"])[:1024]
        fields.append({"name": "注記", "value": notes_value, "inline": False})

    alert = has_findings(result)
    return {
        "title": ("⚠️ A-13 監査: 要対応の所見あり" if alert else "A-13 監査: 所見なし"),
        "description": (
            "規則⇔実装トレーサビリティ監査(定款第6条)。監査は read-only であり修正は行わない。"
        ),
        "color": COLOR_FLASH if alert else COLOR_NORMAL,
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


def enqueue_alert(conn: Any, result: dict[str, Any], run_id: int, *, channel: str = "ops") -> int:
    """検査結果 embed を ``press.outbox`` の ops チャンネルへ投入する(違反時は urgent)。"""
    urgent = bool(result["violations"] or result["direct_pushes"])
    return enqueue(conn, channel, build_alert_embed(result), run_id, urgent=urgent)


def run_and_report(
    repo_path: str | Path,
    *,
    dry_run: bool = False,
    always_report: bool = False,
    since_commit: str | None = RATIFICATION_COMMIT,
    pr_since_commit: str | None = PR_RULE_BASELINE_COMMIT,
) -> dict[str, Any]:
    """A-13 を実行し、所見があれば(または ``always_report``)#運営 へ enqueue する。

    ops-weekly など他ジョブからの呼び出し口。``dry_run`` では DB に接続せずログのみ。
    """
    result = run_a13(repo_path, since_commit=since_commit, pr_since_commit=pr_since_commit)
    report = has_findings(result) or always_report
    if dry_run:
        log.info(
            "[DRY_RUN] A-13 結果: violations=%d mismatches=%d declarations=%d "
            "direct_pushes=%d(enqueue %s)",
            len(result["violations"]), len(result["mismatches"]), len(result["declarations"]),
            len(result["direct_pushes"]),
            "対象" if report else "不要",
        )
        return result
    if not report:
        log.info("A-13: 所見なし(enqueue しない)")
        return result

    from ryza.db.conn import connect
    from ryza.provenance import start_run

    run = start_run("audit.a13", {"repo": str(repo_path)})
    conn = connect()
    try:
        oid = enqueue_alert(conn, result, run.run_id)
        conn.commit()
        run.finish("success")
        log.info("A-13 警告を enqueue: outbox_id=%s", oid)
    except Exception:
        conn.rollback()
        run.finish("failed")
        raise
    finally:
        conn.close()
    return result


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 実行パス
    """CLI: ``python -m ryza.audit.a13 [--repo PATH] [--dry-run] [--always-report]``"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="A-13 規則⇔実装トレーサビリティ監査")
    parser.add_argument("--repo", default=".", help="監査対象の git リポジトリパス")
    parser.add_argument("--dry-run", action="store_true", help="DB へ書き込まずログのみ")
    parser.add_argument(
        "--always-report", action="store_true", help="所見が無くても #運営 へ結果を投入する"
    )
    args = parser.parse_args(argv)

    result = run_and_report(
        args.repo, dry_run=args.dry_run, always_report=args.always_report
    )
    for v in result["violations"]:
        print(f"[違反] {v['commit']} {v['subject']}: {v['files']}", file=sys.stderr)
    for m in result["mismatches"]:
        print(f"[不整合] {m['doc']} v{m['doc_version']} ⇔ {m['config']} v{m['config_version']}",
              file=sys.stderr)
    for d in result["direct_pushes"]:
        print(f"[直push] {d['commit']} {d['subject']}: {d['files']}", file=sys.stderr)
    print(
        f"A-13 完了(検査 {result['checked_commits']} コミット, 違反 {len(result['violations'])}, "
        f"不整合 {len(result['mismatches'])}, 宣言 {len(result['declarations'])}, "
        f"直push {len(result['direct_pushes'])})",
        file=sys.stderr,
    )
    return 1 if has_findings(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
