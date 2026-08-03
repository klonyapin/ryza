"""dashboard/github_api — タスクページの GitHub REST 取得(無認証・streamlit 非依存)。

gh CLI ではなく REST API を requests で直接叩く(VM に gh を入れない・認証情報を
持たせないため)。無認証の public repo 読み取りはレート制限が 60 req/h と厳しいので、
呼び出し側(app.py)は ``st.cache_data(ttl=60)`` で 60 秒キャッシュする。

取得するのはタイトル・番号・日付・URL のみ(機微情報なし)。失敗(レート制限・
非公開リポジトリ・ネットワーク断)は例外を上げ、UI 側が警告表示に変換する。
"""

from __future__ import annotations

import os
from typing import Any

import requests

API_BASE = "https://api.github.com"
DEFAULT_REPO = os.environ.get("RYZA_GITHUB_REPO", "klonyapin/ryza")
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_TIMEOUT = 10.0


def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET して JSON を返す(テストはここを monkeypatch する)。"""
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_open_pulls(repo: str = DEFAULT_REPO, *, limit: int = 30) -> list[dict[str, Any]]:
    """open PR の一覧(番号・タイトル・作成日・draft・head SHA・URL)。"""
    items = _get_json(
        f"{API_BASE}/repos/{repo}/pulls",
        {"state": "open", "per_page": limit, "sort": "created", "direction": "desc"},
    )
    return [
        {
            "number": it["number"],
            "title": it["title"],
            "created_at": it["created_at"],
            "draft": bool(it.get("draft")),
            "head_sha": (it.get("head") or {}).get("sha"),
            "url": it["html_url"],
        }
        for it in items
    ]


def fetch_merged_pulls(repo: str = DEFAULT_REPO, *, limit: int = 30) -> list[dict[str, Any]]:
    """マージ済み PR の一覧(closed のうち merged_at があるもの・更新日降順)。"""
    items = _get_json(
        f"{API_BASE}/repos/{repo}/pulls",
        {"state": "closed", "per_page": limit, "sort": "updated", "direction": "desc"},
    )
    return [
        {
            "number": it["number"],
            "title": it["title"],
            "merged_at": it["merged_at"],
            "url": it["html_url"],
        }
        for it in items
        if it.get("merged_at")
    ]


def fetch_closed_issues(repo: str = DEFAULT_REPO, *, limit: int = 50) -> list[dict[str, Any]]:
    """closed Issue の一覧(更新日降順・PR は除外)。"""
    items = _get_json(
        f"{API_BASE}/repos/{repo}/issues",
        {"state": "closed", "per_page": limit, "sort": "updated", "direction": "desc"},
    )
    return [
        {
            "number": it["number"],
            "title": it["title"],
            "closed_at": it["closed_at"],
            "labels": [lb["name"] for lb in it.get("labels", [])],
            "url": it["html_url"],
        }
        for it in items
        if "pull_request" not in it and it.get("closed_at")
    ]


def fetch_ci_state(sha: str, repo: str = DEFAULT_REPO) -> str:
    """コミット SHA の CI(check-runs)集約状態: success|failure|pending|none。"""
    data = _get_json(f"{API_BASE}/repos/{repo}/commits/{sha}/check-runs", {"per_page": 50})
    runs = data.get("check_runs", [])
    if not runs:
        return "none"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    if all(r.get("conclusion") in ("success", "neutral", "skipped") for r in runs):
        return "success"
    return "failure"


def fetch_open_issues(repo: str = DEFAULT_REPO, *, limit: int = 50) -> list[dict[str, Any]]:
    """open Issue の一覧。GitHub の /issues は PR も返すため ``pull_request`` キーで除外。"""
    items = _get_json(
        f"{API_BASE}/repos/{repo}/issues",
        {"state": "open", "per_page": limit, "sort": "created", "direction": "desc"},
    )
    return [
        {
            "number": it["number"],
            "title": it["title"],
            "created_at": it["created_at"],
            "labels": [lb["name"] for lb in it.get("labels", [])],
            "url": it["html_url"],
        }
        for it in items
        if "pull_request" not in it
    ]
