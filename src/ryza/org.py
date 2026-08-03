"""org — 組織メンバー台帳(``config/org.yaml``)の共通ローダ。

代表指示(2026-08-03)「Discord 上でも Web 上でもキャラクターを使って(役職名と一緒に)」
の共通実装。Discord embed の author・役員室チャットの表示名・アバター・色は全て本モジュール
経由で台帳(``config/org.yaml`` が正)から引き、キャラクター情報を各所にハードコードしない。

**アイコン URL について(Discord の制約)**: Discord は embed author の icon_url に SVG を
表示できない(PNG/JPG/WebP/GIF のみ)。台帳の ``icon`` は ``site/avatars/<id>.svg`` を指すが、
Discord 向けの ``Member.icon_url`` は拡張子を ``.png`` へ読み替えた GitHub raw URL
(``https://raw.githubusercontent.com/klonyapin/ryza/main/site/avatars/<id>.png``)を
組み立てる設計とする。PNG 素材の生成はホームページ側タスク(site/avatars 整備)と調整中で、
未生成の間この URL は 404 になるが、Discord はアイコン取得に失敗しても名前だけで author を
表示するため embed 自体は成立する。Streamlit(役員室チャット)は SVG を表示できるため、
ローカルの SVG パス(``Member.icon_repo_path``)をそのまま使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

# リポジトリルート(src/ryza/org.py から 2 つ上)。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "org.yaml"

# GitHub raw のベース URL。embed アイコンは Discord 側が取得するため公開 URL が必要。
_RAW_BASE = "https://raw.githubusercontent.com/klonyapin/ryza/main/"


@dataclass(frozen=True)
class Member:
    """台帳の 1 メンバー(キャラクター)。"""

    id: str
    name: str  # 例: 玲音
    title: str  # 例: 報道部アナリスト
    dept: str
    persona: str  # 例: personas/press-lain(役職キーとの対応もこの値で取る)
    color: str  # "#rrggbb"
    icon: str  # リポジトリ相対パス(site/avatars/<id>.svg)
    icon_url: str  # Discord 用アイコンの公開 URL(台帳の icon_url、無ければ PNG 読み替え raw URL)
    tagline: str = ""

    @property
    def display_name(self) -> str:
        """「名前(役職)」— 全対話面で名乗る表記(代表指示 2026-08-03)。"""
        return f"{self.name}({self.title})"

    @property
    def color_int(self) -> int:
        """Discord embed の color 値(#rrggbb → int)。"""
        return int(self.color.lstrip("#"), 16)

    @property
    def icon_repo_path(self) -> Path:
        """リポジトリ内アイコン(SVG)の絶対パス(Streamlit 用。存在確認は呼び出し側)。"""
        return _REPO_ROOT / self.icon


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[Member, ...]:
    """org.yaml を読んで Member タプルにする(プロセス内キャッシュ)。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    members: list[Member] = []
    for m in data.get("members", []):
        icon = str(m.get("icon", f"site/avatars/{m['id']}.svg"))
        # 台帳に icon_url(検証済みの直接 URL)があればそれが正。無ければ icon の
        # 拡張子を .png へ読み替えた GitHub raw URL(モジュール docstring 参照)。
        icon_url = str(m.get("icon_url") or _RAW_BASE + str(Path(icon).with_suffix(".png")))
        members.append(
            Member(
                id=str(m["id"]),
                name=str(m["name"]),
                title=str(m["title"]),
                dept=str(m.get("dept", "")),
                persona=str(m.get("persona", "")),
                color=str(m.get("color", "#5b54c7")),
                icon=icon,
                icon_url=icon_url,
                tagline=str(m.get("tagline", "")),
            )
        )
    return tuple(members)


def members(path: str | Path = _CONFIG_PATH) -> dict[str, Member]:
    """全メンバー(id → Member)。"""
    return {m.id: m for m in _load(str(path))}


def get_member(member_id: str, path: str | Path = _CONFIG_PATH) -> Member:
    """id でメンバーを引く。台帳に無い id は即例外(黙って既定人格を出さない)。"""
    try:
        return members(path)[member_id]
    except KeyError as exc:
        raise KeyError(f"config/org.yaml に id='{member_id}' のメンバーがいない") from exc


def member_for_role(role: str, path: str | Path = _CONFIG_PATH) -> Member:
    """役職キー(cio / independent_officer / audit 等)から担当メンバーを引く。

    対応表を二重管理せず、台帳の ``persona`` フィールド
    (``personas/<役職キーをハイフン化>`` — personas.py の命名規約)で解決する。
    """
    persona = "personas/" + role.replace("_", "-")
    for m in _load(str(path)):
        if m.persona == persona:
            return m
    raise KeyError(f"config/org.yaml に persona='{persona}' のメンバーがいない")


def embed_author(member_id: str, path: str | Path = _CONFIG_PATH) -> dict[str, str]:
    """Discord embed の author dict(「名前(役職)」+アイコン URL)。

    報道部(aya)・監査(tanya)の embed 構築が使う共通ヘルパ。T-015 の
    リスクレポート等、今後の embed もここを通す(名前・役職のハードコード禁止)。
    """
    m = get_member(member_id, path)
    return {"name": m.display_name, "icon_url": m.icon_url}


def author_for_role(role: str, path: str | Path = _CONFIG_PATH) -> dict[str, str]:
    """役職キーから embed author dict を引く(台帳のキャラ改名・id 変更に自動追従)。"""
    m = member_for_role(role, path)
    return {"name": m.display_name, "icon_url": m.icon_url}


__all__ = [
    "Member",
    "author_for_role",
    "embed_author",
    "get_member",
    "member_for_role",
    "members",
]
