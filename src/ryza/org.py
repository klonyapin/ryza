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

**アイコンの実行時上書き(0020・代表指示 2026-08-03)**: 代表はダッシュボードの組織ページから
アイコンを差し替えられる。上書きは ``ops.org_icon_overrides`` に入り、``conn`` を渡した
呼び出し(``effective_members`` / ``get_member(..., conn=...)`` 等)でのみ台帳の上に重なる。
``conn`` を渡さない呼び出しは従来どおり YAML そのままで、DB を持たない経路
(``bridge_send`` 等)は影響を受けない。**上書きはキャッシュしない** — 保存直後の投稿・
描画に必ず反映させるため、読取のたびに PK 1 行の SELECT を行う(0020 の履歴方式の注記参照)。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.request
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# リポジトリルート(src/ryza/org.py から 2 つ上)。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "org.yaml"

# GitHub raw のベース URL。embed アイコンは Discord 側が取得するため公開 URL が必要。
_RAW_BASE = "https://raw.githubusercontent.com/klonyapin/ryza/main/"

# embed の author に載せる内部キー(0020)。Discord API のフィールドではないため、
# 配送直前に ``resolve_author`` が取り除く。
AUTHOR_MEMBER_KEY = "member_id"


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
    """全メンバー(id → Member)。台帳(YAML)そのまま — DB 上書きは適用しない。"""
    return {m.id: m for m in _load(str(path))}


def effective_members(
    conn: Any | None = None, path: str | Path = _CONFIG_PATH
) -> dict[str, Member]:
    """台帳に DB のアイコン上書き(0020)を重ねた実効メンバー(id → Member)。

    ``conn`` が None なら ``members()`` と同じ(後方互換 — DB を持たない経路のため)。
    台帳に無い ``member_id`` の上書き行は**無視**する(YAML が正。消えたキャラの
    残骸を表示に混ぜない)。
    """
    base = members(path)
    if conn is None:
        return base
    merged = dict(base)
    for member_id, icon_url in icon_overrides(conn).items():
        current = merged.get(member_id)
        if current is None:
            continue  # 台帳に無い id(改名・削除の残骸)は無視する
        merged[member_id] = replace(current, icon_url=icon_url)
    return merged


def get_member(
    member_id: str, path: str | Path = _CONFIG_PATH, *, conn: Any | None = None
) -> Member:
    """id でメンバーを引く。台帳に無い id は即例外(黙って既定人格を出さない)。"""
    try:
        return effective_members(conn, path)[member_id]
    except KeyError as exc:
        raise KeyError(f"config/org.yaml に id='{member_id}' のメンバーがいない") from exc


def member_for_role(
    role: str, path: str | Path = _CONFIG_PATH, *, conn: Any | None = None
) -> Member:
    """役職キー(cio / independent_officer / audit 等)から担当メンバーを引く。

    対応表を二重管理せず、台帳の ``persona`` フィールド
    (``personas/<役職キーをハイフン化>`` — personas.py の命名規約)で解決する。
    """
    persona = "personas/" + role.replace("_", "-")
    for m in effective_members(conn, path).values():
        if m.persona == persona:
            return m
    raise KeyError(f"config/org.yaml に persona='{persona}' のメンバーがいない")


def embed_author(
    member_id: str, path: str | Path = _CONFIG_PATH, *, conn: Any | None = None
) -> dict[str, str]:
    """Discord embed の author dict(「名前(役職)」+アイコン URL)。

    報道部(aya)・監査(tanya)の embed 構築が使う共通ヘルパ。T-015 の
    リスクレポート等、今後の embed もここを通す(名前・役職のハードコード禁止)。

    ``member_id`` を内部キーとして同梱する(``AUTHOR_MEMBER_KEY``)。配送時に Bot が
    最新のアイコン上書きへ解決し直すために必要で、Discord へ送る直前に
    ``resolve_author`` が取り除く(Discord API へは渡らない)。
    """
    m = get_member(member_id, path, conn=conn)
    return {"name": m.display_name, "icon_url": m.icon_url, AUTHOR_MEMBER_KEY: m.id}


