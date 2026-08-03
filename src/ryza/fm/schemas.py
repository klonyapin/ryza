"""schemas — FM の LLM 構造化出力スキーマ(T-017)。

``ryza.research.schemas`` と同じ狭い語彙(type/properties/required/items/enum/…)で書き、
同じ ``validate`` で検証する(``StructuredLLM.complete`` が呼ぶ)。

スキーマで**構造的に禁止している**こと:

- ``direction`` の enum を ``["buy"]`` に固定 — 第一陣は long-only。LLM が ``short`` を
  返せばスキーマ検証で落ちる(コード側の防御より前に、契約として不可能にする)
- 確信度・スコア・数量・金額のフィールドを**定義しない**。定義しなければ下流が使えない
  (不変原則1: LLM の確信度をサイズにしない)。仮に追加プロパティとして返してきても
  ``ben.py`` は読まない
- ``evidence_refs`` と ``invalidation_md`` を required に — 証憑と反証条件のない提案は
  スキーマ違反(40-fund-managers.md §制約1)
"""

from __future__ import annotations

from typing import Any

from ryza.fm.theses import EVIDENCE_KINDS

_EVIDENCE_REF: dict[str, Any] = {
    "type": "object",
    "required": ["kind"],
    "additionalProperties": True,  # doc_id / report_id / instrument_id+ts は kind 依存
    "properties": {"kind": {"type": "string", "enum": list(EVIDENCE_KINDS)}},
}

_CANDIDATE: dict[str, Any] = {
    "type": "object",
    "required": [
        "instrument_id", "direction", "thesis_md", "evidence_refs", "invalidation_md"
    ],
    "additionalProperties": True,
    "properties": {
        "instrument_id": {"type": "integer"},
        # long-only(第一陣)。short はスキーマ上返せない。
        "direction": {"type": "string", "enum": ["buy"]},
        "thesis_md": {"type": "string"},
        "evidence_refs": {"type": "array", "items": _EVIDENCE_REF},
        "invalidation_md": {"type": "string"},
    },
}

_REVIEW: dict[str, Any] = {
    "type": "object",
    "required": ["instrument_id", "invalidated", "rationale_md", "evidence_refs"],
    "additionalProperties": True,
    "properties": {
        "instrument_id": {"type": "integer"},
        # 建玉時の反証条件が成立したか(true なら全量手仕舞い)。
        "invalidated": {"type": "boolean"},
        "rationale_md": {"type": "string"},
        "evidence_refs": {"type": "array", "items": _EVIDENCE_REF},
    },
}

BEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["candidates", "reviews"],
    "additionalProperties": True,
    "properties": {
        "candidates": {"type": "array", "items": _CANDIDATE},
        "reviews": {"type": "array", "items": _REVIEW},
    },
}

__all__ = ["BEN_SCHEMA"]
