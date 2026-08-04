"""独立役員意見書の front matter パーサ(src/ryza/reviews.py)のテスト。

DB を使わない純ロジック。**旧様式(front matter 無し)を壊さないこと**と、
**様式不備を「旧様式」に読み替えないこと**の両方を固定する — 前者を壊すと過去の意見書が
一斉に無効になり、後者を許すと YAML を壊すことが統制の回避策になる。
"""

from __future__ import annotations

import pytest

from ryza.reviews import (
    ReviewArtifact,
    ReviewArtifactError,
    is_repo_path_ref,
    load_review_artifact,
    parse_review_artifact,
    split_front_matter,
)

SHA = "0123456789abcdef0123456789abcdef01234567"

NEW_STYLE = f"""---
reviewed_sha: {SHA}
review_date: 2026-08-04
verdict: conditional_approve
---

# 独立役員意見書 — サンプル

本文。
"""

OLD_STYLE = """# 独立役員意見書(旧様式)

- 審査日: 2026-08-03

---

区切り線は front matter ではない。
"""


# ── 新様式 ───────────────────────────────────────────────────────────────
def test_front_matter_is_parsed():
    art = parse_review_artifact(NEW_STYLE)
    assert isinstance(art, ReviewArtifact)
    assert art.reviewed_sha == SHA
    assert art.review_date == "2026-08-04"
    assert art.verdict == "conditional_approve"
    assert art.warnings == ()


def test_reviewed_sha_is_normalized_to_lowercase():
    """大文字表記で書かれても A-18-8 の突合が誤検出にならないよう小文字へ揃える。"""
    art = parse_review_artifact(f"---\nreviewed_sha: {SHA.upper()}\n---\n本文\n")
    assert art.reviewed_sha == SHA


def test_body_is_separated_from_the_front_matter():
    raw, body = split_front_matter(NEW_STYLE)
    assert "reviewed_sha" in raw
    assert body.lstrip().startswith("# 独立役員意見書")


# ── 旧様式(遡及改変しない前提の後方互換)─────────────────────────────────
def test_old_style_document_has_no_front_matter():
    """既存の意見書は無改変のまま「front matter 無し」= None として扱う。"""
    assert parse_review_artifact(OLD_STYLE) is None


def test_a_horizontal_rule_in_the_body_is_not_front_matter():
    """本文中の `---`(区切り線)を front matter と誤読しない — 先頭行のみが開始フェンス。"""
    assert split_front_matter(OLD_STYLE) == (None, OLD_STYLE)


# ── 様式不備は fail-safe(「旧様式」に読み替えない)──────────────────────────
def test_unclosed_front_matter_is_an_error():
    """閉じフェンスを消すだけで審査記録を消せる、という抜け道を作らない。"""
    with pytest.raises(ReviewArtifactError, match="閉じフェンス"):
        parse_review_artifact(f"---\nreviewed_sha: {SHA}\n\n本文\n")


def test_broken_yaml_is_an_error():
    with pytest.raises(ReviewArtifactError, match="YAML"):
        parse_review_artifact("---\nreviewed_sha: [unclosed\n---\n本文\n")


def test_non_mapping_front_matter_is_an_error():
    with pytest.raises(ReviewArtifactError, match="マッピング"):
        parse_review_artifact("---\n- a\n- b\n---\n本文\n")


def test_empty_front_matter_is_an_error():
    with pytest.raises(ReviewArtifactError, match="空"):
        parse_review_artifact("---\n---\n本文\n")


@pytest.mark.parametrize("bad", ["abc123", SHA[:12], "z" * 40, 12345])
def test_short_or_invalid_reviewed_sha_is_an_error(bad):
    """短縮 SHA は「一致とも不一致とも言えない」第三の状態を作るため受け付けない。"""
    with pytest.raises(ReviewArtifactError, match="40 桁 hex"):
        parse_review_artifact(f"---\nreviewed_sha: {bad}\n---\n本文\n")


# ── 欠落・語彙外は**警告**にとどめる(発効を止めない)────────────────────────
def test_missing_reviewed_sha_is_a_warning_not_an_error():
    """欠落を致命にすると「front matter ごと消せば通る」という逆インセンティブになる。"""
    art = parse_review_artifact("---\nverdict: approve\n---\n本文\n")
    assert art.reviewed_sha is None
    assert any("reviewed_sha" in w for w in art.warnings)


def test_unknown_verdict_is_a_warning():
    art = parse_review_artifact(f"---\nreviewed_sha: {SHA}\nverdict: とても良い\n---\n本文\n")
    assert art.verdict == "とても良い"
    assert any("語彙外" in w for w in art.warnings)


def test_malformed_review_date_is_a_warning():
    art = parse_review_artifact(f"---\nreviewed_sha: {SHA}\nreview_date: 昨日\n---\n本文\n")
    assert art.review_date == "昨日"
    assert any("review_date" in w for w in art.warnings)


# ── 参照の解決 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "ref", ["https://github.com/x/y/pull/1", "#承認", "/etc/passwd", "../outside.md", "", None]
)
def test_non_repo_path_references_are_not_read(ref):
    """URL・絶対パス・親ディレクトリ参照はリポジトリ内の審査記録として扱わない。"""
    assert not is_repo_path_ref(ref)


def test_load_reads_a_repository_relative_path(tmp_path):
    (tmp_path / "docs" / "reviews").mkdir(parents=True)
    (tmp_path / "docs" / "reviews" / "x.md").write_text(NEW_STYLE, encoding="utf-8")
    art = load_review_artifact("docs/reviews/x.md", repo_root=tmp_path)
    assert art.reviewed_sha == SHA and art.path.name == "x.md"


def test_load_returns_none_for_a_missing_file(tmp_path):
    assert load_review_artifact("docs/reviews/none.md", repo_root=tmp_path) is None


def test_load_returns_none_without_a_repo_root():
    """ルートを決められない実行(パッケージ設置)では検査そのものを行わない。"""
    assert load_review_artifact("docs/reviews/x.md", repo_root=None) is None
