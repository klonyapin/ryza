"""週次運用ジョブ ops-weekly の単体・統合テスト。

- 条件エバリュエータ4種を真偽両方で検証(フィクスチャ)
- 冪等性: 既発火リマインダーの二重発火なし / 当週ダイジェストの二重投稿なし /
  同週2回実行で書き込みが増えない
- DRY_RUN=1 のエンドツーエンド(GitHub API はモック、書き込みが発生しない)
- reminders.yaml の status ターゲット書き換え(コメント・整形を保つ)
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ryza.ops import weekly
from ryza.ops.github import GitHubClient

NOW = datetime(2026, 8, 2, 1, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]

# 小さな合成 reminders.yaml(発火するもの/しないもの/既発火 を1つずつ)
SYNTH_REMINDERS = """\
version: 2
reminders:
  # 発火する(過去日)
  - id: fires-now
    what: "テスト: 過去日で発火"
    conditions:
      - type: date_after
        date: "2020-01-01"
    action:
      type: issue_comment
      issue: 5
      body: "発火テスト"
    status: pending

  # まだ発火しない(未来日)
  - id: not-yet
    what: "テスト: 未来日"
    conditions:
      - type: date_after
        date: "2999-01-01"
    action:
      type: issue_comment
      issue: 6
      body: "まだ"
    status: pending

  # 既発火(冪等性: スキップされる)
  - id: already-fired
    what: "テスト: 既発火"
    conditions:
      - type: date_after
        date: "2020-01-01"
    action:
      type: issue_comment
      issue: 7
      body: "再発火しない"
    status: "fired: 2026-01-05"