def author_for_role(
    role: str, path: str | Path = _CONFIG_PATH, *, conn: Any | None = None
) -> dict[str, str]:
    """役職キーから embed author dict を引く(台帳のキャラ改名・id 変更に自動追従)。"""
    m = member_for_role(role, path, conn=conn)
    return {"name": m.display_name, "icon_url": m.icon_url, AUTHOR_MEMBER_KEY: m.id}


# ── アイコン上書き(0020)─────────────────────────────────────────────────────
def icon_overrides(conn: Any) -> dict[str, str]:
    """``ops.org_icon_overrides`` の現在値(member_id → icon_url)。

    即反映のためキャッシュしない(0020 の履歴方式の注記)。読取専用ロール
    (``ryza_dashboard``)でも実行できる SELECT のみ。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT member_id, icon_url FROM ops.org_icon_overrides")
        return {row[0]: row[1] for row in cur.fetchall()}


def resolve_author(
    author: dict[str, Any], overrides: dict[str, str]
) -> dict[str, Any]:
    """配送直前に author の ``icon_url`` を最新の上書きへ差し替える(純関数)。

    ``member_id`` は内部キーなので常に取り除く(Discord へ未知フィールドを送らない)。
    上書きが無い/古い embed(member_id 無し)はそのまま通す。
    """
    resolved = {k: v for k, v in author.items() if k != AUTHOR_MEMBER_KEY}
    member_id = author.get(AUTHOR_MEMBER_KEY)
    if member_id and member_id in overrides:
        resolved["icon_url"] = overrides[member_id]
    return resolved


def apply_icon_overrides(
    embed: dict[str, Any], overrides: dict[str, str]
) -> dict[str, Any]:
    """embed(dict)の author に上書きを適用した新しい dict を返す(純関数)。

    author を持たない embed(起動通知など)はそのまま返す。
    """
    author = embed.get("author")
    if not isinstance(author, dict):
        return embed
    return {**embed, "author": resolve_author(author, overrides)}


# ── アイコン URL の検証(https のみ・実体が画像であること)───────────────────
class IconUrlError(ValueError):
    """アイコン URL が使えない(スキーム違反・到達不能・画像でない・内部宛)。"""


# 検証の実アクセスは短時間で打ち切る(UI の保存操作を待たせない)。
ICON_URL_TIMEOUT = 5.0

# 受け入れる画像形式(独立役員審査 0020 C-8)。``image/*`` 全体を許すと ``image/svg+xml``
# が通り、SVG は script・外部参照を含みうるマークアップである。Streamlit は SVG を
# レンダリングするため、組織ページの閲覧者(代表)のブラウザで実行されうる。
# Discord が表示できる形式(PNG/JPEG/GIF/WebP)に限れば実害なく塞げる。
ICON_ALLOWED_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")

# 上限 5MB(独立役員審査 0020 C-8)。Discord のアバターに 5MB 超の原寸画像は不要で、
# 巨大ファイルは表示のたびに代表の回線と Discord 側の取得を無駄に使う。
ICON_MAX_BYTES = 5 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトを一切追従しないハンドラ(``None`` を返すと urllib は 3xx を送出)。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _default_opener(url: str, method: str, timeout: float) -> dict[str, str]:
    """URL へ ``method`` でアクセスし、応答ヘッダを小文字キーの dict で返す。

    **リダイレクトは追従しない**(独立役員審査 0020 C-6)。追従を許すと、検証を通った
    https の外部 URL から内部アドレスへ誘導され、検証の実アクセス自体が内部宛リクエスト
    (SSRF)になる。3xx は ``HTTPError`` として失敗させ、利用者には最終 URL を直接
    指定してもらう。
    """
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "RyzaOrg/1.0"})
    with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - https 限定済み
        return {str(k).lower(): str(v) for k, v in resp.headers.items()}


def _reject_internal_host(host: str) -> None:
    """名前解決した宛先がインターネット公開アドレスでなければ ``IconUrlError``。

    暫定の SSRF 緩和(独立役員審査 0020 C-6)。``127.0.0.1`` / ``10.0.0.0/8`` /
    ``169.254.169.254``(GCE メタデータ)等へ検証アクセスさせない。

    **限界**: 検証時の名前解決と実アクセス時の名前解決は別で、DNS の応答を切り替える
    攻撃(DNS rebinding)には無力である。恒久是正は保存時に画像を自前で再ホストして
    外部 URL への実アクセス自体を無くすこと(ops/reminders.yaml icon-rehost-storage)。
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise IconUrlError(f"ホスト名を解決できない({host}): {exc}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise IconUrlError(
                f"内部アドレスへ解決される URL は使えない({host} → {address})"
            )


