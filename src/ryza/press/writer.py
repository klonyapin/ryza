"""writer — 記事執筆（StructuredLLM で玲音の口調＋U字構造を構造化出力）。

出力契約（30-press §2・§4）:
``{argument, sentences: [{text, level(1-5), source_ids[]}], trade_implication, prediction?}``

**執筆規格が人格より優先**（personas/press-lain/charter §位置づけ）。玲音の口調（voice.md）は
system プロンプトに注入するが、U字構造・出典・取引含意・リンター検査には作用させない。
実 LLM は ``StructuredLLM`` 経由のみ（部門タグ ``press``・コスト記録）。テストは
``FixtureProvider`` を注入する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ryza.press.linter import Topic
from ryza.research.llm import LLMResult, StructuredLLM

MODEL_TIER = "mid"
_PERSONA_ROOT = Path(__file__).resolve().parents[3] / "personas"

# 抽象度タグ（1-5）付き 1 文のスキーマ。
_SENTENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["text", "level", "source_ids"],
    "additionalProperties": True,
    "properties": {
        "text": {"type": "string"},
        "level": {"type": "integer", "minimum": 1, "maximum": 5},
        "source_ids": {"type": "array", "items": {"type": "integer"}},
    },
}

_TRADE_IMPL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action", "target", "condition"],
    "additionalProperties": True,
    "properties": {
        "action": {"type": "string"},  # long|short|watch|hold
        "target": {"type": "string"},
        "condition": {"type": "string"},
    },
}

_PREDICTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["claim", "confidence", "verify_by"],
    "additionalProperties": True,
    "properties": {
        "claim": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "verify_by": {"type": "string"},
    },
}

# 朝刊トピックのスキーマ（trade_implication 必須）。
MORNING_TOPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["argument", "sentences", "trade_implication"],
    "additionalProperties": True,
    "properties": {
        "argument": {"type": "string"},
        "sentences": {"type": "array", "items": _SENTENCE_SCHEMA},
        "trade_implication": _TRADE_IMPL_SCHEMA,
        "title": {"type": "string"},
    },
}

# 速報のスキーマ（trade_implication 任意・prediction は速報②で必須）。
FLASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["argument", "sentences"],
    "additionalProperties": True,
    "properties": {
        "argument": {"type": "string"},
        "sentences": {"type": "array", "items": _SENTENCE_SCHEMA},
        "trade_implication": _TRADE_IMPL_SCHEMA,
        "prediction": _PREDICTION_SCHEMA,
        "title": {"type": "string"},
    },
}


def load_press_persona() -> str:
    """玲音の charter+口調ガイドを結合して system プロンプトに載せる。"""
    parts: list[str] = []
    for name in ("charter.md", "voice.md"):
        p = _PERSONA_ROOT / "press-lain" / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


_WRITING_RULES = (
    "【執筆規格（人格より優先）】"
    "各トピックは argument（アーギュメント一文・level5 相当）を持ち、本文 sentences は "
    "抽象度 level(1-5) を U字（先頭≥3→谷 level1 または2→末尾 level5）で並べる。"
    "level1（ファクト）の文には必ず source_ids（doc_id）を付ける。"
    "朝刊は 200〜400字・4〜8文、全トピックに trade_implication(action/target/condition) を付す。"
    "action は long|short|watch|hold のいずれか。"
    "玲音の口調は語り口にのみ作用させ、構造・出典・含意には作用させない。"
)

_FLASH_RULES = (
    "【速報の短縮形】argument（一文）→ level1 の根拠（出典必須・複数可）→ level5 の含意一文。"
    "予兆速報（複数の弱いシグナルの同方向一致）では prediction(claim/confidence(0-1)/verify_by) "
    "を必ず付け『予測』であることを明示する。"
)


@dataclass(frozen=True)
class WriteResult:
    """執筆 1 回の結果。``topic`` はリンター入力、``llm`` はコスト・リトライ記録。"""

    topic: Topic
    llm: LLMResult
    raw: dict[str, Any]


def _build_prompt(
    *,
    task: str,
    material: dict[str, Any],
    feedback: str | None,
) -> str:
    payload: dict[str, Any] = {"task": task, "material": material}
    if feedback:
        payload["previous_lint_feedback"] = feedback
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_topic(
    llm: StructuredLLM,
    material: dict[str, Any],
    *,
    model: str = "mid-default",
    feedback: str | None = None,
) -> WriteResult:
    """朝刊トピックを 1 件執筆する（構造化出力）。

    ``material`` は素材（候補のタイトル・要約・refs・市場観抜粋など）。``feedback`` は前回の
    リンター違反理由（再生成時に注入し、同じ違反を繰り返させない）。
    """
    system = load_press_persona() + "\n\n" + _WRITING_RULES
    task = "以下の素材から朝刊トピックを1件、玲音の口調で執筆し構造化出力せよ。"
    prompt = _build_prompt(task=task, material=material, feedback=feedback)
    result = llm.complete(
        system=system, user=prompt, schema=MORNING_TOPIC_SCHEMA,
        task_type="press.morning.write", model_tier=MODEL_TIER, model=model,
    )
    return WriteResult(topic=Topic.from_dict(result.content), llm=result, raw=result.content)


def write_flash(
    llm: StructuredLLM,
    material: dict[str, Any],
    *,
    is_prediction: bool = False,
    model: str = "mid-default",
    feedback: str | None = None,
) -> WriteResult:
    """速報を 1 件執筆する（短縮テンプレ）。``is_prediction`` で予兆速報の指示を切替える。"""
    system = load_press_persona() + "\n\n" + _WRITING_RULES + "\n\n" + _FLASH_RULES
    kind = "速報②（予兆・予測ラベル必須）" if is_prediction else "速報①（発生済みの事実）"
    task = f"以下のトリガから{kind}を1件、玲音の口調で執筆し構造化出力せよ。"
    prompt = _build_prompt(task=task, material=material, feedback=feedback)
    result = llm.complete(
        system=system, user=prompt, schema=FLASH_SCHEMA,
        task_type="press.flash.write", model_tier=MODEL_TIER, model=model,
    )
    return WriteResult(topic=Topic.from_dict(result.content), llm=result, raw=result.content)