"""


class StubClient:
    """weekly.* が使う高レベル GitHub API のフェイク。

    reminders.yaml を ``files`` に保持し update_file で永続化する(2回目の run で
    fired 済み状態が反映される)。dry_run 時は書き込みを実行せず記録もしない
    (実クライアントの契約を模す。実クライアントの dry_run は test_github で別途検証)。
    """

    def __init__(
        self,
        *,
        reminders_text: str = SYNTH_REMINDERS,
        issues: list[dict[str, Any]] | None = None,
        dir_entries: list[dict[str, Any]] | None = None,
        commits: list[dict[str, Any]] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.dry_run = dry_run
        self.files: dict[str, tuple[str, str]] = {"ops/reminders.yaml": (reminders_text, "sha0")}
        self._issues = issues if issues is not None else []
        self._dir = dir_entries or []
        self._commits = commits or []
        self._comments: dict[int, list[dict[str, Any]]] = {}
        self.comments_posted: list[tuple[int, str]] = []
        self.issues_created: list[dict[str, Any]] = []
        self.files_updated: list[tuple[str, str]] = []
        self._sha_seq = 0

    # 読み取り
    def get_file(self, path: str) -> tuple[str, str]:
        return self.files[path]

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        return self._dir

    def list_issues(self, *, state="open", labels=None, since=None):
        out = []
        for i in self._issues:
            if state != "all" and i.get("state", "open") != state:
                continue
            if labels:
                names = {lb["name"] for lb in i.get("labels", [])}
                if not set(labels) <= names:
                    continue
            out.append(i)
        return out

    def list_commits(self, *, since=None):
        return self._commits

    def list_issue_comments(self, issue_number: int):
        return self._comments.get(issue_number, [])

    # 書き込み(dry_run ガード)
    def create_issue_comment(self, issue_number: int, body: str):
        if self.dry_run:
            return None
        self.comments_posted.append((issue_number, body))
        self._comments.setdefault(issue_number, []).append({"body": body})
        return {"id": len(self.comments_posted)}

    def create_issue(self, title: str, body: str, labels=None):
        if self.dry_run:
            return None
        issue = {
            "number": 900 + len(self.issues_created),
            "title": title,
            "state": "open",
            "labels": [{"name": x} for x in (labels or [])],
        }
        self.issues_created.append(issue)
        self._issues.append(issue)
        return issue

    def update_file(self, path: str, content: str, message: str, sha: str):
        if self.dry_run:
            return None
        self._sha_seq += 1
        new_sha = f"sha{self._sha_seq}"
        self.files[path] = (content, new_sha)
        self.files_updated.append((path, message))
        return {"content": {"sha": new_sha}}


def _true_bq(*_a):
    return True


def _false_bq(*_a):
    return False


# ── 条件エバリュエータ4種(真偽両方) ────────────────────────────────────────
def test_cond_date_after():
    client = StubClient()
    assert weekly.evaluate_condition(
        {"type": "date_after", "date": "2026-08-01"}, client, NOW, bq_checker=_false_bq
    )
    assert not weekly.evaluate_condition(
        {"type": "date_after", "date": "2026-08-03"}, client, NOW, bq_checker=_false_bq
    )


def test_cond_issue_label_open():
    issues = [{"number": 8, "state": "open", "labels": [{"name": "execution-layer"}]}]
    client = StubClient(issues=issues)
    assert weekly.evaluate_condition(
        {"type": "issue_label_open", "label": "execution-layer"}, client, NOW, bq_checker=_false_bq
    )
    assert not weekly.evaluate_condition(
        {"type": "issue_label_open", "label": "nonexistent"}, client, NOW, bq_checker=_false_bq
    )


def test_cond_task_file_glob():
    with_broker = StubClient(dir_entries=[{"path": "docs/tasks/T-010-broker-adapter.md"}])
    without = StubClient(dir_entries=[{"path": "docs/tasks/T-001-repo-and-db.md"}])
    cond = {"type": "task_file_glob", "glob": "docs/tasks/*broker*"}
    assert weekly.evaluate_condition(cond, with_broker, NOW, bq_checker=_false_bq)
    assert not weekly.evaluate_condition(cond, without, NOW, bq_checker=_false_bq)


def test_cond_bq_table_missing():
    client = StubClient()
    cond = {"type": "bq_table_missing", "project": "ryza-main", "dataset": "billing_export"}
    assert weekly.evaluate_condition(cond, client, NOW, bq_checker=_true_bq)
    assert not weekly.evaluate_condition(cond, client, NOW, bq_checker=_false_bq)


def test_conditions_are_or():
    client = StubClient()
    conds = [
        {"type": "date_after", "date": "2999-01-01"},  # False
        {"type": "date_after", "date": "2000-01-01"},  # True
    ]
    assert weekly.evaluate_conditions(conds, client, NOW, bq_checker=_false_bq)


# ── only_if(AND ゲート): billing-export-verify 型 ───────────────────────────
def test_only_if_gate_blocks_when_false():
    # date_after は True だが only_if(bq_table_missing)が False → 発火しない
    text = """\
version: 2
reminders:
  - id: billing
    what: "x"
    conditions:
      - type: date_after
        date: "2020-01-01"
    action:
      type: issue_comment
      issue: 7
      body: "警告"
      only_if:
        type: bq_table_missing
        project: p
        dataset: d
    status: pending