def check_icon_url(
    url: str,
    *,
    opener: Any | None = None,
    timeout: float = ICON_URL_TIMEOUT,
) -> str:
    """アイコン URL を検証して正規化した URL を返す。不可なら ``IconUrlError``。

    - **https のみ**(http・data:・相対 URL は拒否。Discord も Streamlit も外部から
      取得するため、平文経路とスキームの取り違えをここで塞ぐ)
    - **宛先がインターネット公開アドレス**であること(``_reject_internal_host``)
    - 実アクセス(**リダイレクト追従なし**)して ``Content-Type`` が
      ``ICON_ALLOWED_TYPES`` のいずれかであること。HEAD を拒む配信元(405/501)が
      あるため HEAD → GET の順に試す
    - ``Content-Length`` が ``ICON_MAX_BYTES`` 以下であること。**ヘッダが無い場合は
      拒否する** — ボディを実際に読んで実測する経路を作ると、検証のために任意の外部
      URL から大きなデータを取得することになり、SSRF の増幅・DoS の的になる。
      サイズを申告しない配信元は代表に別の URL を選んでもらう方が安全で単純
    - 到達不能・タイムアウトは失敗として扱い、**保存しない**

    ``opener`` は ``(url, method, timeout) -> 小文字キーのヘッダ dict`` の差し替え口
    (テストは実ネットワークを叩かない)。
    """
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        raise IconUrlError(f"https:// の URL のみ受け付ける(受領: {candidate!r})")
    fetch = opener if opener is not None else _default_opener
    if opener is None:
        # 差し替え時(テスト)は名前解決しない。実 I/O を行う既定経路だけが対象。
        _reject_internal_host(parsed.hostname)
    headers: dict[str, str] = {}
    errors: list[str] = []
    for method in ("HEAD", "GET"):
        try:
            headers = fetch(candidate, method, timeout)
            break
        except Exception as exc:  # noqa: BLE001 - 失敗理由は利用者に見せる
            errors.append(f"{method}: {type(exc).__name__}: {exc}")
    else:
        raise IconUrlError(f"URL に到達できない({' / '.join(errors)})")

    content_type = str(headers.get("content-type", "")).split(";")[0].strip().lower()
    if content_type not in ICON_ALLOWED_TYPES:
        raise IconUrlError(
            f"対応していない画像形式(Content-Type: {content_type or '(なし)'})。"
            f"{' / '.join(ICON_ALLOWED_TYPES)} の直リンク URL を指定する"
        )
    raw_length = str(headers.get("content-length", "")).strip()
    if not raw_length.isdigit():
        raise IconUrlError(
            "サイズ(Content-Length)を申告しない URL は受け付けない。"
            "画像の直リンク URL を指定する"
        )
    if int(raw_length) > ICON_MAX_BYTES:
        raise IconUrlError(
            f"画像が大きすぎる({int(raw_length):,} bytes > 上限 {ICON_MAX_BYTES:,} bytes)"
        )
    return candidate


