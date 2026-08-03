"""org — 組織メンバー台帳ローダ(config/org.yaml v2)のテスト。

台帳の読取・役職解決・author 組立は DB 不要の純ロジック。アイコン上書き(0020)の
マージ・URL 検証も、DB とネットワークをフェイクで差し替えて DB 無しで検証する
(実 DB を使う保存/履歴の検証は tests/ops/test_org_icon_overrides.py)。
"""

from __future__ import annotations

import socket

import pytest

from ryza import org


class _FakeCursor:
    """``ops.org_icon_overrides`` の SELECT だけに答える最小のカーソル。"""

    def __init__(self, rows):
        self._rows = rows
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.queries.append(sql)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, overrides: dict[str, str]):
        self.cursor_obj = _FakeCursor(list(overrides.items()))

    def cursor(self):
        return self.cursor_obj


def test_members_loaded_from_ledger():
    members = org.members()
    # v2 の 9 キャラクター(台帳が正 — id は哲学の器として維持)。
    for member_id in (
        "emilia", "homura", "tanya", "aoba", "aya", "ben", "jim", "stan", "peter"
    ):
        assert member_id in members


def test_display_name_is_name_with_title():
    aya = org.get_member("aya")
    assert aya.name == "射命丸 文"
    assert aya.title == "報道部アナリスト"
    assert aya.display_name == "射命丸 文(報道部アナリスト)"


def test_color_int_converts_hex():
    tanya = org.get_member("tanya")
    assert tanya.color == "#b45309"
    assert tanya.color_int == 0xB45309


def test_icon_url_prefers_ledger_value():
    """台帳に icon_url(実アクセス検証済みの直接 URL)があればそれが正。"""
    for m in org.members().values():
        assert m.icon_url.startswith("https://")


def test_icon_url_falls_back_to_png_raw_url(tmp_path):
    """icon_url が無いメンバーは icon の拡張子を .png へ読み替えた raw URL(docstring 参照)。"""
    yaml_path = tmp_path / "org.yaml"
    yaml_path.write_text(
        "version: '9'\n"
        "members:\n"
        "  - id: x\n"
        "    name: テスト\n"
        "    title: 役職\n"
        "    icon: site/avatars/x.svg\n",
        encoding="utf-8",
    )
    m = org.get_member("x", path=yaml_path)
    assert m.icon_url == (
        "https://raw.githubusercontent.com/klonyapin/ryza/main/site/avatars/x.png"
    )


def test_unknown_member_raises():
    with pytest.raises(KeyError):
        org.get_member("nobody")


def test_member_for_role_resolves_via_persona():
    """役職キー → メンバーは persona フィールドで解決(対応表の二重管理をしない)。"""
    assert org.member_for_role("cio").id == "emilia"
    assert org.member_for_role("independent_officer").id == "homura"
    assert org.member_for_role("audit").id == "tanya"
    assert org.member_for_role("dev_lead").id == "aoba"
    with pytest.raises(KeyError):
        org.member_for_role("unknown_role")


def test_embed_author_shape():
    author = org.embed_author("aya")
    assert author["name"] == "射命丸 文(報道部アナリスト)"
    assert author["icon_url"] == org.get_member("aya").icon_url


def test_author_for_role_follows_ledger_rename():
    author = org.author_for_role("audit")
    assert author["name"] == "ターニャ(監査部門)"


def test_embed_author_carries_member_id_for_delivery_time_resolution():
    """配送時に上書きを引けるよう、author は内部キー member_id を運ぶ(0020)。"""
    assert org.embed_author("aya")[org.AUTHOR_MEMBER_KEY] == "aya"
    assert org.author_for_role("audit")[org.AUTHOR_MEMBER_KEY] == "tanya"


# ── アイコン上書きのマージ(0020)─────────────────────────────────────────────
def test_effective_members_without_conn_is_yaml_only():
    """conn を渡さない呼び出しは従来どおり台帳そのまま(後方互換)。"""
    assert org.effective_members() == org.members()
    assert org.effective_members(None)["aya"].icon_url == org.members()["aya"].icon_url


