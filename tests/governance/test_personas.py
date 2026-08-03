"""着任ローダ(src/ryza/governance/personas.py)の純関数部分のテスト。

DB を使わない: 役職資産の読み込みと着任プロンプトの組み立ては決定論の
ファイル読み+文字列連結であることを検証する。DB 依存(stances の書込/読出)は
test_governance_schema.py が担う。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ryza.governance.personas import (
    OFFICER_ROLES,
    PersonaAssets,
    Stance,
    build_onboarding_prompt,
    load_persona_assets,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── リポジトリ実体の役職資産 ────────────────────────────────────────────────
@pytest.mark.parametrize("role", OFFICER_ROLES)
def test_repo_persona_assets_complete(role):
    """3役職とも charter + system が存在し、空でない。"""
    assets = load_persona_assets(role)
    assert assets.role == role
    assert len(assets.charter) > 100
    assert len(assets.system) > 100


def test_charters_contain_grounded_duties():
    """charter に設計書由来の要点が入っている(発明でなく 05/定款からの転記)。"""
    cio = load_persona_assets("cio")
    ind = load_persona_assets("independent_officer")
    aud = load_persona_assets("audit")
    # CIO: 利益相反 — 自分が起草した提案の承認に関与しない(05 §6-4)
    assert "起草" in cio.charter and "承認に関与しない" in cio.charter
    # 独立役員: 毎回最低1つの懸念(05 §3)・的中率評価(05 §6-2)・記憶の非共有
    assert "最低1つの懸念" in ind.charter
    assert "的中率" in ind.charter
    assert "共有" in ind.charter
    # 監査: read-only・代表直接報告・Ver.3.0(定款第8条)
    assert "read-only" in aud.charter
    assert "Ver.3.0" in aud.charter
    assert "直接報告" in aud.charter


def test_role_name_normalization():
    """ハイフン/アンダースコアどちらの表記でも同じ資産に解決する。"""
    a = load_persona_assets("independent-officer")
    b = load_persona_assets("independent_officer")
    assert a == b
    assert a.role == "independent_officer"  # DB 側表記に正規化


def test_missing_assets_raise(tmp_path):
    """charter か system が欠けた役職では着任できない(暗黙の空 charter 禁止)。"""
    (tmp_path / "cio").mkdir()
    (tmp_path / "cio" / "system.md").write_text("人格のみ", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="charter"):
        load_persona_assets("cio", persona_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        load_persona_assets("unknown-role", persona_root=tmp_path)


# ── 着任プロンプトの組み立て(純関数)────────────────────────────────────────
def _assets() -> PersonaAssets:
    return PersonaAssets(role="cio", charter="CHARTER本文", system="SYSTEM本文")


def test_build_onboarding_prompt_order_and_sections():
    """system → charter → stances の順で、決定論的に組み上がる。"""
    stances = [
        Stance(stance_id=2, role="cio", kind="concern", summary="コスト計上漏れの懸念",
               stated_at=datetime(2026, 8, 2, tzinfo=UTC)),
        Stance(stance_id=1, role="cio", kind="claim", summary="慣性ルール維持を主張",
               stated_at=datetime(2026, 8, 1, tzinfo=UTC)),
    ]
    prompt = build_onboarding_prompt(_assets(), stances)
    i_sys = prompt.index("SYSTEM本文")
    i_cha = prompt.index("CHARTER本文")
    i_sta = prompt.index("前回までの自分の主張・懸念")
    assert i_sys < i_cha < i_sta
    assert "- [2026-08-02 / 懸念] コスト計上漏れの懸念" in prompt
    assert "- [2026-08-01 / 主張] 慣性ルール維持を主張" in prompt
    # 同一入力 → 同一出力(決定論)
    assert prompt == build_onboarding_prompt(_assets(), stances)


def test_build_onboarding_prompt_first_assumption():
    """stances が空なら初回着任と明示する。"""
    prompt = build_onboarding_prompt(_assets(), [])
    assert "(記録なし — 初回着任)" in prompt
