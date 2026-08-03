"""週次運用ジョブ ops-weekly(経営管理部ジョブ第1号)。

GCE VM(ryza-bot)上の systemd timer で毎週動く(``ops/deploy-ops-weekly.sh``)。
2026-08-04 まで Cloud Run Job + Cloud Scheduler だったが、下記 7 の形骸化監査が
VM 内 PostgreSQL を読むため実行環境を VM へ移した(決議精緻化審査 懸念4 の配線)。
本モジュール自身はアプリ本体に依存せず、DB は opt-in の監査だけが触る:

1. ``ops/reminders.yaml``(v2)と ``docs/tasks/`` を GitHub Contents API で取得
2. 各リマインダーの conditions(OR)を評価し、``only_if``(AND ゲート)も満たせば action を発火
3. 発火後は reminders.yaml の status を ``"fired: <ISO日付>"`` に更新し contents API でコミット
4. 直近7日の commits / Issue 状態を集計し「週次ダイジェスト」Issue にコメント
5. 冪等: 既発火(status が fired)はスキップ、当週ダイジェストは二重投稿しない
6. DRY_RUN=1 で書き込みせずログのみ
7. 形骸化の監査(決議精緻化審査 2026-08-03 の裁定による新設統制。05-governance §6-5 の
   趣旨に連なる): 批判を経ない決議の直近件数・連続数をダイジェストに1行載せ、連続または
   累積が閾値に達したら警告する(opt-in)

条件エバリュエータ(reminders.yaml v2):
  date_after / issue_label_open / task_file_glob / bq_table_missing

環境変数:
  GITHUB_TOKEN  fine-grained PAT(DRY_RUN=1 以外では必須)
  GITHUB_REPO   owner/name
  DRY_RUN       "1" で書き込み抑止
  BOARDROOM_AUDIT "1" で決議の形骸化監査を有効化(DB へ届く実行環境でのみ設定する。
                VM 経路では ``ops/deploy-ops-weekly.sh`` が /etc/ryza/ops-weekly.env に置く)
"""

from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import yaml

from ryza.ops.github import GitHubClient

log = logging.getLogger("ryza.ops.weekly")

REMINDERS_PATH = "ops/reminders.yaml"
DIGEST_LABEL = "digest"
DIGEST_TITLE = "週次ダイジェスト"
CO_AUTHOR = "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"

# bq_table_missing(project, dataset, table|None) -> missing か
BqChecker = Callable[[str, str, "str | None"], bool]


# ────────────────────────────────────────────────────────────────────────────
# BigQuery(bq_table_missing の既定実装。テストでは差し替える)
# ────────────────────────────────────────────────────────────────────────────
def default_bq_table_missing(project: str, dataset: str, table: str | None = None) -> bool:
    """データセットにテーブルが無い(または指定テーブルが無い)なら True。

    google-cloud-bigquery は本条件でのみ使うため遅延インポートする(``.[ops]`` extra)。
    データセット自体が存在しない場合も「欠落」= True とみなす。
    """
    from google.cloud import bigquery  # 遅延インポート

    client = bigquery.Client(project=project)
    try:
        names = {t.table_id for t in client.list_tables(f"{project}.{dataset}")}
    except Exception:  # noqa: BLE001 - データセット不在等はすべて欠落扱い
        return True
    if table:
        return table not in names
    return len(names) == 0


# ────────────────────────────────────────────────────────────────────────────
# 条件エバリュエータ
# ────────────────────────────────────────────────────────────────────────────
def evaluate_condition(
    cond: dict[str, Any],
    client: GitHubClient,
    now: datetime,
    *,
    bq_checker: BqChecker,
) -> bool:
    """単一条件を評価する。未知 type は False(警告ログ)。"""
    ctype = cond.get("type")
    if ctype == "date_after":
        return now.date() >= date.fromisoformat(cond["date"])
    if ctype == "issue_label_open":
        return len(client.list_issues(state="open", labels=[cond["label"]])) > 0
    if ctype == "task_file_glob":
        glob = cond["glob"]
        entries = client.list_dir(posixpath.dirname(glob) or ".")
        return any(fnmatch.fnmatch(e.get("path", ""), glob) for e in entries)
    if ctype == "bq_table_missing":
        return bool(bq_checker(cond["project"], cond["dataset"], cond.get("table")))
    log.warning("未知の条件 type: %s", ctype)
    return False


