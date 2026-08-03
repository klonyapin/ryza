"""dashboard/github_api(計画ページの GitHub REST 取得)のテスト。

実ネットワークは呼ばない — ``_get_json`` を monkeypatch してレスポンス整形と
フィルタ(PR/Issue の区別・マージ済み判定・CI 集約)だけを検証する。
"""

from __future__ import annotations

import github_api
import pytest


def _patch(monkeypatch, payload):
    monkeypatch.setattr(github_api, "_get_json", lambda url, params=None: payload)


def test_fetch_open_issues_filters_out_pull_requests(monkeypatch):
    _patch(
        monkeypatch,
        [
            {
                "number": 1,
                "title": "本物の Issue",
                "created_at": "2026-08-01T00:00:00Z",
                "labels": [{"name": "user-action"}],
                "html_url": "https://example.test/1",
            },
            {
                "number": 2,
                "title": "PR は除外",
                "created_at": "2026-08-01T00:00:00Z",
                "labels": [],
                "html_url": "https://example.test/2",
                "pull_request": {},
            },
        ],
    )
    out = github_api.fetch_open_issues("o/r")
    assert [i["number"] for i in out] == [1]
    assert out[0]["labels"] == ["user-action"]


def test_fetch_open_pulls_extracts_head_sha(monkeypatch):
    _patch(
        monkeypatch,
        [
            {
                "number": 10,
                "title": "PR",
                "created_at": "2026-08-02T00:00:00Z",
                "draft": True,
                "head": {"sha": "abc123"},
                "html_url": "https://example.test/10",
            }
        ],
    )
    out = github_api.fetch_open_pulls("o/r")
    assert out == [
        {
            "number": 10,
            "title": "PR",
            "created_at": "2026-08-02T00:00:00Z",
            "draft": True,
            "head_sha": "abc123",
            "url": "https://example.test/10",
        }
    ]


def test_fetch_merged_pulls_excludes_unmerged(monkeypatch):
    _patch(
        monkeypatch,
        [
            {"number": 1, "title": "merged", "merged_at": "2026-08-01T00:00:00Z",
             "html_url": "u1"},
            {"number": 2, "title": "closed only", "merged_at": None, "html_url": "u2"},
        ],
    )
    out = github_api.fetch_merged_pulls("o/r")
    assert [p["number"] for p in out] == [1]


def test_fetch_closed_issues_requires_closed_at_and_not_pr(monkeypatch):
    _patch(
        monkeypatch,
        [
            {"number": 1, "title": "closed", "closed_at": "2026-08-01T00:00:00Z",
             "labels": [], "html_url": "u1"},
            {"number": 2, "title": "pr", "closed_at": "2026-08-01T00:00:00Z",
             "labels": [], "html_url": "u2", "pull_request": {}},
        ],
    )
    out = github_api.fetch_closed_issues("o/r")
    assert [i["number"] for i in out] == [1]


@pytest.mark.parametrize(
    ("check_runs", "expected"),
    [
        ([], "none"),
        ([{"status": "in_progress", "conclusion": None}], "pending"),
        ([{"status": "completed", "conclusion": "success"},
          {"status": "completed", "conclusion": "skipped"}], "success"),
        ([{"status": "completed", "conclusion": "failure"}], "failure"),
    ],
)
def test_fetch_ci_state_aggregation(monkeypatch, check_runs, expected):
    _patch(monkeypatch, {"check_runs": check_runs})
    assert github_api.fetch_ci_state("deadbeef", "o/r") == expected


# ── literal_md(第三者タイトルのリテラル化 — 独立役員審査 2026-08-03 低-9)──────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ふつうの Issue", "`ふつうの Issue`"),
        # リンク偽装(表示は正規サイト・遷移先は攻撃者)がリテラル化で無効になる
        ("[GitHub](https://evil.test)", "`[GitHub](https://evil.test)`"),
        # コードスパンからの脱出を狙うバッククォート → 区切りを1本長くする
        ("`code`", "`` `code` ``"),
        ("a`b", "``a`b``"),
        ("``x``", "``` ``x`` ```"),
        # 改行・連続空白は1行に潰す(リスト項目を壊さない)
        ("題名\n- 偽の箇条書き", "`題名 - 偽の箇条書き`"),
        ("", ""),
    ],
)
def test_literal_md_neutralizes_markdown(raw, expected):
    assert github_api.literal_md(raw) == expected


def test_literal_md_accepts_non_str():
    assert github_api.literal_md(123) == "`123`"
