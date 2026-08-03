"""schemas — 分析エージェントの ``scores`` の JSON Schema と最小バリデータ。

設計 20-research §4 の出力表を機械可読なスキーマに落とす。下流(戦略・報道部)は
``scores`` の構造化部分だけに依存する契約なので、保存前に必ず検証して契約違反を弾く。

``jsonschema`` パッケージは導入しない(依存を増やさない方針)。ここで使う語彙は
``type / properties / required / items / enum / minimum / maximum /
additionalProperties`` の狭い部分集合に限定し、自前の ``validate`` で検証する。
これで十分に scores の型・範囲・必須フィールドを機械検査できる。
"""

from __future__ import annotations

from typing import Any

# ── 各エージェントの scores スキーマ ─────────────────────────────────────────

# -1〜+1 のバイアス値の共通定義。
_BIAS = {"type": "number", "minimum": -1.0, "maximum": 1.0}
_UNIT = {"type": "number", "minimum": 0.0, "maximum": 1.0}
# 参照 doc_id の配列(リネージ・監査 A-13 の前提)。
_REFS = {"type": "array", "items": {"type": "integer"}}

MACRO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["regime", "rates_bias", "fx_bias", "refs"],
    "additionalProperties": True,
    "properties": {
        # 資産クラス別の regime 提案。値は自由語彙(risk_on/risk_off/tightening 等)。
        "regime": {"type": "object"},
        "rates_bias": _BIAS,
        "fx_bias": _BIAS,
        "confidence": _UNIT,
        "refs": _REFS,
    },
}

MICRO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["instruments", "refs"],
    "additionalProperties": True,
    "properties": {
        "instruments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["instrument_id", "impact", "materiality"],
                "additionalProperties": True,
                "properties": {
                    "instrument_id": {"type": "integer"},
                    "impact": _BIAS,
                    "materiality": _UNIT,
                    "catalyst": {"type": "string"},
                },
            },
        },
        "refs": _REFS,
    },
}

SENTIMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["by_asset_class", "refs"],
    "additionalProperties": True,
    "properties": {
        # 資産クラス別センチメント(-1〜+1)。
        "by_asset_class": {"type": "object"},
        # 銘柄別センチメント(任意)。
        "by_instrument": {"type": "object"},
        "anomaly": _UNIT,
        "refs": _REFS,
    },
}

# editor は「市場観の更新案(diff)」を出す。これは提案にすぎず、market_view.py の
# 決定論ルールだけがステートを変更する(LLM 直書き禁止)。
EDITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["regime_changes", "key_risk_ops", "refs"],
    "additionalProperties": True,
    "properties": {
        # 反転・追加の提案。{dimension: {"to": regime, "refs": [doc_id...]}}。
        "regime_changes": {"type": "object"},
        # 注目リスクの操作。add/update_confidence/resolve。
        "key_risk_ops": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["op", "risk_id"],
                "additionalProperties": True,
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["add", "update_confidence", "resolve"],
                    },
                    "risk_id": {"type": "string"},
                    "confidence": _UNIT,
                    "statement": {"type": "string"},
                    "observable": {"type": "string"},
                    "refs": _REFS,
                },
            },
        },
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "morning_topics": {"type": "array"},
        "refs": _REFS,
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "macro": MACRO_SCHEMA,
    "micro": MICRO_SCHEMA,
    "sentiment": SENTIMENT_SCHEMA,
    "editor": EDITOR_SCHEMA,
}


class SchemaError(ValueError):
    """scores がスキーマに適合しないときに送出する(検証失敗の詳細を保持)。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate(instance: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    """``instance`` を ``schema`` で検証し、エラーメッセージの一覧を返す(空 = 適合)。

    対応語彙: type / properties / required / items / enum / minimum / maximum /
    additionalProperties。未知のキーワードは無視する。
    """
    errors: list[str] = []
    _validate(instance, schema, path, errors)
    return errors


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def _is_type(value: Any, expected: str) -> bool:
    types = _TYPE_MAP[expected]
    # bool は int のサブクラスなので integer/number では弾く。
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, types)


def _validate(instance: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not _is_type(instance, expected_type):
        errors.append(f"{path}: 型が {expected_type} ではない({type(instance).__name__})")
        return  # 型不一致ならこれ以上の検査は無意味

    if expected_type == "object":
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 必須フィールド '{key}' が無い")
        props: dict[str, Any] = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, val in instance.items():
            if key in props:
                _validate(val, props[key], f"{path}.{key}", errors)
            elif additional is False:
                errors.append(f"{path}: 未知のフィールド '{key}'")

    elif expected_type == "array":
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, item in enumerate(instance):
                _validate(item, item_schema, f"{path}[{i}]", errors)

    enum = schema.get("enum")
    if enum is not None and instance not in enum:
        errors.append(f"{path}: 値 {instance!r} は {enum} のいずれでもない")

    if _is_type(instance, "number"):
        lo = schema.get("minimum")
        hi = schema.get("maximum")
        if lo is not None and instance < lo:
            errors.append(f"{path}: {instance} < minimum {lo}")
        if hi is not None and instance > hi:
            errors.append(f"{path}: {instance} > maximum {hi}")
