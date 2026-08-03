"""役職資産の着任ローダ(05-governance §2)。

役職の charter.md(職務規程)+ system.md(人格プロンプト)と、``governance.stances``
の直近 N 件(過去の主張・懸念)から**着任プロンプト**を決定論的に組み立てる。
セッションは使い捨てでよく、起動時にこのプロンプトを読み込んだモデルが
「その役職に着任」する(モデル世代交代しても役職資産が継続性を担保する)。

**本モジュールは LLM を呼ばない**(純粋な読み込み・組み立てのみ)。呼び出しは
役員室チャット(ダッシュボード実装時)・月次委員会ジョブの管轄。

独立性について(05 §6-2): stances は role 単位で分離して読み、本モジュールは
単一 role の資産のみを扱う API とする。ただしこれは**慣習+テストによる分離**で
あり、DB レベルの強制(RLS・役職別資格情報の分離)は未実装。執行側コードが
他 role の stances を直接 SELECT することを物理的には防げない点に注意
(強制化は役員室・監査ジョブ実装時の課題)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

# personas/ はリポジトリルート直下。src/ryza/governance/personas.py から 3 つ上がルート。
_PERSONA_ROOT = Path(__file__).resolve().parents[3] / "personas"

# 役員層の役職(05 §3)。ディレクトリ名はハイフン、DB(governance.yaml roles)は
# アンダースコアを用いるため、両表記を受け付けて正規化する。
OFFICER_ROLES = ("cio", "independent_officer", "audit")

_KIND_LABELS = {"claim": "主張", "concern": "懸念", "dissent": "反対意見", "retraction": "撤回"}

# stance の出所種別(0022 の governance.stances.source。CHECK と同じ語彙)。
SOURCES = ("direct", "office_chat", "committee")

# 盲検着任(``assume_role(blind=True)``)で読み込まない出所。**会議由来はすべて外す**:
# 会議では代表が指示・選好を述べ、役員はそれに応答する形で主張を形成するため、
# 会議由来の stance は「自分の過去の主張」の形をとった代表の選好になりうる
# (独立役員審査 boardroom-meeting C-3)。'committee' はまだ書き手が居ないが、
# 書き手が現れた時点で自動的に除外されるよう先に列挙する。
BLIND_EXCLUDED_SOURCES = ("office_chat", "committee")


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
    source: str = "direct"  # direct | office_chat | committee(0022)


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
    retracts: int | None = None,
    source: str = "direct",
) -> int:
    """主張・懸念の要約を ``governance.stances`` に追記する(05 §4)。

    テーブルは追記オンリー(UPDATE/DELETE 禁止トリガ)。訂正は
    ``kind='retraction'`` + ``retracts=<対象 stance_id>`` の行を追記する。
    撤回は自 role の行に対してのみ許す(他 role の記憶を消させない)。

    ``source`` は出所種別(0022)。既定 ``'direct'`` は「他役職・代表の発言を
    聞いていない文脈での記録」を意味する。会議由来の書込は必ず出所を明示する
    こと(``boardroom.record_chat_stances`` は ``'office_chat'``)— 明示を忘れると
    会議で聞いた代表の選好が盲検レビューへ透過する(議論規約3)。
    """
    if retracts is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM governance.stances WHERE stance_id = %s", (retracts,)
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"撤回対象 stance_id={retracts} が存在しない")
        if row[0] != _db_role(role):
            raise ValueError(
                f"role '{_db_role(role)}' は role '{row[0]}' の stance を撤回できない"
            )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.stances
                (role, kind, summary, minute_id, retracts, run_id, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING stance_id
            """,
            (_db_role(role), kind, summary, minute_id, retracts, run_id, source),
        )
        return cur.fetchone()[0]


def recent_stances(
    conn: psycopg.Connection,
    role: str,
    *,
    limit: int = 10,
    exclude_sources: Sequence[str] = (),
) -> list[Stance]:
    """当該 role の直近 N 件の主張・懸念(新しい順)。

    単一 role のみを読む(他 role の記憶は返さない — docstring 冒頭の注意どおり
    これは API 慣習であり DB レベルの強制ではない)。撤回された行と撤回行自体は
    着任プロンプトに載せないため除外する。

    ``exclude_sources`` を渡すと当該出所(0022 の ``source``)の行を落とす。
    盲検着任は ``BLIND_EXCLUDED_SOURCES`` を渡す。**撤回の判定は除外の影響を
    受けない** — 撤回行が会議で述べられたものでも、撤回された事実は消えない。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.stance_id, s.role, s.kind, s.summary, s.stated_at, s.source
            FROM governance.stances s
            WHERE s.role = %s
              AND s.kind <> 'retraction'
              AND NOT (s.source = ANY(%s::text[]))
              AND NOT EXISTS (SELECT 1 FROM governance.stances r
                              WHERE r.retracts = s.stance_id)
            ORDER BY s.stated_at DESC, s.stance_id DESC
            LIMIT %s
            """,
            (_db_role(role), list(exclude_sources), limit),
        )
        return [
            Stance(
                stance_id=r[0], role=r[1], kind=r[2], summary=r[3],
                stated_at=r[4], source=r[5],
            )
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
    blind: bool = False,
) -> str:
    """役職資産+直近 stances を読み、着任プロンプトを返す(上記関数の合成)。

    ``blind=True``(盲検モード)は会議由来の stance(``BLIND_EXCLUDED_SOURCES``)を
    着任プロンプトから外す。戦略昇格・IPS 改訂案の評価で独立役員が着任する経路は
    これを使う(議論規約3・独立役員審査 boardroom-meeting C-3): 会議で聞いた
    代表の選好が「自分の過去の主張」の形で盲検レビューに透過するのを防ぐ。
    既定は ``False`` = 従来挙動(全出所を読む)。
    """
    assets = load_persona_assets(role, persona_root=persona_root)
    stances = recent_stances(
        conn,
        role,
        limit=limit,
        exclude_sources=BLIND_EXCLUDED_SOURCES if blind else (),
    )
    return build_onboarding_prompt(assets, stances)


__all__ = [
    "BLIND_EXCLUDED_SOURCES",
    "OFFICER_ROLES",
    "SOURCES",
    "PersonaAssets",
    "Stance",
    "assume_role",
    "build_onboarding_prompt",
    "load_persona_assets",
    "recent_stances",
    "record_stance",
]
