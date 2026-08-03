"""linter — 文体リンター（30-press §4 の L-1〜L-5・L-7）。

**純関数・LLM 不使用**。執筆モデルの構造化出力を決定論で検査し、不合格なら理由付きで
再生成させる（再生成は呼び出し側 ``morning``/``flash`` が制御）。L-6（抽象度タグの妥当性の
抜取検査）だけは軽量 LLM を要するため本モジュールに含めず、``sample_tag_check`` として別に置く。

入力（§4）: ``{topics: [{argument, sentences: [{text, level(1-5), source_ids[]}],
trade_implication, prediction}]}``。本モジュールはこの構造を型（``Topic``/``Sentence`` 等）で
受け、``lint_topic`` / ``lint_bulletin`` で検査する。

検査モード:

- ``"morning"``: 朝刊トピック。L-1・L-2（U字）・L-3（200-400字・4-8文）・L-4・L-7 を全適用。
- ``"flash"``: 速報の短縮形（§3 テンプレ「アーギュメント→レベル1根拠→レベル5含意」）。
  U字の先頭≥3 は課さず、L-1・L-4・末尾=5・（予兆なら）L-5 を課す。L-7 は任意。

U字判定（§4）: ``levels`` 配列に対し ①min 値が {1,2} ②min の最初の出現位置より前が非増加・
後が非減少 ③先頭≥3 ④末尾=5。理想形 4→3→2→1→3→5 を含む一般形。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 取引含意の action 語彙（§2 編集方針①: ロング/ショート/ウォッチ追加/様子見）。
TRADE_ACTIONS = frozenset({"long", "short", "watch", "hold"})

_WS_RE = re.compile(r"[\s　]+")


# ── 入力型（執筆モデルの構造化出力を写す）────────────────────────────────────────
@dataclass(frozen=True)
class Sentence:
    """本文 1 文。``level`` は抽象度（1=ファクト 〜 5=アーギュメント）。"""

    text: str
    level: int
    source_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class TradeImplication:
    """取引への含意（L-7）。action/対象/条件の 3 点。"""

    action: str
    target: str
    condition: str


@dataclass(frozen=True)
class Prediction:
    """予兆速報（速報②）の予測ラベル（L-5）。確度・検証期限つき。"""

    claim: str
    confidence: float  # 0-1
    verify_by: str  # ISO8601 文字列（検証期限）


@dataclass(frozen=True)
class Topic:
    """1 トピック（朝刊）または 1 速報。"""

    argument: str
    sentences: list[Sentence]
    trade_implication: TradeImplication | None = None
    prediction: Prediction | None = None
    title: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Topic:
        """執筆モデルの構造化出力（dict）から ``Topic`` を組む。"""
        sentences = [
            Sentence(
                text=str(s.get("text", "")),
                level=int(s.get("level", 0)),
                source_ids=[int(x) for x in (s.get("source_ids") or [])],
            )
            for s in (d.get("sentences") or [])
        ]
        ti_raw = d.get("trade_implication")
        ti = (
            TradeImplication(
                action=str(ti_raw.get("action", "")),
                target=str(ti_raw.get("target", "")),
                condition=str(ti_raw.get("condition", "")),
            )
            if isinstance(ti_raw, dict)
            else None
        )
        pr_raw = d.get("prediction")
        pr = (
            Prediction(
                claim=str(pr_raw.get("claim", "")),
                confidence=float(pr_raw.get("confidence", 0.0)),
                verify_by=str(pr_raw.get("verify_by", "")),
            )
            if isinstance(pr_raw, dict)
            else None
        )
        return cls(
            argument=str(d.get("argument", "")),
            sentences=sentences,
            trade_implication=ti,
            prediction=pr,
            title=str(d.get("title", "")),
        )


# ── 検査結果型 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Violation:
    """1 件のリンター違反。``rule`` は L-1〜L-7、``message`` は再生成プロンプト用の理由。"""

    rule: str
    message: str


@dataclass(frozen=True)
class LintReport:
    """トピック 1 件の検査結果。"""

    ok: bool
    violations: list[Violation]

    def reasons(self) -> str:
        """再生成プロンプトに載せる理由文字列。"""
        return "; ".join(f"[{v.rule}] {v.message}" for v in self.violations)


# ── U字判定（§4）──────────────────────────────────────────────────────────────
def is_u_shape(levels: list[int]) -> bool:
    """抽象度系列が U字（単谷・先頭≥3・末尾=5・谷∈{1,2}）か。純関数。"""
    if not levels:
        return False
    if levels[0] < 3:
        return False
    if levels[-1] != 5:
        return False
    lo = min(levels)
    if lo not in (1, 2):
        return False
    i = levels.index(lo)  # 谷（min）の最初の出現位置
    # 谷より前（0..i）は非増加。
    for a, b in zip(levels[:i], levels[1 : i + 1], strict=False):
        if b > a:
            return False
    # 谷より後（i..末尾）は非減少。
    for a, b in zip(levels[i:], levels[i + 1 :], strict=False):
        if b < a:
            return False
    return True


def _non_ws_len(text: str) -> int:
    return len(_WS_RE.sub("", text))


# ── 個別検査 ──────────────────────────────────────────────────────────────────
def check_argument(topic: Topic) -> list[Violation]:
    """L-1: argument（level=5 相当の一文）が存在し本文と重複しない。"""
    arg = topic.argument.strip()
    if not arg:
        return [Violation("L-1", "argument（アーギュメント一文）が空")]
    for s in topic.sentences:
        if s.text.strip() == arg:
            return [Violation("L-1", "argument が本文の一文と重複している")]
    return []


def check_u_shape(topic: Topic) -> list[Violation]:
    """L-2: 本文 level 系列が U字（先頭≥3・単谷∈{1,2}・末尾=5）。"""
    levels = [s.level for s in topic.sentences]
    if any(lv < 1 or lv > 5 for lv in levels):
        return [Violation("L-2", f"level が 1-5 の範囲外: {levels}")]
    if not is_u_shape(levels):
        return [Violation("L-2", f"抽象度系列が U字（4→3→2→1→3→5 型）でない: {levels}")]
    return []


def check_length(
    topic: Topic, *, min_chars: int = 200, max_chars: int = 400,
    min_sentences: int = 4, max_sentences: int = 8,
) -> list[Violation]:
    """L-3: 分量（空白除き min_chars〜max_chars 字）と文数（min〜max 文）。"""
    out: list[Violation] = []
    n = len(topic.sentences)
    if n < min_sentences or n > max_sentences:
        out.append(Violation("L-3", f"文数 {n} が範囲 {min_sentences}-{max_sentences} 外"))
    chars = sum(_non_ws_len(s.text) for s in topic.sentences)
    if chars < min_chars or chars > max_chars:
        out.append(Violation("L-3", f"字数 {chars}（空白除く）が範囲 {min_chars}-{max_chars} 外"))
    return out


def check_sources(
    topic: Topic, *, valid_source_ids: set[int] | None = None
) -> list[Violation]:
    """L-4: level 1 の全文に source_ids≥1。存在しない ID は不合格。"""
    out: list[Violation] = []
    for i, s in enumerate(topic.sentences):
        if s.level == 1 and not s.source_ids:
            out.append(Violation("L-4", f"level 1 の文 #{i} に出典 source_ids が無い"))
        if valid_source_ids is not None:
            unknown = [sid for sid in s.source_ids if sid not in valid_source_ids]
            if unknown:
                out.append(Violation("L-4", f"文 #{i} が存在しない出典 ID を参照: {unknown}"))
    return out


def check_prediction(topic: Topic, *, required: bool) -> list[Violation]:
    """L-5: 速報②で「確度・検証期限」欠落を拒否。

    ``required=True`` のとき prediction が存在し confidence(0-1) と verify_by を持つこと。
    """
    if not required:
        return []
    pr = topic.prediction
    if pr is None:
        return [Violation("L-5", "予兆速報に prediction（確度・検証期限）が無い")]
    out: list[Violation] = []
    if not (0.0 <= pr.confidence <= 1.0):
        out.append(Violation("L-5", f"confidence {pr.confidence} が 0-1 の範囲外"))
    if not pr.verify_by.strip():
        out.append(Violation("L-5", "verify_by（検証期限）が空"))
    if not pr.claim.strip():
        out.append(Violation("L-5", "claim（予測内容）が空"))
    return out


def check_trade_implication(topic: Topic) -> list[Violation]:
    """L-7: trade_implication（action/対象/条件）が全て埋まっている。"""
    ti = topic.trade_implication
    if ti is None:
        return [Violation("L-7", "trade_implication（取引への含意）が無い")]
    out: list[Violation] = []
    if ti.action not in TRADE_ACTIONS:
        out.append(Violation("L-7", f"action '{ti.action}' が {sorted(TRADE_ACTIONS)} でない"))
    if not ti.target.strip():
        out.append(Violation("L-7", "trade_implication.target（対象）が空"))
    if not ti.condition.strip():
        out.append(Violation("L-7", "trade_implication.condition（条件）が空"))
    return out


# ── まとめ検査 ─────────────────────────────────────────────────────────────────
def lint_topic(
    topic: Topic,
    *,
    mode: str = "morning",
    valid_source_ids: set[int] | None = None,
    min_chars: int = 200,
    max_chars: int = 400,
    min_sentences: int = 4,
    max_sentences: int = 8,
    is_prediction: bool = False,
) -> LintReport:
    """1 トピックを検査する。

    - ``mode="morning"``: L-1・L-2（U字）・L-3・L-4・L-7 を全適用。
    - ``mode="flash"``: 短縮形。L-1・L-4・末尾=5・（``is_prediction`` なら）L-5 を適用。
      U字の先頭≥3 と L-3 の字数下限、L-7 は課さない（§3 の速報テンプレに合わせる）。
    """
    v: list[Violation] = []
    v += check_argument(topic)  # L-1（両モード共通）
    v += check_sources(topic, valid_source_ids=valid_source_ids)  # L-4（両モード共通）

    if mode == "morning":
        v += check_u_shape(topic)  # L-2
        v += check_length(
            topic, min_chars=min_chars, max_chars=max_chars,
            min_sentences=min_sentences, max_sentences=max_sentences,
        )  # L-3
        v += check_trade_implication(topic)  # L-7
    elif mode == "flash":
        v += _check_flash_shape(topic)  # 末尾=5 の含意一文
        v += check_prediction(topic, required=is_prediction)  # L-5
    else:  # pragma: no cover - 呼び出し側のバグ
        raise ValueError(f"未知の lint mode: {mode}")

    return LintReport(ok=not v, violations=v)


def _check_flash_shape(topic: Topic) -> list[Violation]:
    """速報短縮形: 少なくとも level 1 の根拠を 1 文含み、末尾が level 5 含意。"""
    out: list[Violation] = []
    levels = [s.level for s in topic.sentences]
    if not levels:
        return [Violation("L-2", "速報本文が空")]
    if any(lv < 1 or lv > 5 for lv in levels):
        out.append(Violation("L-2", f"level が 1-5 の範囲外: {levels}"))
    if 1 not in levels:
        out.append(Violation("L-2", "速報にレベル1（ファクト根拠）の文が無い"))
    if levels[-1] != 5:
        out.append(Violation("L-2", f"速報末尾が level 5（含意）でない: {levels[-1]}"))
    return out


def lint_bulletin(
    topics: list[Topic],
    *,
    mode: str = "morning",
    valid_source_ids: set[int] | None = None,
    max_topics: int = 5,
    **kwargs: Any,
) -> dict[int, LintReport]:
    """複数トピックを一括検査し index→LintReport を返す。

    ``mode="morning"`` では ``max_topics`` 超過を index=-1 のレポートで通知する。
    """
    reports: dict[int, LintReport] = {}
    if mode == "morning" and len(topics) > max_topics:
        reports[-1] = LintReport(
            ok=False,
            violations=[Violation("L-3", f"トピック数 {len(topics)} が上限 {max_topics} 超")],
        )
    for i, t in enumerate(topics):
        reports[i] = lint_topic(
            t, mode=mode, valid_source_ids=valid_source_ids, **kwargs
        )
    return reports