"""
    client = StubClient(reminders_text=text)
    doc = weekly.yaml.safe_load(text)
    assert weekly.fire_reminders(client, doc, text, "sha0", NOW, bq_checker=_false_bq).fired == []
    fired2 = weekly.fire_reminders(client, doc, text, "sha0", NOW, bq_checker=_true_bq)
    assert fired2.fired == ["billing"]


# ── set_reminder_status: ターゲット書き換え ─────────────────────────────────
def test_set_reminder_status_targeted():
    real = (REPO_ROOT / "ops" / "reminders.yaml").read_text(encoding="utf-8")
    updated = weekly.set_reminder_status(real, "ips-quarterly-review", '"fired: 2026-08-02"')
    # 対象だけ fired、他は pending のまま。
    doc = weekly.yaml.safe_load(updated)
    by_id = {r["id"]: r for r in doc["reminders"]}
    assert by_id["ips-quarterly-review"]["status"] == "fired: 2026-08-02"
    assert by_id["ibkr-account-application"]["status"] == "pending"
    assert by_id["billing-export-verify"]["status"] == "pending"
    # コメント行が保たれている(全体再シリアライズしていない)。
    assert "# conditions は OR 評価" in updated


# ── 発火 + 冪等性(既発火スキップ) ──────────────────────────────────────────
def test_fire_skips_already_fired_and_future():
    client = StubClient()
    doc = weekly.yaml.safe_load(SYNTH_REMINDERS)
    outcome = weekly.fire_reminders(
        client, doc, SYNTH_REMINDERS, "sha0", NOW, bq_checker=_false_bq
    )
    assert outcome.fired == ["fires-now"]  # not-yet(未来)・already-fired(既発火)は除外
    assert outcome.failures == []
    assert client.comments_posted == [(5, "発火テスト")]
    # reminders.yaml が1回コミットされ、status が fired になっている。
    assert len(client.files_updated) == 1
    new_text, _ = client.files["ops/reminders.yaml"]
    by_id = {r["id"]: r for r in weekly.yaml.safe_load(new_text)["reminders"]}
    assert by_id["fires-now"]["status"].startswith("fired")


# ── 冪等性: 同週2回実行で書き込みが増えない ─────────────────────────────────
def test_run_weekly_idempotent_across_runs():
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue])
    weekly.run_weekly(client, now=NOW, bq_checker=_false_bq)
    writes_after_1 = (len(client.files_updated), len(client.comments_posted))
    # 1回目: reminder 1件コミット + ダイジェスト1件コメント。
    assert writes_after_1 == (1, 2)  # files_update=1, comments=(発火issue5)+(ダイジェスト)=2

    weekly.run_weekly(client, now=NOW, bq_checker=_false_bq)
    writes_after_2 = (len(client.files_updated), len(client.comments_posted))
    # 2回目: 既発火スキップ + 当週ダイジェスト済み → 追加書き込みなし。
    assert writes_after_2 == writes_after_1


# ── 冪等性: ダイジェスト二重投稿なし ────────────────────────────────────────
def test_digest_not_double_posted():
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue])
    assert weekly.post_digest(client, NOW, fired=[]) is True
    assert weekly.post_digest(client, NOW, fired=[]) is False
    assert len(client.comments_posted) == 1


def test_digest_created_when_missing():
    client = StubClient(issues=[])  # digest Issue が無い
    posted = weekly.post_digest(client, NOW, fired=["x"])
    assert posted is True
    assert len(client.issues_created) == 1
    assert client.issues_created[0]["labels"] == [{"name": "digest"}]


# ── DRY_RUN エンドツーエンド(StubClient): 書き込みが発生しない ────────────────
def test_dry_run_end_to_end_no_writes():
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue], dry_run=True)
    outcome = weekly.run_weekly(client, now=NOW, bq_checker=_true_bq)
    # 評価・発火判定は行われる(fires-now が対象)。
    assert outcome.fired == ["fires-now"]
    # だが書き込みは一切行われない。
    assert client.comments_posted == []
    assert client.issues_created == []
    assert client.files_updated == []


# ── DRY_RUN エンドツーエンド(実クライアント): ネットワーク書き込みが出ない ─────
class _Resp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


class _PathOpener:
    def __init__(self, routes):
        self.routes = routes
        self.records = []

    def open(self, req):
        path = req.full_url.split("api.github.com", 1)[-1].split("?", 1)[0]
        self.records.append({"method": req.get_method(), "path": path})
        payload = self.routes.get(("GET", path))
        return _Resp(json.dumps(payload).encode() if payload is not None else b"")


def test_dry_run_real_client_makes_no_write_requests():
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    routes = {
        ("GET", "/repos/acme/ryza/contents/ops/reminders.yaml"): {
            "content": base64.b64encode(SYNTH_REMINDERS.encode()).decode(),
            "sha": "sha0",
        },
        ("GET", "/repos/acme/ryza/issues"): [digest_issue],
        ("GET", "/repos/acme/ryza/issues/9/comments"): [],
        ("GET", "/repos/acme/ryza/commits"): [],
    }
    opener = _PathOpener(routes)
    client = GitHubClient("tok", "acme/ryza", dry_run=True, opener=opener)
    outcome = weekly.run_weekly(client, now=NOW, bq_checker=_true_bq)
    assert outcome.fired == ["fires-now"]
    # opener に届いたのは GET のみ(POST/PUT なし)。
    assert all(r["method"] == "GET" for r in opener.records)


# ── A-18 実行状態行(独立役員審査条件: 沈黙を多義的にしない)──────────────────
def test_digest_always_contains_a18_status_line():
    """a18_status を渡さなくてもダイジェストに必ず A-18 行(未配線)が載る。"""
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue])
    assert weekly.post_digest(client, NOW, fired=[]) is True
    body = client.comments_posted[0][1]
    assert f"### A-18 監査: {weekly.A18_STATUS_UNWIRED}" in body


def test_digest_carries_custom_a18_status():
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue])
    status = "実行(違反 0 / 不整合 0 / 宣言 3)"
    assert weekly.post_digest(client, NOW, fired=[], a18_status=status) is True
    assert f"### A-18 監査: {status}" in client.comments_posted[0][1]


def test_run_a18_if_configured_unwired(monkeypatch):
    monkeypatch.delenv("A18_REPO_PATH", raising=False)
    assert weekly.run_a18_if_configured(dry_run=True) == weekly.A18_STATUS_UNWIRED


def test_run_a18_always_reports_and_shows_acknowledged_and_inherited(monkeypatch, tmp_path):
    """週次からの A-18 呼び出しは always_report=True(受容・承継が定常状態でも毎週見える)。

    受容(acknowledged)と PR 承継(inherited)は has_findings に数えないため、所見ゼロでは
    報告自体が出ず「必ず可視化」が経路依存になっていた(独立役員審査 2026-08-04 中-4)。
    """
    from ryza.audit import a18

    captured: dict = {}

    def fake_run_and_report(repo_path, **kwargs):
        captured.update(kwargs)
        return {
            "violations": [], "mismatches": [], "declarations": [{"rule": "x"}],
            "acknowledged": [{"commit": "abc123def456"}],
            "inherited": [{"commit": "def456abc123"}, {"commit": "111222333444"}],
        }

    monkeypatch.setattr(a18, "run_and_report", fake_run_and_report)
    monkeypatch.setenv("A18_REPO_PATH", str(tmp_path))
    status = weekly.run_a18_if_configured(dry_run=True)
    assert captured["always_report"] is True
    assert "受容 1" in status and "PR 承継 2" in status


def test_run_a18_if_configured_failure_is_reported(monkeypatch, tmp_path):
    """A-18 の失敗は握るが、状態行に「失敗」として必ず現れる(週次ジョブは継続)。"""
    monkeypatch.setenv("A18_REPO_PATH", str(tmp_path))  # git リポジトリでない → 失敗
    status = weekly.run_a18_if_configured(dry_run=True)
    assert status.startswith("失敗:")


# ── 決議の形骸化監査(05-governance §6-5 の趣旨に連なる新設統制)──────────────
# 本番の判定主体は A-18-6(tests/audit/test_a18.py)。ここで固定するのは週次側の表示契約:
# 行は必ず載り、既定は「スキップ」ではなく報告先への参照であること。
def test_digest_always_contains_resolution_status_line():
    """判定を持たない実行でも「決議の批判経由」行は必ず載る(沈黙させない)。"""
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue])
    assert weekly.post_digest(client, NOW, fired=[]) is True
    body = client.comments_posted[0][1]
    assert f"### 決議の批判経由: {weekly.RESOLUTION_STATUS_DELEGATED}" in body


def test_default_resolution_line_points_at_the_reporter_not_skip():
    """既定行は「スキップ」ではなく報告先(A-18-6)の参照であること。

    「スキップ」を常設すると、統制が外された状態と正常状態が同じ文字列になり、
    無害化が正常表示に化ける(ops-weekly VM 移設審査 2026-08-04 重大-2 と同型)。
    """
    assert "スキップ" not in weekly.RESOLUTION_STATUS_DELEGATED
    assert "A-18-6" in weekly.RESOLUTION_STATUS_DELEGATED


def test_digest_carries_resolution_alert():
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue])
    status = (
        "⚠ 形骸化の疑い(連続 3 件以上): 直近 3 件中 3 件が批判を経ない決議"
        "(確認付き 3 / 判定不能 0)/ 連続 3 件"
    )
    assert weekly.post_digest(client, NOW, fired=[], resolution_status=status) is True
    assert f"### 決議の批判経由: {status}" in client.comments_posted[0][1]


def test_resolution_audit_status_delegates_by_default(monkeypatch):
    """opt-in が無い実行では判定せず、報告先(A-18-6)の参照行を返す。"""
    monkeypatch.delenv("BOARDROOM_AUDIT", raising=False)
    assert weekly.resolution_audit_status() == weekly.RESOLUTION_STATUS_DELEGATED


def test_resolution_audit_status_failure_is_reported(monkeypatch):
    """DB へ届かない実行環境でも週次ジョブは止めず、状態行に「失敗」を出す。"""
    monkeypatch.setenv("BOARDROOM_AUDIT", "1")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no db")

    monkeypatch.setattr("ryza.db.conn.connect", _boom)
    assert weekly.resolution_audit_status().startswith("失敗: OSError")


# ── action: notify(ops/reminders.yaml の様式: type / channel / body)──────────
NOTIFY_REMINDERS = """\
version: 2
reminders:
  - id: notify-now
    what: "テスト: notify 型"
    conditions:
      - type: date_after
        date: "2020-01-01"
    action:
      type: notify
      channel: 運営
      body: "OAuth クライアントの紐付けを確認する。"
    status: pending
