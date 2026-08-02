"""GitHub REST API 薄クライアント(fine-grained PAT、stdlib urllib のみ)。

Cloud Run Job から ``git clone`` せず、Contents / Issues / Commits API だけで運用ジョブを
回すための最小クライアント。認証は Secret Manager の ``github-token``(fine-grained PAT)を
env 経由で受け取る。

書き込み系(Issue コメント・Issue 作成・ファイル更新)は ``dry_run=True`` のとき実行せず
ログのみ出す(T-004 §6 の DRY_RUN)。読み取り系は dry_run でも実行する(条件評価・ダイジェスト
集計に必要なため)。

依存を stdlib に閉じることで、テストは ``opener``(``urllib`` の OpenerDirector 互換)を差し替えて
ネットワークなしで検証できる。
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("ryza.ops.github")

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"


class GitHubError(RuntimeError):
    """GitHub API がエラー応答を返したとき。"""


class GitHubClient:
    """GitHub REST API の薄いラッパ。

    Parameters
    ----------
    token: fine-grained PAT。
    repo:  ``owner/name`` 形式。
    dry_run: True なら書き込み系を実行せずログのみ。
    api_base: テスト・GHE 用のベース URL 差し替え。
    opener: ``urllib.request.OpenerDirector`` 互換。テストで注入する。
    """

    def __init__(
        self,
        token: str,
        repo: str,
        *,
        dry_run: bool = False,
        api_base: str = _API_BASE,
        opener: Any | None = None,
    ) -> None:
        self.token = token
        self.repo = repo
        self.dry_run = dry_run
        self.api_base = api_base.rstrip("/")
        self._opener = opener or urllib.request.build_opener()

    # ── 低レベル ──────────────────────────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", _API_VERSION)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:  # pragma: no cover - ネットワーク依存
            detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
            raise GitHubError(f"{method} {path} -> {exc.code}: {detail}") from exc
        if not raw:
            return None
        return json.loads(raw)

    # ── 読み取り ──────────────────────────────────────────────────────────
    def get_file(self, path: str) -> tuple[str, str]:
        """Contents API でファイルを取得し (本文, blob sha) を返す。"""
        obj = self._request("GET", f"/repos/{self.repo}/contents/{path}")
        content = base64.b64decode(obj["content"]).decode("utf-8")
        return content, obj["sha"]

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        """Contents API でディレクトリ直下のエントリ一覧を返す(各 dict に path/name/type)。"""
        obj = self._request("GET", f"/repos/{self.repo}/contents/{path}")
        return obj if isinstance(obj, list) else []

    def list_issues(
        self,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Issue 一覧(PR は除外)。labels は AND、since は ISO8601。"""
        params: dict[str, Any] = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = since
        issues = self._request("GET", f"/repos/{self.repo}/issues", params=params) or []
        return [i for i in issues if "pull_request" not in i]

    def list_commits(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """コミット一覧。since は ISO8601(この時刻以降)。"""
        params: dict[str, Any] = {"per_page": 100}
        if since:
            params["since"] = since
        return self._request("GET", f"/repos/{self.repo}/commits", params=params) or []

    def list_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        """Issue のコメント一覧。"""
        return (
            self._request(
                "GET",
                f"/repos/{self.repo}/issues/{issue_number}/comments",
                params={"per_page": 100},
            )
            or []
        )

    # ── 書き込み(dry_run ガード) ───────────────────────────────────────────
    def create_issue_comment(self, issue_number: int, body: str) -> dict[str, Any] | None:
        if self.dry_run:
            log.info("[DRY_RUN] issue_comment #%s: %s", issue_number, body.splitlines()[0][:80])
            return None
        return self._request(
            "POST", f"/repos/{self.repo}/issues/{issue_number}/comments", body={"body": body}
        )

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any] | None:
        if self.dry_run:
            log.info("[DRY_RUN] issue_create: %s", title)
            return None
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return self._request("POST", f"/repos/{self.repo}/issues", body=payload)

    def update_file(
        self, path: str, content: str, message: str, sha: str
    ) -> dict[str, Any] | None:
        """Contents API でファイルを更新(コミット)する。sha は更新対象の現在の blob sha。"""
        if self.dry_run:
            log.info("[DRY_RUN] update_file %s: %s", path, message.splitlines()[0])
            return None
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": sha,
        }
        return self._request("PUT", f"/repos/{self.repo}/contents/{path}", body=body)
