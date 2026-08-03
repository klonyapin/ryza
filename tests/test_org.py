"""org — 組織メンバー台帳ローダ(config/org.yaml v2)のテスト。DB 不要の純ロジック。"""

from __future__ import annotations

import pytest

from ryza import org


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