"""


def test_notify_action_fires_and_is_delivered():
    """notify 型が例外を投げずに配送され、status が fired へ遷移する。"""
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(reminders_text=NOTIFY_REMINDERS, issues=[digest_issue])
    doc = weekly.yaml.safe_load(NOTIFY_REMINDERS)
    outcome = weekly.fire_reminders(
        client, doc, NOTIFY_REMINDERS, "sha0", NOW, bq_checker=_false_bq
    )
    assert outcome.fired == ["notify-now"]
    assert outcome.failures == []
    # 配送先はこのジョブが持つ唯一の通知経路(ダイジェスト Issue のコメント)。
    assert len(client.comments_posted) == 1
    issue_number, body = client.comments_posted[0]
    assert issue_number == 9
    assert "運営" in body
    assert "OAuth クライアントの紐付けを確認する。" in body
    # status 遷移は他 action 型と同じ慣習("fired: <日付>")。
    new_text, _ = client.files["ops/reminders.yaml"]
    by_id = {r["id"]: r for r in weekly.yaml.safe_load(new_text)["reminders"]}
    assert by_id["notify-now"]["status"] == f"fired: {NOW.date().isoformat()}"


def test_notify_body_includes_optional_title():
    """様式上 title は任意(実在の 2 エントリは持たない)。あれば見出しに載せる。"""
    with_title = weekly.build_notify_body(
        {"type": "notify", "channel": "運営", "title": "確認", "body": "本文"}
    )
    assert "確認" in with_title and "本文" in with_title
    without_title = weekly.build_notify_body({"type": "notify", "channel": "運営", "body": "本文"})
    assert "本文" in without_title


# ── 終端 status(done)は発火対象から外れる ───────────────────────────────────
DONE_REMINDERS = """\
version: 2
reminders:
  - id: already-done
    what: "テスト: 完了済み(条件は充足するが再発火してはならない)"
    conditions:
      - type: date_after
        date: "2020-01-01"
    action:
      type: issue_create
      title: "誤発火"
      body: "発火してはならない"
    status: done