# ── 上書きの書込(ダッシュボード組織ページ — ryza_boardroom ロール)──────────
def set_icon_override(
    conn: Any, member_id: str, icon_url: str, actor: str, *, path: str | Path = _CONFIG_PATH
) -> None:
    """アイコン上書きを保存し、変更履歴を 1 行残す(呼び出し側が commit / autocommit)。

    台帳に無い ``member_id`` は ``KeyError``(存在しないキャラの上書きを作らない)。
    URL の検証は呼び出し側の責務(``check_icon_url``)— ここは DB 書込のみを行う。

    現在値とログは ``conn.transaction()`` で明示的に囲む(独立役員審査 0020 C-1)。
    本番の呼び出し元(``queries.connect_boardroom``)は **autocommit=True** の接続で、
    囲まないと 2 文が別トランザクションになり、ログ INSERT が失敗しても現在値だけが
    残る。それは 0020 が方式 B の担保として掲げた「同一トランザクション」の不成立
    であり、履歴の無い上書き=改竄と区別できない状態を作る。``transaction()`` は
    autocommit / 非 autocommit のどちらの接続でも 1 単位に束ねる。

    **非 autocommit の呼び出し元への注意**: psycopg の ``transaction()`` は、
    トランザクションが未開始なら BEGIN して**ブロック脱出時に COMMIT する**。既に
    トランザクション中なら SAVEPOINT として振る舞い、commit の判断は呼び出し元に残る。
    したがって本関数を「その接続の最初の文」として呼ぶと即時確定する。
    """
    if member_id not in members(path):
        raise KeyError(f"config/org.yaml に id='{member_id}' のメンバーがいない")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.org_icon_overrides (member_id, icon_url, updated_by, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (member_id) DO UPDATE
            SET icon_url = EXCLUDED.icon_url,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (member_id, icon_url, actor),
        )
        cur.execute(
            """
            INSERT INTO ops.org_icon_override_log (member_id, action, icon_url, actor)
            VALUES (%s, 'set', %s, %s)
            """,
            (member_id, icon_url, actor),
        )


def update_icon(
    conn: Any,
    member_id: str,
    url: str,
    actor: str,
    *,
    opener: Any | None = None,
    timeout: float = ICON_URL_TIMEOUT,
    path: str | Path = _CONFIG_PATH,
) -> str:
    """URL を検証してから上書きを保存する。**検証に失敗したら書き込まない**。

    ダッシュボードの保存ボタンが呼ぶ入口。検証(``check_icon_url``)と書込
    (``set_icon_override``)を必ずこの順で結び、「検証を飛ばして保存する」経路を
    UI 側に作らせない。
    """
    checked = check_icon_url(url, opener=opener, timeout=timeout)
    set_icon_override(conn, member_id, checked, actor, path=path)
    return checked


def clear_icon_override(conn: Any, member_id: str, actor: str) -> bool:
    """上書きを削除して台帳の初期値へ戻す。削除した行があれば True。

    上書きが無い場合も履歴は残さない(状態が変わっていないため)。
    削除とログを ``conn.transaction()`` で束ねる理由は ``set_icon_override`` と同じ
    (独立役員審査 0020 C-1 — autocommit 接続で削除だけが残る経路を塞ぐ)。
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ops.org_icon_overrides WHERE member_id = %s RETURNING member_id",
            (member_id,),
        )
        if cur.fetchone() is None:
            return False
        cur.execute(
            """
            INSERT INTO ops.org_icon_override_log (member_id, action, icon_url, actor)
            VALUES (%s, 'reset', NULL, %s)
            """,
            (member_id, actor),
        )
    return True


__all__ = [
    "AUTHOR_MEMBER_KEY",
    "ICON_ALLOWED_TYPES",
    "ICON_MAX_BYTES",
    "ICON_URL_TIMEOUT",
    "IconUrlError",
    "Member",
    "apply_icon_overrides",
    "author_for_role",
    "check_icon_url",
    "clear_icon_override",
    "effective_members",
    "embed_author",
    "get_member",
    "icon_overrides",
    "member_for_role",
    "members",
    "resolve_author",
    "set_icon_override",
    "update_icon",
]