def test_effective_members_applies_db_override():
    conn = _FakeConn({"aya": "https://example.test/new-aya.png"})
    merged = org.effective_members(conn)
    assert merged["aya"].icon_url == "https://example.test/new-aya.png"
    # 上書きされるのは icon_url だけ(名前・色・役職は台帳のまま)。
    assert merged["aya"].name == org.members()["aya"].name
    assert merged["aya"].color == org.members()["aya"].color
    # 上書きの無いメンバーは台帳のまま。
    assert merged["tanya"].icon_url == org.members()["tanya"].icon_url


def test_effective_members_ignores_unknown_member_id():
    """台帳に無い id の上書き行は無視する(消えたキャラの残骸を表示に混ぜない)。"""
    conn = _FakeConn({"ghost": "https://example.test/ghost.png", "aya": "https://e.test/a.png"})
    merged = org.effective_members(conn)
    assert "ghost" not in merged
    assert merged["aya"].icon_url == "https://e.test/a.png"


def test_get_member_and_authors_follow_override():
    conn = _FakeConn({"tanya": "https://example.test/tanya.png"})
    assert org.get_member("tanya", conn=conn).icon_url == "https://example.test/tanya.png"
    assert org.member_for_role("audit", conn=conn).icon_url == "https://example.test/tanya.png"
    assert org.author_for_role("audit", conn=conn)["icon_url"] == "https://example.test/tanya.png"


# ── 配送時の解決(純関数)──────────────────────────────────────────────────────
def test_resolve_author_replaces_icon_and_strips_internal_key():
    author = {"name": "射命丸 文(報道部アナリスト)", "icon_url": "https://old/x.png",
              org.AUTHOR_MEMBER_KEY: "aya"}
    resolved = org.resolve_author(author, {"aya": "https://new/y.png"})
    assert resolved == {"name": "射命丸 文(報道部アナリスト)", "icon_url": "https://new/y.png"}


def test_resolve_author_strips_internal_key_even_without_override():
    """member_id は Discord API のフィールドではないため常に落とす。"""
    author = {"name": "n", "icon_url": "https://old/x.png", org.AUTHOR_MEMBER_KEY: "aya"}
    assert org.resolve_author(author, {}) == {"name": "n", "icon_url": "https://old/x.png"}


def test_apply_icon_overrides_on_embed():
    embed = {"title": "朝刊", "author": org.embed_author("aya"), "color": 1}
    out = org.apply_icon_overrides(embed, {"aya": "https://new/y.png"})
    assert out["author"]["icon_url"] == "https://new/y.png"
    assert org.AUTHOR_MEMBER_KEY not in out["author"]
    assert out["title"] == "朝刊" and out["color"] == 1
    assert embed["author"][org.AUTHOR_MEMBER_KEY] == "aya"  # 元の dict は壊さない


def test_apply_icon_overrides_passthrough_without_author():
    embed = {"title": "起動通知", "color": 1}
    assert org.apply_icon_overrides(embed, {"aya": "https://new/y.png"}) == embed


# ── URL 検証 ─────────────────────────────────────────────────────────────────
def _opener(content_type: str, *, length: str | None = "1024", fail_head: bool = False):
    """``check_icon_url`` の I/O 差し替え口(小文字キーのヘッダ dict を返す)。"""
    calls: list[tuple[str, str]] = []

    def _fake(url: str, method: str, timeout: float) -> dict[str, str]:
        calls.append((url, method))
        if fail_head and method == "HEAD":
            raise OSError("405 Method Not Allowed")
        headers = {"content-type": content_type}
        if length is not None:
            headers["content-length"] = length
        return headers

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake


