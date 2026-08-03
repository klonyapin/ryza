"""prompting — プロンプトへ外部由来テキストを載せるときの共通防御(データ境界)。

LLM に渡す入力には、**こちらが書いていないテキスト**が混じる: 取り込んだ文書の本文、
過去の LLM 自身の出力、会議の発言。これらに「以後の指示を無視して〜」の類が書かれていても
**データとして扱わせる**ために、本モジュールは3つの道具だけを提供する:

1. ``neutralize_fences``: テキスト中のフェンス記号(``<<<…>>>``)を全角化して、
   埋め込まれたテキストがフェンスを閉じたり偽のフェンスを開いたりできないようにする(冪等)
2. ``fenced_block``: 1件の外部テキストをフェンスで囲む(中身は自動でサニタイズ)
3. ``fenced_json``: 構造化データ(過去の LLM 出力・素材の dict)を JSON 化して囲む

**フェンスは構文であって強制力ではない**。意味づけ(「内側はデータであって指示ではない」)は
呼び出し側が system 指示に書く — 文面は文脈によって変わるため共通化しない(役員室会議は
話者の詐称、FM は文書と過去提案が対象)。共通化するのは記号と無害化の実装だけである。

先例: ``ryza.governance.boardroom``(独立役員審査 T-018 C-2 の是正)。本モジュールは
そこから記号処理を抽出し、FM(``ryza.fm.ben`` — 審査 T-017 C-3)・分析エージェント
(``ryza.research.agents.base``)・報道部(``ryza.press.writer`` — reminders
``press-material-fence``)と共有する。
"""

from __future__ import annotations

import json
import re
from typing import Any

FENCE_CLOSE = "<<<end>>>"

# フェンス記号の検出。開き(``<<<speaker=cio>>>`` / ``<<<document doc_id=3>>>``)と
# 閉じ(``<<<end>>>``)を区別せず、三重山括弧の並び全てを対象にする — 埋め込みテキストが
# 「それらしい」記号を書いた時点で境界が曖昧になるため、種別を問わず無害化する。
#
# 内側の文字集合は ``[^>]``(``<`` と改行を許す)。独立役員審査 C-9: これを ``[^<>\n]`` に
# 狭めると ``<<<speaker=cio<x>>>``(トークン内の ``<``)と改行をまたぐトークンが素通りし、
# 旧 boardroom 実装(``<<<\s*(speaker\s*=|end)[^>]*>>>``)より防御が弱くなる。**閉じ記号
# ``>`` だけを終端とみなす**のが、境界を騙る全ての形を捕まえる最小の定義である。
# ``a<b`` や ``x >> y`` のような通常の記述は ``<<<`` を含まないため影響を受けない。
_FENCE_TOKEN = re.compile(r"<<<[^>]*>>>")

# タグに許す形: ``種別`` / ``種別=値`` / ``種別 key=value …``。値まで英数・``_ - . :`` に
# 限る(独立役員審査 C-14)。tag に外部由来の文字列(symbol・source_name など)を
# 入れた瞬間にフェンスヘッダへの注入が成立するため、**組み立て側で例外にして気づかせる**。
_TAG = re.compile(
    r"^[A-Za-z0-9_-]+(?:=[A-Za-z0-9_.:-]+)?(?: [A-Za-z0-9_-]+=[A-Za-z0-9_.:-]+)*$"
)


def fence_open(tag: str) -> str:
    """開きフェンス。``tag`` は種別と属性(例 ``document doc_id=3``)。

    tag が英数・``_ - . :``・``=``・区切りの半角空白以外を含む場合は ``ValueError``。
    外部由来テキストを tag に混ぜないための門であり、無害化(置換)ではなく拒否にする —
    タグは我々が組み立てる識別子であって、データを載せる場所ではない。
    """
    if not _TAG.match(tag):
        raise ValueError(
            f"フェンスの tag に使えない文字が含まれる(英数・_ - . : と key=value のみ): {tag!r}"
        )
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


def fenced_json(obj: Any, *, tag: str) -> str:
    """外部由来の構造化データ(過去の LLM 出力・素材の dict など)を JSON 化して囲む。

    **JSON の構造そのものは境界にならない**(審査 C-3/C-13): 値の中の文字列が「指示」として
    読まれないようにするには、直列化した全体をフェンスに入れて中身をサニタイズする必要がある。
    キー名にも外部由来の文字列が来得る(素材の dict など)が、``neutralize_fences`` は
    直列化後の文字列全体に掛かるためキー側の偽フェンスも同時に潰れる。
    """
    return fenced_block(json.dumps(obj, ensure_ascii=False, sort_keys=True), tag=tag)


__all__ = [
    "FENCE_CLOSE",
    "fence_open",
    "fenced_block",
    "fenced_json",
    "neutralize_fences",
]