"""


def test_done_entries_do_not_fire():
    client = StubClient(reminders_text=DONE_REMINDERS)
    doc = weekly.yaml.safe_load(DONE_REMINDERS)
    outcome = weekly.fire_reminders(
        client, doc, DONE_REMINDERS, "sha0", NOW, bq_checker=_false_bq
    )
    assert outcome.fired == []
    assert client.issues_created == []
    assert client.files_updated == []


def test_real_reminders_yaml_done_entries_never_fire():
    """実ファイルの done エントリ(過去日条件を多数含む)が1件も発火しない。"""
    real = (REPO_ROOT / "ops" / "reminders.yaml").read_text(encoding="utf-8")
    doc = weekly.yaml.safe_load(real)
    done_ids = {r["id"] for r in doc["reminders"] if str(r.get("status", "")).startswith("done")}
    assert done_ids, "前提: 実ファイルに done エントリが存在する"
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(reminders_text=real, issues=[digest_issue])
    outcome = weekly.fire_reminders(client, doc, real, "sha0", NOW, bq_checker=_false_bq)
    assert not (set(outcome.fired) & done_ids)


# ── 1件の失敗がループ全体を止めない(失敗は黙殺せずサマリに載せる) ─────────────
MIXED_REMINDERS = """\
version: 2
reminders:
  - id: ok-before
    what: "テスト: 失敗エントリの前"
    conditions:
      - type: date_after
        date: "2020-01-01"
    action:
      type: issue_comment
      issue: 5
      body: "前"
    status: pending

  - id: broken
    what: "テスト: 未知 action type で失敗する"
    conditions:
      - type: date_after
        date: "2020-01-01"
    action:
      type: no_such_action
    status: pending

  - id: ok-after
    what: "テスト: 失敗エントリの後(処理が続くこと)"
    conditions:
      - type: date_after
        date: "2020-01-01"
    action:
      type: issue_comment
      issue: 6
      body: "後"
    status: pending
