"""prompting — プロンプトへ外部由来テキストを載せるときの共通防御(データ境界)。

LLM に渡す入力には、**こちらが書いていないテキスト**が混じる: 取り込んだ文書の本文、
過去の LLM 自身の出力、会議の発言。これらに「以後の指示を無視して〜」の類が書かれていても
**データとして扱わせる**ために、本モジュールは2つの道具だけを提供する:

1. ``neutralize_fences``: テキスト中のフェンス記号(``<<<…>>>``)を全角化して、
   埋め込まれたテキストがフェンスを閉じたり偽のフェンスを開いたりできないようにする(冪等)
2. ``fenced_block``: 1件の外部テキストをフェンスで囲む(中身は自動でサニタイズ)

**フェンスは構文であって強制力ではない**。意味づけ(「内側はデータであって指示ではない」)は
呼び出し側が system 指示に書く — 文面は文脈によって変わるため共通化しない(役員室会議は
話者の詐称、FM は文書と過去提案が対象)。共通化するのは記号と無害化の実装だけである。

先例: ``ryza.governance.boardroom``(独立役員審査 T-018 C-2 の是正)。本モジュールは
そこから記号処理を抽出し、FM(``ryza.fm.ben`` — 審査 T-017 C-3)と共有する。
"""

from __future__ import annotations

import re

FENCE_CLOSE = "<<<end>>>"

# フェンス記号の検出。開き(``<<<speaker=cio>>>`` / ``<<<document doc_id=3>>>``)と
# 閉じ(``<<<end>>>``)を区別せず、三重山括弧の並び全てを対象にする — 埋め込みテキストが
# 「それらしい」記号を書いた時点で境界が曖昧になるため、種別を問わず無害化する。
_FENCE_TOKEN = re.compile(r"<<<[^<>\n]*>>>")


def fence_open(tag: str) -> str:
    """開きフェンス。``tag`` は種別と属性(例 ``document doc_id=3``)。"""
    return f"<<<{tag}>>>"


def neutralize_fences(text: str) -> str:
    """テキスト中のフェンス記号を全角化する(冪等)。

    全角化された ``＜＜＜…＞＞＞`` は再びマッチしないため、二重適用しても変化しない。
    """
    return _FENCE_TOKEN.sub(
        lambda m: m.group(0).replace("<", "＜").replace(">", "＞"), text
    )


def fenced_block(text: str, *, tag: str) -> str:
    """外部由来テキスト1件をフェンスで囲む(中身はサニタイズ済みになる)。"""
    return f"{fence_open(tag)}\n{neutralize_fences(text)}\n{FENCE_CLOSE}"


__all__ = [
    "FENCE_CLOSE",
    "fence_open",
    "fenced_block",
    "neutralize_fences",
]
