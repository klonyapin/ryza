"""embeds — Discord embed 組立（純関数・30-press §6）。

色・免責フッター・画像（URL 参照＋クレジット）・取引含意を Discord embed(dict) に組む。
Bot（``src/ryza/bot/outbox.py``）はこの dict を ``press.outbox.embed_json`` から読んで配送する。
本モジュールは discord API に一切依存しない。
"""

from __future__ import annotations

from typing import Any

from ryza.press.images import ImageResult
from ryza.press.linter import Topic

# embed 色（§6）。#RRGGBB を int 化。
COLOR_NORMAL = 0x5B54C7  # 紫（朝刊・通常）
COLOR_FLASH = 0xC24E3A  # 赤（速報）
COLOR_APPROVAL = 0x2E7D5B  # 緑（承認）

# 免責フッター（全投稿・§6 / 00-system-design §7）。
DISCLAIMER = "本投稿は自己運用システムの内部記録であり投資助言ではない（完全非公開）"

# 取引含意 action の日本語表示。
_ACTION_JA = {
    "long": "ロング",
    "short": "ショート",
    "watch": "ウォッチ追加",
    "hold": "様子見",
}


def _footer_text(image: ImageResult | None) -> str:
    """免責＋画像クレジット（§6 共通規則③）を footer にまとめる。"""
    text = DISCLAIMER
    if image is not None and image.artist:
        text += f" ｜ 画像: {image.artist}（{image.source}）"
    return text


def _topic_body(topic: Topic) -> str:
    """argument + 本文 + 取引含意を 1 トピックの本文文字列にする。"""
    lines = [f"**{topic.argument.strip()}**", ""]
    lines.append("".join(s.text for s in topic.sentences))
    ti = topic.trade_implication
    if ti is not None:
        action = _ACTION_JA.get(ti.action, ti.action)
        lines.append("")
        lines.append(f"→ 取引への含意: **{action}** ／ 対象: {ti.target} ／ 条件: {ti.condition}")
    return "\n".join(lines)


def build_morning_embed(
    topics: list[Topic],
    *,
    title: str = "Ryza 朝刊",
    image: ImageResult | None = None,
    events: list[dict[str, Any]] | None = None,
    nav: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """朝刊 embed を組む（トピック→本日の注目→ポートフォリオ概況）。

    - ``image``: マスコット/サムネイル（URL 参照・クレジットは footer）。取得失敗時 None なら画像なし。
    - ``events``: 本日の注目イベント表 [{title, at}]。
    - ``nav``: ポートフォリオ概況 {value, change_pct, provisional}。確定値のみ（provisional は明示）。
    """
    fields: list[dict[str, Any]] = []
    for i, t in enumerate(topics, 1):
        name = f"{i}. {t.title or t.argument[:24]}"
        fields.append({"name": name[:256], "value": _topic_body(t)[:1024], "inline": False})

    if events:
        lines = [f"・{e.get('at', '')} {e.get('title', '')}".strip() for e in events]
        fields.append({"name": "本日の注目", "value": "\n".join(lines)[:1024], "inline": False})

    if nav is not None:
        fields.append({"name": "ポートフォリオ概況", "value": _nav_line(nav)[:1024], "inline": False})

    embed: dict[str, Any] = {
        "title": title,
        "color": COLOR_NORMAL,
        "fields": fields,
        "footer": {"text": _footer_text(image)},
    }
    if timestamp:
        embed["timestamp"] = timestamp
    if image is not None:
        embed["image"] = {"url": image.url}
    return embed


def build_flash_embed(
    topic: Topic,
    *,
    is_prediction: bool = False,
    image: ImageResult | None = None,
    mention: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """速報 embed を組む（赤・§3）。予兆速報は「予測」ラベル＋確度＋検証期限を明示。"""
    label = "【速報②・予測】" if is_prediction else "【速報】"
    title = f"{label}{topic.title or topic.argument[:40]}"
    body = _topic_body(topic)
    if is_prediction and topic.prediction is not None:
        pr = topic.prediction
        body += (
            f"\n\n― 予測ラベル ―\n確度: {pr.confidence:.0%} ／ 検証期限: {pr.verify_by}\n{pr.claim}"
        )
    embed: dict[str, Any] = {
        "title": title[:256],
        "description": body[:4096],
        "color": COLOR_FLASH,
        "footer": {"text": _footer_text(image)},
    }
    if mention:
        embed["content"] = mention  # Bot が @メンションとして扱う
    if timestamp:
        embed["timestamp"] = timestamp
    if image is not None:
        embed["image"] = {"url": image.url}
    return embed


def build_digest_embed(
    topics: list[Topic],
    *,
    image: ImageResult | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """まとめ速報 embed（レート上限超過時の統合・§3）。複数トリガを1本に束ねる。"""
    lines: list[str] = []
    for i, t in enumerate(topics, 1):
        lines.append(f"**{i}. {t.argument.strip()}**")
        lines.append("".join(s.text for s in topic_sentences(t)))
        lines.append("")
    embed: dict[str, Any] = {
        "title": f"【まとめ速報】{len(topics)}件",
        "description": "\n".join(lines)[:4096],
        "color": COLOR_FLASH,
        "footer": {"text": _footer_text(image)},
    }
    if timestamp:
        embed["timestamp"] = timestamp
    if image is not None:
        embed["image"] = {"url": image.url}
    return embed


def topic_sentences(topic: Topic) -> list[Any]:
    return topic.sentences


def _nav_line(nav: dict[str, Any]) -> str:
    """ポートフォリオ概況の 1 行（確定値のみ。provisional は「暫定」明記・§2）。"""
    value = nav.get("value")
    change = nav.get("change_pct")
    prov = " ※暫定" if nav.get("provisional") else ""
    parts: list[str] = []
    if value is not None:
        parts.append(f"NAV ¥{value:,.0f}")
    if change is not None:
        parts.append(f"前日比 {change:+.2f}%")
    return (" ／ ".join(parts) or "データなし") + prov