"""


def test_single_entry_failure_does_not_stop_the_loop():
    client = StubClient(reminders_text=MIXED_REMINDERS)
    doc = weekly.yaml.safe_load(MIXED_REMINDERS)
    outcome = weekly.fire_reminders(
        client, doc, MIXED_REMINDERS, "sha0", NOW, bq_checker=_false_bq
    )
    assert outcome.fired == ["ok-before", "ok-after"]
    assert [rid for rid, _ in outcome.failures] == ["broken"]
    assert "no_such_action" in outcome.failures[0][1]
    # 失敗エントリの status は据え置き(翌週再試行される)。
    new_text, _ = client.files["ops/reminders.yaml"]
    by_id = {r["id"]: r for r in weekly.yaml.safe_load(new_text)["reminders"]}
    assert by_id["broken"]["status"] == "pending"


def test_digest_always_contains_failure_line():
    """失敗ゼロでも行は必ず載る(沈黙を多義的にしない — A-18 行と同じ流儀)。"""
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue])
    assert weekly.post_digest(client, NOW, fired=[]) is True
    assert "### 失敗したリマインダー: なし" in client.comments_posted[0][1]


def test_digest_lists_failed_reminder_ids():
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(issues=[digest_issue])
    failures = [("broken", "ValueError: 未知の action type: no_such_action")]
    assert weekly.post_digest(client, NOW, fired=[], failures=failures) is True
    body = client.comments_posted[0][1]
    assert "### ⚠ 失敗したリマインダー: 1 件" in body
    assert "broken" in body and "no_such_action" in body


def test_run_weekly_reports_failures_in_digest():
    """run_weekly 経由でも失敗が週次サマリ通知に載る(発火は継続する)。"""
    digest_issue = {"number": 9, "state": "open", "labels": [{"name": "digest"}]}
    client = StubClient(reminders_text=MIXED_REMINDERS, issues=[digest_issue])
    outcome = weekly.run_weekly(client, now=NOW, bq_checker=_false_bq)
    assert outcome.fired == ["ok-before", "ok-after"]
    assert [rid for rid, _ in outcome.failures] == ["broken"]
    digest_body = client.comments_posted[-1][1]
    assert "### ⚠ 失敗したリマインダー: 1 件" in digest_body
    assert "broken" in digest_body
