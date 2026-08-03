"""役職資産の着任ローダ(05-governance §2)。

役職の charter.md(職務規程)+ system.md(人格プロンプト)と、``governance.stances``
の直近 N 件(過去の主張・懸念)から**着任プロンプト**を決定論的に組み立てる。
セッションは使い捨てでよく、起動時にこのプロンプトを読み込んだモデルが
「その役職に着任」する(モデル世代交代しても役職資産が継続性を担保する)。

**本モジュールは LLM を呼ばない**(純粋な読み込み・組み立てのみ)。呼び出しは
役員室チャット(ダッシュボード実装時)・月次委員会ジョブの管轄。

独立性の担保(05 §6-2): stances は role 単位で分離して読む。独立役員は執行側と
プロンプト資産・記憶を共有しないため、本モジュールは常に単一 role の資産のみを
扱い、他 role の記憶を混ぜる API を持たない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

# personas/ はリポジトリルート直下。src/ryza/governance/personas.py から 3 つ上がルート。
_PERSONA_ROOT = Path(__file__).resolve().parents[3] / "personas"

# 役員層の役職(05 §3)。ディレクトリ名はハイフン、DB(governance.yaml roles)は
# アンダースコアを用いるため、両表記を受け付けて正規化する。
OFFICER_ROLES = ("cio", "independent_officer", "audit")

_KIND_LABELS = {"claim": "主張", "concern": "懸念", "dissent": "反対意見"}


def _dir_name(role: str) -> str:
    """personas/ のディレクトリ名(ハイフン区切り)。"""
    return role.replace("_", "-")


def _db_role(role: str) -> str:
    """governance.stances / governance.yaml roles のキー(アンダースコア区切り)。"""
    return role.replace("-", "_")


@dataclass(frozen=True)
class PersonaAssets:
    """役職資産のうちリポジトリ側(プロンプト資産)の2点セット。"""

    role: str  # 正規化済み(アンダースコア区切り)
    charter: str  # charter.md 全文(職務規程 — 権限・義務・禁止)
    system: str  # system.md 全文(人格プロンプト)


@dataclass(frozen=True)
class Stance:
    """過去の主張・懸念の 1 件(governance.stances の行)。"""

    stance_id: int
    role: str
    kind: str  # claim | concern | dissent
    summary: str
    stated_at: datetime


def load_persona_assets(role: str, persona_root: Path = _PERSONA_ROOT) -> PersonaAssets:
    """``personas/<role>/`` の charter.md + system.md を読む。

    分析エージェント(system.md のみ)と異なり、役職資産は charter が必須
    (定款第7条: 権限は charter に列挙された行為のみ)。どちらか欠けていれば
    ``FileNotFoundError``(暗黙の空 charter で着任させない)。
    """
    directory = persona_root / _dir_name(role)
    charter_path = directory / "charter.md"
    system_path = directory / "system.md"
    missing = [str(p) for p in (charter_path, system_path) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"役職 '{role}' の役職資産が不完全: {', '.join(missing)} が無い"
        )
    return PersonaAssets(
        role=_db_role(role),
        charter=charter_path.read_text(encoding="utf-8"),
        system=system_path.read_text(encoding="utf-8"),
    )


def record_stance(
    conn: psycopg.Connection,
    *,
    role: str,
    kind: str,
    summary: str,
    run_id: int,
    minute_id: int | None = None,
) -> int:
    """主張・懸念の要約を ``governance.stances`` に追記する(05 §4)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.stances (role, kind, summary, minute_id, run_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING stance_id
            """,
            (_db_role(role), kind, summary, minute_id, run_id),
        )
        return cur.fetchone()[0]


def recent_stances(
    conn: psycopg.Connection, role: str, *, limit: int = 10
) -> list[Stance]:
    """当該 role の直近 N 件の主張・懸念(新しい順)。他 role の記憶は返さない。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT stance_id, role, kind, summary, stated_at
            FROM governance.stances
            WHERE role = %s
            ORDER BY stated_at DESC, stance_id DESC
            LIMIT %s
            """,
            (_db_role(role), limit),
        )
        return [
            Stance(stance_id=r[0], role=r[1], kind=r[2], summary=r[3], stated_at=r[4])
            for r in cur.fetchall()
        ]


def build_onboarding_prompt(assets: PersonaAssets, stances: list[Stance]) -> str:
    """着任プロンプトを組み立てる(決定論の文字列連結。LLM は呼ばない)。

    構成: 人格プロンプト(system)→ 職務規程(charter)→ 過去の主張・懸念
    (「前回私はこう懸念した」の引き継ぎ — 05 §4)。
    """
    parts = [
        assets.system.strip(),
        "---",
        "# 職務規程(charter)— 権限・義務・禁止はここに列挙された範囲のみ(定款第7条)",
        assets.charter.strip(),
        "---",
        "# 前回までの自分の主張・懸念(governance.stances 直近・新しい順)",
    ]
    if stances:
        for s in stances:
            label = _KIND_LABELS.get(s.kind, s.kind)
            parts.append(f"- [{s.stated_at:%Y-%m-%d} / {label}] {s.summary}")
    else:
        parts.append("(記録なし — 初回着任)")
    return "\n\n".join(parts) + "\n"


def assume_role(
    conn: psycopg.Connection,
    role: str,
    *,
    limit: int = 10,
    persona_root: Path = _PERSONA_ROOT,
) -> str:
    """役職資産+直近 stances を読み、着任プロンプトを返す(上記関数の合成)。"""
    assets = load_persona_assets(role, persona_root=persona_root)
    stances = recent_stances(conn, role, limit=limit)
    return build_onboarding_prompt(assets, stances)


__all__ = [
    "OFFICER_ROLES",
    "PersonaAssets",
    "Stance",
    "assume_role",
    "build_onboarding_prompt",
    "load_persona_assets",
    "recent_stances",
    "record_stance",
]