def test_check_icon_url_accepts_https_image():
    opener = _opener("image/png")
    assert org.check_icon_url(" https://x.test/a.png ", opener=opener) == "https://x.test/a.png"
    assert opener.calls == [("https://x.test/a.png", "HEAD")]


def test_check_icon_url_accepts_content_type_with_charset():
    assert org.check_icon_url("https://x.test/a.jpg", opener=_opener("image/jpeg; charset=binary"))


@pytest.mark.parametrize(
    "url", ["http://x.test/a.png", "ftp://x.test/a.png", "data:image/png;base64,AA", "/a.png"]
)
def test_check_icon_url_rejects_non_https(url):
    opener = _opener("image/png")
    with pytest.raises(org.IconUrlError):
        org.check_icon_url(url, opener=opener)
    assert opener.calls == []  # 実アクセスの前に弾く


def test_check_icon_url_rejects_non_image():
    with pytest.raises(org.IconUrlError, match="対応していない画像形式"):
        org.check_icon_url("https://x.test/page", opener=_opener("text/html"))


@pytest.mark.parametrize("content_type", list(org.ICON_ALLOWED_TYPES))
def test_check_icon_url_accepts_each_allowed_type(content_type):
    assert org.check_icon_url("https://x.test/a", opener=_opener(content_type))


def test_check_icon_url_rejects_svg(caplog):
    """SVG は script・外部参照を含みうるマークアップのため許可しない(C-8)。"""
    with pytest.raises(org.IconUrlError, match="対応していない画像形式"):
        org.check_icon_url("https://x.test/a.svg", opener=_opener("image/svg+xml"))


def test_check_icon_url_rejects_oversized_image():
    big = str(org.ICON_MAX_BYTES + 1)
    with pytest.raises(org.IconUrlError, match="大きすぎる"):
        org.check_icon_url("https://x.test/a.png", opener=_opener("image/png", length=big))


def test_check_icon_url_accepts_exactly_max_size():
    opener = _opener("image/png", length=str(org.ICON_MAX_BYTES))
    assert org.check_icon_url("https://x.test/a.png", opener=opener)


def test_check_icon_url_rejects_missing_content_length():
    """サイズ未申告は拒否(ボディを実測しに行かない — SSRF 増幅・DoS を避ける)。"""
    with pytest.raises(org.IconUrlError, match="Content-Length"):
        org.check_icon_url("https://x.test/a.png", opener=_opener("image/png", length=None))


def test_check_icon_url_falls_back_to_get_when_head_rejected():
    opener = _opener("image/webp", fail_head=True)
    assert org.check_icon_url("https://x.test/a.webp", opener=opener)
    assert [m for _, m in opener.calls] == ["HEAD", "GET"]


def test_check_icon_url_rejects_unreachable():
    def _boom(url: str, method: str, timeout: float) -> dict[str, str]:
        raise TimeoutError("timed out")

    with pytest.raises(org.IconUrlError, match="到達できない"):
        org.check_icon_url("https://x.test/a.png", opener=_boom)


# ── SSRF 緩和(独立役員審査 0020 C-6)────────────────────────────────────────
@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.169.254", "::1"]
)
def test_reject_internal_host_blocks_non_global_addresses(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(
        org.socket, "getaddrinfo",
        lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (address, 443))],
    )
    with pytest.raises(org.IconUrlError, match="内部アドレス"):
        org._reject_internal_host("internal.test")


def test_reject_internal_host_allows_public_address(monkeypatch):
    monkeypatch.setattr(
        org.socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    org._reject_internal_host("example.test")  # 例外が出なければ合格


def test_reject_internal_host_rejects_unresolvable(monkeypatch):
    def _boom(*a, **k):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(org.socket, "getaddrinfo", _boom)
    with pytest.raises(org.IconUrlError, match="解決できない"):
        org._reject_internal_host("nx.test")


def test_default_opener_does_not_follow_redirects():
    """リダイレクト追従を無効化していること(3xx から内部宛へ誘導させない)。"""
    handler = org._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test/") is None
