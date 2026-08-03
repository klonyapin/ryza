r"""tagger — 銘柄・エンティティタグ付け（階層0・LLM 非依存）。

設計 20-research §3 ④「銘柄・エンティティタグ（銘柄コード辞書・社名辞書のマッチ）」。
辞書は ``market.instruments`` から生成する。

## 辞書の構成

``market.instruments`` の現行行（``valid_to IS NULL``）から:
- **証券コード**: 日本株の ``symbol``（例 ``'7203.T'``）から数値コード ``'7203'`` を抽出し、
  4 桁コードとして辞書化する。全 symbol そのもの（``'7203.T'`` / ``'AAPL'``）もキーにする。

``instruments`` には社名列が無いため、**社名辞書は config で補う**（``name_map``: 社名→symbol）。
既定は空。社名対応表が用意でき次第、runner から渡す（設計の「社名辞書」を満たす拡張口）。

## マッチ

本文中の 4 桁証券コードは ``\b\d{4}\b`` の境界付きで拾い、辞書に存在するものだけ採用する
（誤検出抑制）。symbol・社名は部分文字列一致。マッチした表層文字列も根拠として残す。

読み取りのみ（辞書生成時に ``conn`` を使う）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg

# 日本株 symbol（例 '7203.T'）から数値コードを取り出す。
_JP_CODE_RE = re.compile(r"^(\d{4})\b")
# 本文中の 4 桁コード候補（境界付き）。
_FOUR_DIGIT_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


@dataclass(frozen=True)
class InstrumentDict:
    """銘柄辞書。表層キー → instrument_id の 3 索引を持つ。"""

    by_code: dict[str, int] = field(default_factory=dict)    # '7203' -> id
    by_symbol: dict[str, int] = field(default_factory=dict)  # '7203.T' / 'AAPL' -> id
    by_name: dict[str, int] = field(default_factory=dict)    # 社名 -> id（config 由来）

    def is_empty(self) -> bool:
        return not (self.by_code or self.by_symbol or self.by_name)


@dataclass(frozen=True)
class TagResult:
    """タグ付け結果。

    - ``instrument_ids``: マッチした銘柄 ID（重複なし・昇順）。
    - ``matched``: 根拠（どの表層文字列がどの種別・ID にマッチしたか）。
    """

    instrument_ids: list[int] = field(default_factory=list)
    matched: list[dict[str, str | int]] = field(default_factory=list)


def build_dictionary(
    conn: psycopg.Connection,
    name_map: dict[str, str] | None = None,
) -> InstrumentDict:
    """``market.instruments`` の現行行から銘柄辞書を生成する。

    ``name_map`` は社名→symbol の補助辞書（``instruments`` に社名列が無いため config で補う）。
    与えた symbol が現行 instruments に存在すればその id で社名索引に加える。
    """
    by_code: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_id, symbol FROM market.instruments WHERE valid_to IS NULL"
        )
        rows = cur.fetchall()
    for instrument_id, symbol in rows:
        by_symbol[symbol] = instrument_id
        m = _JP_CODE_RE.match(symbol)
        if m:
            by_code[m.group(1)] = instrument_id

    by_name: dict[str, int] = {}
    for name, symbol in (name_map or {}).items():
        iid = by_symbol.get(symbol)
        if iid is not None:
            by_name[name] = iid
    return InstrumentDict(by_code=by_code, by_symbol=by_symbol, by_name=by_name)


def tag(text: str, dictionary: InstrumentDict) -> TagResult:
    """テキストから銘柄を抽出する。

    ① 4 桁証券コード（境界付き・辞書に存在するもののみ）
    ② symbol の部分文字列一致
    ③ 社名の部分文字列一致
    """
    if not text or dictionary.is_empty():
        return TagResult()

    ids: dict[int, None] = {}  # 挿入順を保ちつつ重複排除
    matched: list[dict[str, str | int]] = []

    def add(iid: int, surface: str, kind: str) -> None:
        if iid not in ids:
            ids[iid] = None
        matched.append({"instrument_id": iid, "surface": surface, "kind": kind})

    # ① 4 桁コード
    for m in _FOUR_DIGIT_RE.finditer(text):
        code = m.group(1)
        iid = dictionary.by_code.get(code)
        if iid is not None:
            add(iid, code, "code")

    # ② symbol（'7203.T' や 'AAPL' 等）。短すぎる誤爆を避け 2 文字以上のみ。
    for symbol, iid in dictionary.by_symbol.items():
        if len(symbol) >= 2 and symbol in text:
            add(iid, symbol, "symbol")

    # ③ 社名
    for name, iid in dictionary.by_name.items():
        if name and name in text:
            add(iid, name, "name")

    return TagResult(instrument_ids=sorted(ids.keys()), matched=matched)
