"""scores の JSON Schema と最小バリデータの単体テスト(DB 非依存・決定論)。"""

from __future__ import annotations

from ryza.research.schemas import (
    EDITOR_SCHEMA,
    MACRO_SCHEMA,
    MICRO_SCHEMA,
    SENTIMENT_SCHEMA,
    validate,
)


def test_macro_valid():
    scores = {"regime": {"jp_equity": "risk_on"}, "rates_bias": 0.3,
              "fx_bias": -0.2, "confidence": 0.6, "refs": [1, 2]}
    assert validate(scores, MACRO_SCHEMA) == []


def test_macro_missing_required():
    errors = validate({"regime": {}}, MACRO_SCHEMA)
    assert any("rates_bias" in e for e in errors)
    assert any("fx_bias" in e for e in errors)
    assert any("refs" in e for e in errors)


def test_macro_bias_out_of_range():
    scores = {"regime": {}, "rates_bias": 1.5, "fx_bias": 0.0, "refs": [1]}
    errors = validate(scores, MACRO_SCHEMA)
    assert any("maximum" in e for e in errors)


def test_number_rejects_bool():
    # bool は number として弾く(True が 1.0 扱いされない)。
    scores = {"regime": {}, "rates_bias": True, "fx_bias": 0.0, "refs": [1]}
    errors = validate(scores, MACRO_SCHEMA)
    assert any("rates_bias" in e for e in errors)


def test_refs_must_be_integers():
    scores = {"regime": {}, "rates_bias": 0.0, "fx_bias": 0.0, "refs": ["x"]}
    errors = validate(scores, MACRO_SCHEMA)
    assert any("refs[0]" in e for e in errors)


def test_micro_nested_items():
    scores = {"instruments": [{"instrument_id": 10, "impact": 0.5, "materiality": 0.9}],
              "refs": [1]}
    assert validate(scores, MICRO_SCHEMA) == []
    bad = {"instruments": [{"instrument_id": 10, "impact": 2.0, "materiality": 0.9}],
           "refs": [1]}
    assert validate(bad, MICRO_SCHEMA)


def test_sentiment_valid():
    scores = {"by_asset_class": {"jp_equity": -0.4}, "anomaly": 0.2, "refs": [3]}
    assert validate(scores, SENTIMENT_SCHEMA) == []


def test_editor_enum_op():
    good = {"regime_changes": {}, "key_risk_ops": [
        {"op": "add", "risk_id": "r1", "confidence": 0.5, "refs": [1]}], "refs": [1]}
    assert validate(good, EDITOR_SCHEMA) == []
    bad = {"regime_changes": {}, "key_risk_ops": [
        {"op": "frobnicate", "risk_id": "r1"}], "refs": [1]}
    errors = validate(bad, EDITOR_SCHEMA)
    assert any("enum" in e or "いずれでもない" in e for e in errors)
