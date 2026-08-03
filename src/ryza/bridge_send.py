"""bridge_send — 開発セッション → Discord #dev ブリッジ送信(設計リード名義)。

``~/.ryza/send.py``(リポジトリ外・素のテキスト送信)の後継。開発セッション(Claude)が
代表へ連絡するとき、stdin のテキストを**設計リードのキャラクター名義**で送信する
(代表指示 2026-08-03「対話は Discord 上でもキャラクターを使って(役職名と一緒に)」)。
名義は ``config/org.yaml`` の台帳から ``member_for_role("dev_lead")`` で引く
(名前をハードコードしないため、台帳の改名にそのまま追従する)。

送信経路(優先順):

1. **Webhook**: env ``DISCORD_WEBHOOK_URL``(または ``~/.ryza/discord_bot.env`` の同キー)
   があれば、username=「名前(役職)」/ avatar_url を設定して webhook で送信
   (投稿ごとに名前・アイコンを変えられる唯一の方式)
2. **Bot 投稿**: 無ければ ``~/.ryza/discord_bot.env`` の ``DISCORD_BOT_TOKEN`` /
   ``DISCORD_CHANNEL_ID`` で、author にキャラクターを載せた embed を送信

使い方: ``echo "本文" | python -m ryza.bridge_send [--title タイトル]``
DB・discord.py に依存しない(stdlib の urllib のみ)。Bot 本体の outbox 経路とは
独立の軽量経路(開発対話用)。

**資格情報の分散について(独立役員審査 2026-08-03 の注記)**: 本ブリッジの秘密
(Bot トークン・webhook URL)は ``~/.ryza/discord_bot.env`` に、Bot 本体の webhook
URL は DB(``ops.discord_webhooks``・0017)にあり、保管が 2 系統に分散している。
webhook URL は知っていれば誰でも投稿できる秘密のため、エラー経路では必ずマスク
(``_mask_webhook_url``)して出力する。保管の一元化・最小権限化は DB ロール分離
(実弾移行前提)と合わせて行う(``ops/reminders.yaml`` の
``db-role-separation-webhook-url`` に登録済み)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from ryza import org

_ENV_PATH = Path.home() / ".ryza" / "discord_bot.env"
_API = "https://discord.com/api/v10"

# Discord embed description の上限は 4096。分割はパラグラフ境界で余裕をもって切る。
_CHUNK_LIMIT = 3800

# ブリッジの名義は役職キーで引く(台帳の改名・キャラ変更に自動追従)。
BRIDGE_ROLE = "dev_lead"

# webhooks.mask_url と同じマスク形式。bot.webhooks を import しないのは、本モジュールを
# stdlib のみ(psycopg 非依存)に保つため(docstring の軽量経路の約束)。
_WEBHOOK_URL_RE = re.compile(r"(?P<prefix>.*?/webhooks/\d+)/\S+")


def _mask_webhook_url(url: str) -> str:
    """エラー出力用に webhook URL のトークン部を隠す(``.../webhooks/<id>/***``)。"""
    m = _WEBHOOK_URL_RE.match(url)
    return f"{m.group('prefix')}/***" if m else "<webhook url masked>"


def load_env(path: Path = _ENV_PATH) -> dict[str, str]:
    """``~/.ryza/discord_bot.env``(KEY=VALUE 行)を読む。無ければ空 dict。"""
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def split_chunks(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """パラグラフ(改行)境界で ``limit`` 字以下に分割する(純関数)。"""
    chunks: list[str] = []
    cur = ""
    for para in text.split("\n"):
        if cur and len(cur) + len(para) + 1 > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def build_embeds(
    text: str, member: org.Member, *, title: str | None = None
) -> list[dict[str, Any]]:
    """本文を author(名前(役職)+アイコン)付き embed 列へ組む(純関数)。

    タイトルは先頭 embed のみ。author は全 embed に付け、色はキャラクター色。
    """
    embeds: list[dict[str, Any]] = []
    for i, chunk in enumerate(split_chunks(text)):
        embed: dict[str, Any] = {
            "description": chunk,
            "color": member.color_int,
            "author": {"name": member.display_name, "icon_url": member.icon_url},
        }
        if title and i == 0:
            embed["title"] = title[:256]
        embeds.append(embed)
    return embeds


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str]) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "RyzaBridge/2.0",
                 **headers},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)  # noqa: S310 - Discord API 固定


def send(text: str, *, title: str | None = None, env: dict[str, str] | None = None) -> int:
    """本文を設計リード名義で送信し、送ったメッセージ数を返す。"""
    env = env if env is not None else load_env()
    member = org.member_for_role(BRIDGE_ROLE)
    embeds = build_embeds(text, member, title=title)

    webhook_url = env.get("DISCORD_WEBHOOK_URL", "")
    if webhook_url:
        # webhook: username / avatar_url を投稿ごとに設定できる(完全表示)。
        for embed in embeds:
            body = {
                "embeds": [{k: v for k, v in embed.items() if k != "author"}],
                "username": member.display_name,
                "avatar_url": member.icon_url,
            }
            try:
                _post_json(webhook_url, body, {})
            except Exception as exc:  # noqa: BLE001 - URL をマスクして報告(トークン漏洩防止)
                detail = str(exc).replace(webhook_url, _mask_webhook_url(webhook_url))
                raise SystemExit(
                    f"webhook 送信失敗({_mask_webhook_url(webhook_url)}): "
                    f"{type(exc).__name__}: {detail}"
                ) from None
            time.sleep(0.5)
        return len(embeds)

    token = env.get("DISCORD_BOT_TOKEN", "")
    channel_id = env.get("DISCORD_CHANNEL_ID", "")
    if not token or not channel_id:
        raise SystemExit(
            f"{_ENV_PATH} に DISCORD_WEBHOOK_URL か "
            "DISCORD_BOT_TOKEN/DISCORD_CHANNEL_ID が必要"
        )
    for embed in embeds:
        _post_json(
            f"{_API}/channels/{channel_id}/messages",
            {"embeds": [embed]},
            {"Authorization": f"Bot {token}"},
        )
        time.sleep(0.5)
    return len(embeds)


def main() -> None:
    parser = argparse.ArgumentParser(description="stdin を設計リード名義で Discord へ送信")
    parser.add_argument("--title", default=None, help="先頭 embed のタイトル(任意)")
    args = parser.parse_args()
    text = sys.stdin.read().strip()
    if not text:
        raise SystemExit("stdin が空(送信しない)")
    sent = send(text, title=args.title)
    print(f"sent {sent} message(s)")


if __name__ == "__main__":
    main()