def evaluate_conditions(
    conditions: list[dict[str, Any]],
    client: GitHubClient,
    now: datetime,
    *,
    bq_checker: BqChecker,
) -> bool:
    """conditions を OR で評価する。"""
    return any(evaluate_condition(c, client, now, bq_checker=bq_checker) for c in conditions)


# ────────────────────────────────────────────────────────────────────────────
# アクション実行
# ────────────────────────────────────────────────────────────────────────────
def execute_action(action: dict[str, Any], client: GitHubClient) -> None:
    """action を実行する(書き込み抑止は client 側の dry_run が担う)。"""
    atype = action.get("type")
    if atype == "issue_comment":
        client.create_issue_comment(action["issue"], action["body"])
    elif atype == "issue_create":
        client.create_issue(action["title"], action.get("body", ""), action.get("labels"))
    else:
        raise ValueError(f"未知の action type: {atype}")


# ────────────────────────────────────────────────────────────────────────────
# reminders.yaml の status 書き換え(コメント・整形を保つターゲット編集)
# ────────────────────────────────────────────────────────────────────────────
def set_reminder_status(text: str, reminder_id: str, status_value: str) -> str:
    """``reminder_id`` の status 行だけを ``status_value`` に置き換えた YAML テキストを返す。

    yaml.safe_dump による全体再シリアライズはコメント・整形を失うため、対象リマインダーの
    ブロック内の status 行のみをインデントを保ってテキスト置換する。
    """
    lines = text.splitlines(keepends=True)
    id_re = re.compile(rf"^\s*-\s*id:\s*{re.escape(reminder_id)}\s*$")
    next_id_re = re.compile(r"^\s*-\s*id:\s*")
    status_re = re.compile(r"^(\s*)status:\s*.*$")

    start = next((i for i, ln in enumerate(lines) if id_re.match(ln.rstrip("\n"))), None)
    if start is None:
        raise KeyError(f"reminder が見つからない: {reminder_id}")
    end = next(
        (j for j in range(start + 1, len(lines)) if next_id_re.match(lines[j])),
        len(lines),
    )
    for k in range(start, end):
        m = status_re.match(lines[k].rstrip("\n"))
        if m:
            lines[k] = f"{m.group(1)}status: {status_value}\n"
            return "".join(lines)
    raise KeyError(f"reminder {reminder_id} に status 行が無い")


# ────────────────────────────────────────────────────────────────────────────
# リマインダー発火
# ────────────────────────────────────────────────────────────────────────────
def _is_fired(status: Any) -> bool:
    return str(status or "").startswith("fired")


def fire_reminders(
    client: GitHubClient,
    doc: dict[str, Any],
    reminders_text: str,
    sha: str,
    now: datetime,
    *,
    bq_checker: BqChecker,
    reminders_path: str = REMINDERS_PATH,
) -> list[str]:
    """条件を満たす未発火リマインダーを発火し、発火した id 一覧を返す。

    発火ごとに reminders.yaml の status を更新して contents API でコミットする
    (メッセージ: ``chore(ops): reminder <id> fired`` + Co-Authored-By 行)。
    DRY_RUN 時は action もコミットも実行されない(client / 本関数の両方でガード)。
    """
    fired: list[str] = []
    current_text, current_sha = reminders_text, sha
    for r in doc.get("reminders", []):
        rid = r.get("id")
        if _is_fired(r.get("status")):
            continue  # 冪等性: 既発火はスキップ(二重発火しない)
        if not evaluate_conditions(r["conditions"], client, now, bq_checker=bq_checker):
            continue
        only_if = r.get("action", {}).get("only_if")
        if only_if and not evaluate_condition(only_if, client, now, bq_checker=bq_checker):
            continue

        log.info("リマインダー発火: %s", rid)
        execute_action(r["action"], client)
        fired.append(rid)

        current_text = set_reminder_status(
            current_text, rid, f'"fired: {now.date().isoformat()}"'
        )
        if client.dry_run:
            log.info("[DRY_RUN] reminders.yaml status 更新スキップ: %s", rid)
            continue
        resp = client.update_file(
            reminders_path,
            current_text,
            f"chore(ops): reminder {rid} fired\n\n{CO_AUTHOR}",
            current_sha,
        )
        if resp and isinstance(resp.get("content"), dict):
            current_sha = resp["content"].get("sha", current_sha)
    return fired


