"""独立役員意見書の front matter パーサ(src/ryza/reviews.py)のテスト。

DB を使わない純ロジック。**旧様式(front matter 無し)を壊さないこと**と、
**様式不備を「旧様式」に読み替えないこと**の両方を固定する — 前者を壊すと過去の意見書が
一斉に無効になり、後者を許すと YAML を壊すことが統制の回避策になる。
"""

from __future__ import annotations

import pytest

from ryza.reviews import (
    BLOCKING_VERDICTS,
    ReviewArtifact,
    ReviewArtifactError,
    first_commit_date,
    is_repo_path_ref,
    load_review_artifact,
    parse_review_artifact,
    resolve_review_path,
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
    "ref", ["https://github.com/x/y/pull/1", "http://a/b/c", "#承認", "/etc/passwd", "", None]
)
def test_non_repo_path_references_are_not_read(ref):
    """URL・絶対パス・#-参照はリポジトリ内の審査記録として扱わない。

    ``..`` は本関数では弾かない(旧実装が弾いていたため、``docs/reviews/../reviews/x.md`` が
    無音で「審査記録なし」に落ちる迂回路になっていた — 独立役員審査 C-2)。正規化と範囲検査は
    :func:`resolve_review_path` の担当で、そこでリポジトリ外へ出るものは**エラー**になる。
    """
    assert not is_repo_path_ref(ref)


def test_dot_dot_reference_is_repo_path_shape():
    """``..`` を含む参照は本関数では通し、範囲検査は resolve_review_path に任せる(C-2)。"""
    assert is_repo_path_ref("docs/reviews/../reviews/x.md")


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


# ── C-1: 発効を止める判定(BLOCKING_VERDICTS)─────────────────────────────────
def test_blocking_verdicts_are_defined():
    """``request_changes`` と ``reject`` は語彙内で発効を止める判定である(C-1)。"""
    assert "reject" in BLOCKING_VERDICTS
    assert "request_changes" in BLOCKING_VERDICTS
    assert "approve" not in BLOCKING_VERDICTS
    assert "conditional_approve" not in BLOCKING_VERDICTS


# ── C-2: 参照表記の書式を変えるだけの無音迂回を塞ぐ ──────────────────────────
def _install_new_style(tmp_path):
    d = tmp_path / "docs" / "reviews"
    d.mkdir(parents=True)
    (d / "x.md").write_text(NEW_STYLE, encoding="utf-8")


def test_dot_dot_normalization_resolves_to_the_same_file(tmp_path):
    """``docs/reviews/../reviews/x.md`` は正規化されて同じファイルに解決する(C-2(a))。

    旧実装は ``..`` を含む参照を「リポジトリ内パスではない」と判定し、無音で「審査記録なし」に
    落としていた。書式を変えるだけで fail-safe を迂回できてはならない。
    """
    _install_new_style(tmp_path)
    art = load_review_artifact("docs/reviews/../reviews/x.md", repo_root=tmp_path)
    assert art is not None and art.reviewed_sha == SHA


def test_escape_reference_is_an_error(tmp_path):
    """正規化してリポジトリ外へ出る参照は ``None`` ではなくエラー(中止)にする(C-2(a))。"""
    with pytest.raises(ReviewArtifactError, match="外"):
        resolve_review_path("../outside.md", repo_root=tmp_path)


def test_absolute_reference_is_an_error(tmp_path):
    """絶対パスはリポジトリ相対で書く規則を明示するためエラーにする。"""
    with pytest.raises(ReviewArtifactError, match="絶対パス"):
        resolve_review_path("/etc/passwd", repo_root=tmp_path)


def test_symlink_reference_is_an_error(tmp_path):
    """symlink 経由でリポジトリ外を読む経路を塞ぐ(C-2 / C-7)。"""
    outside = tmp_path.parent / "outside.md"
    outside.write_text(NEW_STYLE, encoding="utf-8")
    d = tmp_path / "docs" / "reviews"
    d.mkdir(parents=True)
    (d / "link.md").symlink_to(outside)
    with pytest.raises(ReviewArtifactError, match="symlink|外"):
        resolve_review_path("docs/reviews/link.md", repo_root=tmp_path)


