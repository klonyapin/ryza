"""GitHubClient(stdlib urllib 薄クライアント)の単体テスト。

ネットワークを使わず、``opener``(urllib の OpenerDirector 互換)を差し替えて検証する:
- 認証・バージョンヘッダが付く
- Contents API のファイル取得で base64 デコードされる
- 書き込み系が dry_run で抑止される / 非 dry_run で正しいリクエストを出す
- Issue 一覧が PR を除外する
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ryza.ops.github import GitHubClient


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeOpener:
    """(method, path) -> JSON を返すフェイク。全リクエストを records に記録する。"""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.records: list[dict[str, Any]] = []

    def open(self, req: Any) -> _FakeResponse:
        # req.full_url からパス(クエリ除去)を取り出す。
        url = req.full_url
        path = url.split("api.github.com", 1)[-1].split("?", 1)[0]
        body = json.loads(req.data.decode()) if req.data else None
        self.records.append(
            {
                "method": req.get_method(),
                "path": path,
                "headers": dict(req.header_items()),
                "body": body,
            }
        )
        payload = self.routes.get((req.get_method(), path), None)
        return _FakeResponse(json.dumps(payload).encode("utf-8") if payload is not None else b"")


def _client(routes, **kw) -> tuple[GitHubClient, FakeOpener]:
    opener = FakeOpener(routes)
    return GitHubClient("tok", "acme/ryza", opener=opener, **kw), opener


def test_headers_and_auth():
    c, op = _client({("GET", "/repos/acme/ryza/commits"): []})
    c.list_commits()
    rec = op.records[-1]
    # urllib は追加ヘッダのキーを capitalize する。
    headers = {k.lower(): v for k, v in rec["headers"].items()}
    assert headers["authorization"] == "Bearer tok"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["accept"] == "application/vnd.github+json"


def test_get_file_decodes_base64():
    content = "version: 2\nreminders: []\n"
    routes = {
        ("GET", "/repos/acme/ryza/contents/ops/reminders.yaml"): {
            "content": base64.b64encode(content.encode()).decode(),
            "sha": "abc123",
        }
    }
    c, _ = _client(routes)
    text, sha = c.get_file("ops/reminders.yaml")
    assert text == content
    assert sha == "abc123"


def test_list_issues_excludes_pull_requests():
    routes = {
        ("GET", "/repos/acme/ryza/issues"): [
            {"number": 1, "title": "issue"},
            {"number": 2, "title": "pr", "pull_request": {"url": "x"}},
        ]
    }
    c, _ = _client(routes)
    issues = c.list_issues()
    assert [i["number"] for i in issues] == [1]


def test_write_methods_suppressed_in_dry_run():
    c, op = _client({}, dry_run=True)
    assert c.create_issue_comment(7, "hi") is None
    assert c.create_issue("t", "b", ["digest"]) is None
    assert c.update_file("ops/reminders.yaml", "x", "msg", "sha") is None
    # dry_run では書き込みリクエストが opener に一切届かない。
    assert op.records == []


def test_update_file_sends_base64_and_sha():
    routes = {("PUT", "/repos/acme/ryza/contents/ops/reminders.yaml"): {"content": {"sha": "new"}}}
    c, op = _client(routes)
    resp = c.update_file("ops/reminders.yaml", "hello", "chore(ops): x", "oldsha")
    assert resp["content"]["sha"] == "new"
    rec = op.records[-1]
    assert rec["method"] == "PUT"
    assert rec["body"]["sha"] == "oldsha"
    assert base64.b64decode(rec["body"]["content"]).decode() == "hello"
    assert rec["body"]["message"] == "chore(ops): x"


def test_create_issue_comment_posts_body():
    routes = {("POST", "/repos/acme/ryza/issues/8/comments"): {"id": 1}}
    c, op = _client(routes)
    c.create_issue_comment(8, "本文")
    rec = op.records[-1]
    assert rec["method"] == "POST"
    assert rec["body"] == {"body": "本文"}