# ────────────────────────────────────────────────────────────────────────────
# 週次ダイジェスト
# ────────────────────────────────────────────────────────────────────────────
def iso_week(now: datetime) -> str:
    """ISO 週識別子(例 '2026-W31')。冪等性の週判定に使う。"""
    cal = now.isocalendar()
    return f"{cal[0]}-W{cal[1]:02d}"


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def stalled_impl_issues(open_issues: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """7日以上更新の無い実装(impl 系ラベル)Issue を返す。"""
    cutoff = now - timedelta(days=7)
    out: list[dict[str, Any]] = []
    for issue in open_issues:
        labels = {str(lb.get("name", "")).lower() for lb in issue.get("labels", [])}
        if not any("impl" in name for name in labels):
            continue
        updated = issue.get("updated_at")
        if updated and _parse_iso(updated) < cutoff:
            out.append(issue)
    return out


def find_or_create_digest_issue(client: GitHubClient) -> dict[str, Any] | None:
    """label='digest' の Issue を返す。無ければ作成(DRY_RUN 時は作らず None)。"""
    issues = client.list_issues(state="all", labels=[DIGEST_LABEL])
    if issues:
        return issues[0]
    return client.create_issue(
        DIGEST_TITLE, "週次進捗ダイジェストの集約先(ops-weekly が自動投稿)。", [DIGEST_LABEL]
    )


# A-18 監査の実行状態(未配線時の既定)。ダイジェストに必ず1行載せ、沈黙を多義的にしない。
A18_STATUS_UNWIRED = "スキップ(A18_REPO_PATH 未配線)"

# 決議の形骸化監査の実行状態(未配線時の既定)。A-18 と同じく
# **必ず1行載せる**: 「アラートが無い」と「そもそも見ていない」を沈黙で同一視させない。
RESOLUTION_STATUS_UNWIRED = "スキップ(BOARDROOM_AUDIT 未配線)"


def build_digest(
    client: GitHubClient,
    now: datetime,
    fired: list[str],
    marker: str,
    a18_status: str = A18_STATUS_UNWIRED,
    resolution_status: str = RESOLUTION_STATUS_UNWIRED,
) -> str:
    """ダイジェスト本文(Markdown)を組み立てる。先頭に当週マーカーを埋める(冪等判定用)。"""
    since = (now - timedelta(days=7)).isoformat()
    commits = client.list_commits(since=since)
    open_issues = client.list_issues(state="open")
    stalled = stalled_impl_issues(open_issues, now)

    lines = [marker, "", f"## 週次ダイジェスト {now.date().isoformat()} ({iso_week(now)})", ""]
    lines.append(f"### 今週のコミット: {len(commits)} 件")
    for c in commits[:10]:
        msg = (c.get("commit", {}).get("message", "") or "").splitlines()
        lines.append(f"- {msg[0] if msg else ''}")
    lines.append("")
    lines.append(f"### OPEN Issue: {len(open_issues)} 件")
    if stalled:
        lines.append("### ⚠ 停滞(7日更新なし)の実装 Issue")
        for i in stalled:
            lines.append(f"- #{i.get('number')} {i.get('title', '')}")
    lines.append("")
    lines.append(f"### 発火したリマインダー: {', '.join(fired) if fired else 'なし'}")
    lines.append("")
    # A-18 監査の実行状態(実行/スキップ(未配線)/失敗)は必ず明記する(独立役員審査条件)。
    lines.append(f"### A-18 監査: {a18_status}")
    lines.append("")
    # 形骸化の監査: 批判を経ない決議の直近件数・連続数(05 §6-5 の趣旨に連なる新設統制)。
    lines.append(f"### 決議の批判経由: {resolution_status}")
    return "\n".join(lines)


def post_digest(
    client: GitHubClient,
    now: datetime,
    fired: list[str],
    a18_status: str = A18_STATUS_UNWIRED,
    resolution_status: str = RESOLUTION_STATUS_UNWIRED,
) -> bool:
    """当週ダイジェストを投稿する。既に当週分があれば投稿しない(冪等)。投稿したら True。"""
    week = iso_week(now)
    marker = f"<!-- ops-weekly digest {week} -->"
    issue = find_or_create_digest_issue(client)
    if issue is None:
        log.info("[DRY_RUN] ダイジェスト Issue 未作成のため投稿スキップ (week=%s)", week)
        return False
    for c in client.list_issue_comments(issue["number"]):
        if marker in (c.get("body") or ""):
            log.info("当週ダイジェストは投稿済み: %s", week)
            return False
    body = build_digest(client, now, fired, marker, a18_status, resolution_status)
    client.create_issue_comment(issue["number"], body)
    return True


# ────────────────────────────────────────────────────────────────────────────
# エントリポイント
# ────────────────────────────────────────────────────────────────────────────
def run_weekly(
    client: GitHubClient,
    *,
    now: datetime | None = None,
    bq_checker: BqChecker = default_bq_table_missing,
    reminders_path: str = REMINDERS_PATH,
    a18_status: str = A18_STATUS_UNWIRED,
    resolution_status: str = RESOLUTION_STATUS_UNWIRED,
) -> list[str]:
    """週次ジョブ本体。発火したリマインダー id 一覧を返す。"""
    now = now or datetime.now(UTC)
    reminders_text, sha = client.get_file(reminders_path)
    doc = yaml.safe_load(reminders_text) or {}
    fired = fire_reminders(
        client, doc, reminders_text, sha, now,
        bq_checker=bq_checker, reminders_path=reminders_path,
    )
    post_digest(client, now, fired, a18_status, resolution_status)
    return fired


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    dry_run = os.environ.get("DRY_RUN") == "1"
    if not repo:
        raise SystemExit("GITHUB_REPO が未設定です(owner/name)")
    if not token and not dry_run:
        raise SystemExit("GITHUB_TOKEN が未設定です(DRY_RUN=1 以外では必須)")

    client = GitHubClient(token, repo, dry_run=dry_run)
    # A-18 はダイジェストより先に実行し、実行状態をダイジェストに必ず1行載せる。
    a18_status = run_a18_if_configured(dry_run=dry_run)
    resolution_status = resolution_audit_status()
    fired = run_weekly(client, a18_status=a18_status, resolution_status=resolution_status)
    log.info(
        "ops-weekly 完了。発火: %s / A-18: %s / 決議の批判経由: %s",
        fired or "なし", a18_status, resolution_status,
    )


def run_a18_if_configured(*, dry_run: bool) -> str:
    """A-18 監査(規則⇔実装トレーサビリティ)を週次で実行し、実行状態の1行を返す(opt-in)。

    A-18 は git 履歴(ローカル checkout)と DB(press.outbox)を必要とするため、両方に届く
    実行環境で ``A18_REPO_PATH`` を設定したときだけ走る。**現行の VM 経路では意図的に未設定**
    (= スキップ)である: A-18 は ``ops/deploy-a18.sh`` が設置する専用 timer が監査専用 clone
    ``/opt/ryza-audit`` から独立に実行し、結果は press.outbox 経由で #運営 へ出る。週次から
    稼働コード(/opt/ryza)を対象に走らせ直すと「デプロイ経路の改変が監査を無害化する」経路を
    再び開けてしまうため、ここでは配線しない。監査の失敗は握って週次ジョブ本体は継続するが、
    返す状態行(→週次ダイジェスト)とログに必ず残す(沈黙を多義的にしない)。
    """
    repo_path = os.environ.get("A18_REPO_PATH")
    if not repo_path:
        log.info("A-18 監査はスキップ(A18_REPO_PATH 未設定)")
        return A18_STATUS_UNWIRED
    from ryza.audit import a18

    try:
        # always_report=True: 受容済み既知違反(acknowledged)と PR 承継(inherited)は
        # has_findings に数えないため、所見ゼロの定常状態では報告自体が出ず「必ず可視化」が
        # 経路依存になっていた(独立役員審査 2026-08-04 中-4)。VM 経路(ops/deploy-a18.sh)は
        # 既に --always-report を渡しており、週次からの呼び出しもこれに揃える。
        result = a18.run_and_report(repo_path, dry_run=dry_run, always_report=True)
    except Exception as exc:
        log.exception("A-18 監査の実行に失敗(週次ジョブ自体は継続)")
        return f"失敗: {type(exc).__name__}: {exc}"
    status = (
        f"実行(違反 {len(result['violations'])} / 受容 {len(result.get('acknowledged') or [])} / "
        f"PR 承継 {len(result.get('inherited') or [])} / 不整合 {len(result['mismatches'])} / "
        f"宣言 {len(result['declarations'])})"
    )
    log.info("A-18 監査完了: %s", status)
    return status


def resolution_audit_status() -> str:
    """批判を経ない決議(確認付き+判定不能)の直近件数・連続数を1行で返す(opt-in)。

    決議精緻化審査(2026-08-03)の裁定による新設統制で、05-governance §6-5(形骸化の
    防止)が挙げる指標そのものではなくその趣旨に連なる同型の指標である。1件ごとの摩擦
    (``boardroom.CriticAbsentError`` と UI の明示確認)は「毎回チェックを外す」運用には
    無力なので、**列として**見る。連続が ``boardroom.CONFIRMATION_STREAK_ALERT`` 件に
    達するか、走査窓内の累積が ``boardroom.CONFIRMATION_COUNT_ALERT`` 件に達した週は
    行頭が ⚠ になる(交互に確認を外す運用は連続数だけでは検出できない)。

    DB(``governance.minute_resolutions``)を読むため、DB へ届く実行環境で
    ``BOARDROOM_AUDIT=1`` を設定したときだけ走る。現行の VM 経路(ryza-bot の
    systemd timer)は VM 内 PostgreSQL に届くため ``ops/deploy-ops-weekly.sh`` が
    ``/etc/ryza/ops-weekly.env`` で 1 を設定する。ローカル実行や DB の無い環境では
    未設定 = スキップになり、その事実も1行としてダイジェストに出す(A-18 と同じ流儀 —
    沈黙を多義的にしない)。失敗は握って週次ジョブ本体は継続する(監査の失敗で
    リマインダー発火まで止めない)。
    """
    if os.environ.get("BOARDROOM_AUDIT") != "1":
        log.info("決議の形骸化監査はスキップ(BOARDROOM_AUDIT 未設定)")
        return RESOLUTION_STATUS_UNWIRED
    from ryza.db.conn import connect
    from ryza.governance.boardroom import (
        confirmation_status_line,
        resolution_confirmation_stats,
    )

    try:
        with connect() as conn:
            stats = resolution_confirmation_stats(conn)
    except Exception as exc:  # noqa: BLE001 - 監査の失敗で週次ジョブを止めない
        log.exception("決議の形骸化監査に失敗(週次ジョブ自体は継続)")
        return f"失敗: {type(exc).__name__}: {exc}"
    line = confirmation_status_line(stats)
    log.info("決議の形骸化監査: %s", line)
    return line


if __name__ == "__main__":
    main()
