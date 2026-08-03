"""A-18 規則⇔実装トレーサビリティ監査(定款第6条・config/governance.yaml controls)。

4つの検査を実行し、構造化 dict を返す:

  A-18-1 保護領域突合   … `protected_areas` の glob に触れた発効日以後のコミットを列挙し、
                          (a) ``Approved:`` トレーラ (b) GitHub マージ PR 経由(Merge pull request
                          マージコミットの配下)のいずれも無いものを違反として列挙する
  A-18-2 文書⇔config    … 80-ips.md ⇔ config/ips.yaml、06-constitution.md ⇔ config/governance.yaml
                          のバージョン文字列一致を検査する
  A-18-3 宣言棚卸し     … controls のうち ``enforcement: declaration`` を列挙する(検査ではなく
                          可視化 — 四半期ごとの執行点実装可否の再評価対象)
  A-18-4 全変更 PR 化   … 基準コミット(``PR_RULE_BASELINE_COMMIT``)以降の first-parent 履歴で、
                          (a) マージコミットでないコミット(= main への直 push)
                          (b) 件名が PR マージ形式(``Merge pull request``)でないマージコミット
                          を保護領域か否かにかかわらず違反として列挙する。例外なし
                          (``Approved:`` トレーラ付き直 push も違反 — 2026-08-03 代表指示)

**read-only 原則**: 本モジュールは検査と警告(``press.outbox`` の ops チャンネルへの embed 投入)
のみを行い、修正・巻き戻し・コミットは一切行わない。

**対象範囲**: 発効日(2026-08-03 の定款批准コミット ``RATIFICATION_COMMIT``)より後のコミットのみ。
``git rev-list <批准>..HEAD`` は批准コミット自身とその祖先を除外する。

**既知の限界(独立役員審査 2026-08-03 指摘により報告 notes へ毎回開示する)**:

- PR 件名(``Merge pull request``)は自己申告であり GitHub API と未照合。件名偽装で承認を
  装える(実弾移行前提条件として API 照合を実装する — ops/reminders.yaml 登録済み)
- ``Approved:`` トレーラのうち **Issue URL 形式**は実在を照会しない(存在検査まで)。
  ``governance.decisions`` の **ID 形式**(``123`` / ``decision:123``)は DB 接続がある実行に
  限り ``governance.current_decisions`` と突合し、否認済み(``effective_decision='vetoed'``)・
  却下・不在は承認として受理しない(独立役員審査 0021 C-5)。``--dry-run`` や conn 未指定の
  呼び出しでは照合を行わず、その旨を notes に載せる
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

from ryza import org
from ryza.bot import COLOR_FLASH, COLOR_NORMAL, DISCLAIMER
from ryza.bot.outbox import enqueue

log = logging.getLogger("ryza.audit.a18")

# 定款批准コミット(2026-08-03 発効・Merge pull request #32)。これ以前は監査対象外。
RATIFICATION_COMMIT = "c7af81ef85cc9f45bb7881ffc45769abfbc771dc"

# 全変更 PR 化ルール(2026-08-03 代表指示: 保護領域に限らずリポジトリへの全変更を PR 経由と
# する)の基準コミット = ルール採用日(A-18-4 実装時点)の origin/main HEAD。
#   4c7f6e9 "docs(tasks): T-017 FM エージェント第一陣(Ben・Jim)の実装指示書"
# これ以前の直 push は対象外(遡及しない)。GitHub 無料プラン(私有リポ)ではブランチ保護が
# 使えないため、本監査(A-18-4)がこのルールの執行点になる。
PR_RULE_BASELINE_COMMIT = "4c7f6e9daded18a3e9e903a80c87feba3576b52c"

GOVERNANCE_PATH = "config/governance.yaml"

# 既知の限界の常時開示(独立役員審査条件)。報告 embed の notes に毎回載せる。
STANDARD_DISCLOSURES: tuple[str, ...] = (
    "PR 件名(Merge pull request)は自己申告で GitHub API 未照合(照合実装は実弾移行前提条件)",
    "Approved トレーラの Issue URL は実在未照合(governance.decisions の ID は "
    "current_decisions と突合し、否認済み・却下・不在を承認として受理しない)",
    "マージのコンフリクト解消差分(evil merge)は --cc で検査し、保護パスに触れる場合は"
    "マージ自身の Approved トレーラを要求",
    "A-18-4 のマージ判定は親数+PR 件名(A-18-1 と同一の検査)— 件名は自己申告で"
    "GitHub API 未照合の限界を共有する",
)

# 文書⇔config のバージョン突合ペア(A-18-2)。(文書, config, config 内の version キー)
VERSION_PAIRS: tuple[tuple[str, str], ...] = (
    ("docs/design/80-ips.md", "config/ips.yaml"),
    ("docs/design/06-constitution.md", "config/governance.yaml"),
)

# GitHub マージ PR のマージコミット件名。
_PR_MERGE_RE = re.compile(r"^Merge pull request #\d+")

# 見出し行のバージョン表記(例: 「# Ryza 投資方針書(IPS)v1.3」)。
_DOC_VERSION_RE = re.compile(r"v(\d+(?:\.\d+)+)")

# Approved トレーラの参照が governance.decisions の ID を指す表記(裸の数字 / decision:123)。
_DECISION_REF_RE = re.compile(r"^(?:decision:)?(\d+)$")

# 現決定 view の effective_decision のうち「発効している承認」。'vetoed' は含めない
# (否認された承認をトレーラの参照先として受理しない — 独立役員審査 0021 C-5)。
APPROVED_DECISIONS: frozenset[str] = frozenset({"approve", "deemed"})


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
    """governance.yaml を読み込む(A-18 の検査仕様はこのファイルが定義する)。"""
    text = (Path(repo_path) / governance_path).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def protected_patterns(gov: dict[str, Any]) -> list[re.Pattern[str]]:
    return [glob_to_regex(str(e["path"])) for e in gov.get("protected_areas", [])]


# ────────────────────────────────────────────────────────────────────────────
# A-18-1 保護領域突合
# ────────────────────────────────────────────────────────────────────────────
def approval_trailer_refs(message: str, trailer: str = "Approved:") -> list[str]:
    """``Approved: <参照>`` トレーラ行の参照値を全て返す(定款第5条 C-5 様式)。

    参照は「GitHub Issue URL または ``governance.decisions`` の ID」
    (config/governance.yaml の様式コメント)。1コミットに複数のトレーラを許すのは、
    複数の承認記録にまたがる変更(例: 独立役員審査 + 代表の明示承認)を表現するため。
    """
    pat = re.compile(rf"^{re.escape(trailer)}\s*(\S+)", re.MULTILINE)
    return [m.group(1) for m in pat.finditer(message)]


def has_approval_trailer(message: str, trailer: str = "Approved:") -> bool:
    """コミット本文に ``Approved: <参照>`` トレーラ行があるか(存在検査のみ)。"""
    return bool(approval_trailer_refs(message, trailer))


def decision_ref_id(ref: str) -> int | None:
    """トレーラ参照が ``governance.decisions`` の ID 形式なら整数、違えば None。

    受理する表記は裸の数字(``123``)と ``decision:123``。Issue URL 等は None を返し、
    従来どおり存在検査までで扱う(URL の実在照合は GitHub API が必要 — 未実装)。
    """
    m = _DECISION_REF_RE.match(ref)
    return int(m.group(1)) if m else None


def verify_decision_refs(conn: Any, refs: list[str]) -> tuple[bool, list[str]]:
    """トレーラ参照を ``governance.current_decisions`` と突合する。

    Returns:
        ``(承認として受理できるか, 受理できない理由)``。ID 形式の参照が1つも無ければ
        照合対象が無いので ``(True, [])``(従来どおりトレーラの存在をもって受理)。
        ID 形式が1つでもある場合は、**そのうち少なくとも1つが有効な承認**
        (``effective_decision`` が ``approve`` / ``deemed``)であることを要求する。

    **否認済みを受理しない**のが本関数の存在理由である(独立役員審査 0021 C-5)。
    ``governance.decisions`` を直読すると、代表が否認した承認を A-18 が承認として受理し、
    否認された変更(= 取消義務が発生している変更)が無承認変更として検出されない。
    現決定 view は否認を反映して ``vetoed`` を返すため、view 経由でのみ突合する。
    """
    from ryza.governance.decisions import current_decision_by_id

    ids = [i for i in (decision_ref_id(r) for r in refs) if i is not None]
    if not ids:
        return True, []
    problems: list[str] = []
    for decision_id in ids:
        row = current_decision_by_id(conn, decision_id)
        if row is None:
            problems.append(f"承認記録 id={decision_id} が governance.decisions に存在しない")
            continue
        effective = str(row["effective_decision"])
        if effective in APPROVED_DECISIONS:
            return True, []
        if effective == "vetoed":
            problems.append(
                f"承認記録 id={decision_id} は代表により否認済み"
                f"(recorded={row['recorded_decision']} / 取消義務が発生している)"
            )
        else:
            problems.append(f"承認記録 id={decision_id} は decision='{effective}' で承認ではない")
    return False, problems


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
    conn: Any | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """A-18-1: 保護領域に触れた無承認コミットの一覧と、検査したコミット数を返す。

    承認とみなす条件(定款附則):
      (a) コミット本文の ``Approved:`` トレーラ。``conn`` が与えられ、参照が
          ``governance.decisions`` の ID 形式なら :func:`verify_decision_refs` で実在照合し、
          **否認済み・却下・不在は承認と見なさない**
      (b) GitHub マージ PR 経由 = ``Merge pull request`` マージコミットの配下で main に到達
    ``since_commit``(批准コミット)以前のコミットは ``rev-list since..HEAD`` により対象外。

    **トレーラが無効なら (b) では救済しない**: 「この承認記録で承認された」と明示的に
    主張しているコミットが、その記録の否認によって主張を失った場合、PR 経由であることを
    理由に承認扱いへ戻すと否認が監査から見えなくなる。否認は取消義務(定款第3条)を
    生じさせるので、取消されるまでは無承認変更として列挙されるのが正しい。
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
        refs = approval_trailer_refs(message, trailer)
        trailer_reason: str | None = None
        if refs:
            accepted, problems = (True, []) if conn is None else verify_decision_refs(conn, refs)
            if accepted:
                continue
            trailer_reason = "Approved トレーラの承認記録が有効でない: " + "; ".join(problems)

        if trailer_reason is not None:
            reason = trailer_reason
        elif is_merge:
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
# A-18-2 文書⇔config 整合
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
    """A-18-2: 発効文書と機械可読 config のバージョン不一致を列挙する。"""
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
# A-18-3 宣言棚卸し
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
# A-18-4 全変更 PR 化(直 push 検査)
# ────────────────────────────────────────────────────────────────────────────
def check_direct_pushes(
    repo_path: str | Path,
    *,
    since_commit: str | None = PR_RULE_BASELINE_COMMIT,
) -> tuple[list[dict[str, Any]], int]:
    """A-18-4: main への直 push・非 PR マージの一覧と、検査した first-parent コミット数を返す。

    基準コミット(全変更 PR 化ルール採用日の main HEAD)以降の first-parent 履歴で、
    (a) マージコミットでないコミット = 直 push、(b) 件名が PR マージ形式
    (``_PR_MERGE_RE``、A-18-1 と同一の検査)でないマージコミット = 非 PR マージ、
    を違反とする。保護領域か否かは問わず、例外も設けない(``Approved:`` トレーラ付き
    直 push も違反 — 全 PR 化ルールに例外なし)。基準コミット以前は
    ``rev-list since..HEAD`` により対象外。
    """
    repo = str(repo_path)
    if since_commit and not _git_ok(repo, "cat-file", "-e", f"{since_commit}^{{commit}}"):
        raise ValueError(f"全変更 PR 化の基準コミットがリポジトリに存在しない: {since_commit}")

    fp_commits = _rev_list(repo, since_commit, "--first-parent")
    violations: list[dict[str, Any]] = []
    for sha in fp_commits:
        parents = _git(repo, "log", "-1", "--format=%P", sha).split()
        if len(parents) > 1:
            if _PR_MERGE_RE.match(_git(repo, "log", "-1", "--format=%s", sha)):
                continue  # PR マージコミット(件名は自己申告 — 開示のとおり API 未照合)
            reason = "main への非 PR マージ(全変更 PR 化ルール違反 — 例外なし)"
            # マージが main に持ち込んだ内容 = first parent との差分を列挙する。
            diff_args = (
                "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", sha
            )
        else:
            reason = "main への直 push(全変更 PR 化ルール違反 — 例外なし)"
            diff_args = ("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
        files = [ln for ln in _git(repo, *diff_args).splitlines() if ln]
        violations.append(
            {
                "commit": sha[:12],
                "subject": _git(repo, "log", "-1", "--format=%s", sha).strip(),
                "files": files,
                "reason": reason,
            }
        )
    return violations, len(fp_commits)


# ────────────────────────────────────────────────────────────────────────────
# 本体・報告
# ────────────────────────────────────────────────────────────────────────────
def run_a18(
    repo_path: str | Path,
    *,
    governance_path: str = GOVERNANCE_PATH,
    since_commit: str | None = RATIFICATION_COMMIT,
    pr_since_commit: str | None = PR_RULE_BASELINE_COMMIT,
    version_pairs: tuple[tuple[str, str], ...] = VERSION_PAIRS,
    conn: Any | None = None,
) -> dict[str, Any]:
    """A-18 の4検査を実行して構造化 dict を返す(git と設定ファイルのみの検査)。

    ``conn`` を渡すと A-18-1 が ``Approved:`` トレーラの参照先(``governance.decisions``
    の ID 形式)を ``governance.current_decisions`` と突合する(read-only)。渡さない
    場合は従来どおりトレーラの存在検査までで、その旨を notes に載せる。
    """
    gov = load_governance(repo_path, governance_path)
    violations, checked = check_protected_commits(
        repo_path, gov, since_commit=since_commit, conn=conn
    )
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
        "decision_refs_verified": conn is not None,
        # 既知の限界は毎回開示する(独立役員審査条件)+ 個別の注記(登録漏れ・鮮度)。
        "notes": [
            *_coverage_notes(gov),
            *_staleness_note(repo_path),
            *([] if conn is not None else [
                "DB 接続なしの実行のため Approved トレーラの承認記録(否認済みか)は未照合"
            ]),
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
                "name": "⚠️ A-18-1 保護領域の無承認変更",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append(
            {
                "name": "A-18-1 保護領域突合",
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
                "name": "⚠️ A-18-2 文書⇔config 不整合",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append({"name": "A-18-2 文書⇔config 整合", "value": "✅ 一致", "inline": False})

    decls = result["declarations"]
    decl_lines = [f"- {d['rule']}" for d in decls] or ["なし"]
    fields.append(
        {
            "name": f"A-18-3 宣言のみ条文(執行点なし): {len(decls)} 件",
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
                "name": "⚠️ A-18-4 全変更 PR 化違反(直 push・非 PR マージ)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append(
            {
                "name": "A-18-4 全変更 PR 化",
                "value": (
                    f"✅ 直 push・非 PR マージなし(検査 {result['checked_first_parent']} コミット)"
                ),
                "inline": False,
            }
        )
    if result["notes"]:
        notes_value = "\n".join(f"- {n}" for n in result["notes"])[:1024]
        fields.append({"name": "注記", "value": notes_value, "inline": False})

    alert = has_findings(result)
    return {
        "title": ("⚠️ A-18 監査: 要対応の所見あり" if alert else "A-18 監査: 所見なし"),
        "description": (
            "規則⇔実装トレーサビリティ監査(定款第6条)。監査は read-only であり修正は行わない。"
        ),
        "color": COLOR_FLASH if alert else COLOR_NORMAL,
        # 監査報告の発信者 = 監査部門のキャラクター(台帳 org.yaml から役職キーで解決)。
        "author": org.author_for_role("audit"),
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
    """A-18 を実行し、所見があれば(または ``always_report``)#運営 へ enqueue する。

    ops-weekly など他ジョブからの呼び出し口。``dry_run`` では DB に接続せずログのみ
    (このとき ``Approved:`` トレーラの承認記録との突合は行われない — notes に開示する)。

    通常実行では**検査より先に接続を開き、その接続を検査へ渡す**。トレーラが指す
    ``governance.decisions`` の ID を ``current_decisions`` と突合するため
    (否認済みの承認を承認として受理しないため)であり、読取と警告投入を同一接続に
    まとめることで、検査時点と報告時点で承認状態が食い違う窓も狭くなる。
    """
    if dry_run:
        result = run_a18(repo_path, since_commit=since_commit, pr_since_commit=pr_since_commit)
        log.info(
            "[DRY_RUN] A-18 結果: violations=%d mismatches=%d declarations=%d "
            "direct_pushes=%d(enqueue %s)",
            len(result["violations"]), len(result["mismatches"]), len(result["declarations"]),
            len(result["direct_pushes"]),
            "対象" if (has_findings(result) or always_report) else "不要",
        )
        return result

    from ryza.db.conn import connect
    from ryza.provenance import start_run

    run = start_run("audit.a18", {"repo": str(repo_path)})
    conn = connect()
    try:
        result = run_a18(
            repo_path, since_commit=since_commit, pr_since_commit=pr_since_commit, conn=conn
        )
        if has_findings(result) or always_report:
            oid = enqueue_alert(conn, result, run.run_id)
            log.info("A-18 警告を enqueue: outbox_id=%s", oid)
        else:
            log.info("A-18: 所見なし(enqueue しない)")
        conn.commit()
        run.finish("success")
    except Exception:
        conn.rollback()
        run.finish("failed")
        raise
    finally:
        conn.close()
    return result


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 実行パス
    """CLI: ``python -m ryza.audit.a18 [--repo PATH] [--dry-run] [--always-report]``"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="A-18 規則⇔実装トレーサビリティ監査")
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
        f"A-18 完了(検査 {result['checked_commits']} コミット, 違反 {len(result['violations'])}, "
        f"不整合 {len(result['mismatches'])}, 宣言 {len(result['declarations'])}, "
        f"直push {len(result['direct_pushes'])})",
        file=sys.stderr,
    )
    return 1 if has_findings(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