def test_blob_url_for_own_repo_resolves_to_repository_path(tmp_path):
    """自リポジトリの GitHub blob URL はリポジトリ内パスに変換して扱う(C-2(b))。

    ``resolve_review_path`` に ``repo_slug`` を明示すれば ``git remote get-url`` は呼ばれない。
    URL の書式を変えるだけで「リポジトリ内参照ではない」に落ちる迂回を塞ぐ。
    """
    _install_new_style(tmp_path)
    url = "https://github.com/klonyapin/ryza/blob/main/docs/reviews/x.md"
    path = resolve_review_path(url, repo_root=tmp_path, repo_slug="klonyapin/ryza")
    assert path is not None and path.name == "x.md"


def test_blob_url_for_other_repo_is_not_local(tmp_path):
    """他リポジトリの blob URL は従来どおりリポジトリ外扱い(``None``)にする。"""
    _install_new_style(tmp_path)
    url = "https://github.com/other/other/blob/main/docs/reviews/x.md"
    assert resolve_review_path(url, repo_root=tmp_path, repo_slug="klonyapin/ryza") is None


def test_load_via_blob_url_reads_the_repository_file(tmp_path):
    """自リポの blob URL を ``load_review_artifact`` に渡しても意見書として読める。"""
    _install_new_style(tmp_path)
    url = "https://github.com/klonyapin/ryza/blob/main/docs/reviews/x.md"
    art = load_review_artifact(url, repo_root=tmp_path, repo_slug="klonyapin/ryza")
    assert art is not None and art.reviewed_sha == SHA


def test_front_matter_after_leading_blank_line_is_parsed():
    """先頭に空行を1行入れた front matter は読める(C-2(d))。

    旧実装は 0 行目だけを開始フェンスと見なしたため、先頭に空行1行を入れるだけで front matter
    全体が無効化できた。開始フェンスの前後の空白は既に許容されているのに、空行だけを無効化する
    非対称は様式として説明できない。
    """
    text = "\n" + NEW_STYLE
    art = parse_review_artifact(text)
    assert art is not None and art.reviewed_sha == SHA


def test_multiple_leading_blank_lines_are_tolerated():
    text = "\n\n\n" + NEW_STYLE
    art = parse_review_artifact(text)
    assert art is not None and art.reviewed_sha == SHA


# ── C-6: front matter のトップレベルキー重複は拒否 ─────────────────────────
def test_duplicate_top_level_key_is_an_error():
    """``yaml.safe_load`` の後勝ちで無警告に採用値が入れ替わる経路を塞ぐ(C-6)。"""
    text = f"---\nreviewed_sha: {SHA}\nreviewed_sha: {'f'*40}\n---\n本文\n"
    with pytest.raises(ReviewArtifactError, match="複数"):
        parse_review_artifact(text)


def test_indented_duplicate_is_not_a_top_level_key():
    """インデントされた行は「別のキーの値」でありトップレベルキー重複ではない。"""
    # 現様式にネスト構造は無いが、将来の拡張で誤検出しないことを固定する。
    text = f"---\nreviewed_sha: {SHA}\nnotes:\n  reviewed_sha: なにか\n---\n本文\n"
    # notes のパース自体は成立するので余剰キー警告のみ。
    art = parse_review_artifact(text)
    assert art is not None and art.reviewed_sha == SHA


# ── C-3: 意見書の初出コミット日時 ─────────────────────────────────────────
def test_first_commit_date_returns_none_for_untracked_file(tmp_path):
    """git 追跡されていないファイル(または git 以外のリポジトリ)は ``None`` を返す。"""
    (tmp_path / "x.md").write_text("hello", encoding="utf-8")
    assert first_commit_date(tmp_path, tmp_path / "x.md") is None
