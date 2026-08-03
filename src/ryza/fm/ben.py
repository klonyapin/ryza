"""ben — バリュー FM(LLM・週次)。T-017。

Ben は **候補の採否**だけを出す。数量・金額は決定論コード(``sizing``)が決め、LLM の
出力は一切サイズに触れない(不変原則1)。LLM の出力は全て ``trading.fm_theses`` に
記録され、証憑は as_of 以前に限られる(point-in-time — 不変原則4)。

1回の実行で行うこと:

1. **着任**: ``personas/fm-ben/``(system + charter)+ 直近の自分の提案(ゲート判定つき)
   を決定論で連結した着任プロンプトを組む(governance.personas と同じ思想。FM の永続記憶は
   ``governance.stances`` ではなく ``fm_theses``)
2. **入力**: マンデートのユニバース(分類済み銘柄+as_of 以前の最新終値)・現在の保有と
   その建玉根拠・as_of 以前の文書
3. **出力**: 新規候補(thesis / invalidation / evidence_refs)+ **保有の見直し**
   (建玉時の invalidation が成立したか)
4. **検証**: スキーマ(``BEN_SCHEMA``)→ ユニバース所属 → 証憑の point-in-time。
   落ちた候補は ``rejected`` に理由つきで残し、実行全体は止めない(1候補の不備で
   他の候補と保有見直しまで捨てない)
5. **投入**: ``base.submit_intents``(サイジング → thesis 記録 → ゲート)

モデル階層は ``config/fm_ben.yaml`` の ``model_tier``(既定 mid)。呼び出し側は
``dept_tag='fm.ben'`` の ``StructuredLLM`` を注入する(コスト台帳の部門タグ)。
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg

from ryza.fm import base
from ryza.fm.base import Candidate, Intent
from ryza.fm.config import BenConfig
from ryza.fm.schemas import BEN_SCHEMA
from ryza.fm.sizing import held_positions
from ryza.fm.theses import (
    ThesisError,
    open_theses_by_instrument,
    recent_theses,
    validate_evidence_refs,
)
from ryza.governance.personas import load_persona_assets
from ryza.ips import IPSConfig, Mandate, load_and_validate
from ryza.provenance import Run
from ryza.research.llm import StructuredLLM

FM = "ben"
PERSONA_ROLE = "fm_ben"
TASK_TYPE = "fm.ben.selection"

# 手仕舞い提案の反証条件は決定論の定型文にする(LLM に「降りる理由の反証」を書かせない —
# 解消は建玉時の invalidation 成立に対する機械的な帰結であるべき)。
_CLOSE_INVALIDATION = (
    "本手仕舞いに反証条件は置かない(建玉時の invalidation 成立に対する機械的な解消)。"
    "同じ銘柄を再び買うなら、今日はじめて見た証拠として新規に評価し直す。"
)


# ── 着任プロンプト ────────────────────────────────────────────────────────────
def build_system_prompt(conn: psycopg.Connection, *, limit: int = 10) -> str:
    """人格 + 職務規程 + 直近の自分の提案(ゲート判定つき)を連結する(決定論)。"""
    assets = load_persona_assets(PERSONA_ROLE)
    parts = [
        assets.system.strip(),
        "---",
        "# 職務規程(charter)— 権限・義務・禁止はここに列挙された範囲のみ(定款第7条)",
        assets.charter.strip(),
        "---",
        "# 前回までの自分の提案とゲート判定(trading.fm_theses 直近・新しい順)",
    ]
    rows = recent_theses(conn, FM, limit=limit)
    if not rows:
        parts.append("(記録なし — 初回着任)")
    for r in rows:
        verdict = r.gate_verdict or "(未投入)"
        reasons = ""
        if r.gate_reasons:
            reasons = " / 理由: " + "; ".join(
                str(x.get("message", "")) for x in r.gate_reasons
            )
        parts.append(
            f"- [{r.as_of:%Y-%m-%d} / {r.direction} / instrument {r.instrument_id} / "
            f"ゲート {verdict}{reasons}] {r.thesis_md[:200]}"
            f"(降りる条件: {r.invalidation_md[:120]})"
        )
    return "\n\n".join(parts) + "\n"


def _load_documents(
    conn: psycopg.Connection, *, as_of: datetime, cfg: BenConfig
) -> list[dict[str, Any]]:
    """as_of 以前に知り得た文書(新しい順)。リプレイ時に未来のニュースを混ぜない。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_id, source_name, title, left(coalesce(body, ''), %s), as_of
            FROM docs.documents
            WHERE as_of <= %s
            ORDER BY as_of DESC, doc_id DESC
            LIMIT %s
            """,
            (cfg.doc_body_chars, as_of, cfg.max_documents),
        )
        return [
            {
                "doc_id": r[0], "source": r[1], "title": r[2],
                "body": r[3], "as_of": r[4].isoformat(),
            }
            for r in cur.fetchall()
        ]


def build_user_prompt(
    *,
    as_of: datetime,
    universe: list[Candidate],
    prices: dict[int, Decimal],
    holdings: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    max_candidates: int,
) -> str:
    """ユーザープロンプト(決定論の JSON 文字列)。"""
    payload = {
        "task": (
            "マンデートのユニバースから、安全域のある新規候補を選ぶ。候補が無ければ空で返す。"
            "同時に、保有銘柄について建玉時の invalidation が成立しているかを判定する。"
            "数量・金額・比率は書かない(サイジングは決定論コードが行う)。"
        ),
        "as_of": as_of.isoformat(),
        "rules": [
            "direction は buy のみ(ショート・デリバティブ・信用はマンデートの禁じ手)",
            f"新規候補は最大 {max_candidates} 件。無理に埋めない",
            "全候補に invalidation_md(観測可能な反証条件)と evidence_refs を付ける",
            "evidence_refs は as_of 以前の証憑のみ("
            '{"kind":"document","doc_id":N} 等。未来の情報は使えない)',
        ],
        "universe": [
            {
                "instrument_id": c.instrument_id,
                "symbol": c.symbol,
                "asset_class": c.asset_class,
                "last_close": str(prices.get(c.instrument_id, "")),
            }
            for c in universe
        ],
        "holdings": holdings,
        "documents": documents,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _holdings_payload(
    conn: psycopg.Connection, held: dict[int, Decimal]
) -> list[dict[str, Any]]:
    """保有銘柄と、その建玉根拠(最新の buy thesis)。見直しの入力。"""
    theses = open_theses_by_instrument(conn, FM, sorted(held))
    payload: list[dict[str, Any]] = []
    for instrument_id in sorted(held):
        thesis = theses.get(instrument_id)
        payload.append(
            {
                "instrument_id": instrument_id,
                "qty": str(held[instrument_id]),
                "entry_thesis": None if thesis is None else thesis.thesis_md,
                "invalidation": None if thesis is None else thesis.invalidation_md,
            }
        )
    return payload


# ── 出力の検証 → Intent ───────────────────────────────────────────────────────
def _to_intents(
    conn: psycopg.Connection,
    content: dict[str, Any],
    *,
    as_of: datetime,
    candidates: dict[int, Candidate],
    held: dict[int, Decimal],
    model: str,
    cfg: BenConfig,
) -> tuple[list[Intent], list[dict[str, Any]]]:
    """LLM 出力 → Intent 群。不備のある項目は ``rejected`` に理由つきで落とす。"""
    intents: list[Intent] = []
    rejected: list[dict[str, Any]] = []

    # 重複銘柄は候補数上限を数える前に潰す(先頭優先・決定論 — 審査 C-1)。
    # 上限が重複で埋まって実質1銘柄になるのを避け、かつ集中度の二重割り当てを防ぐ。
    unique_candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for cand in content.get("candidates") or []:
        instrument_id = int(cand["instrument_id"])
        if instrument_id in seen:
            rejected.append(
                {"instrument_id": instrument_id, "reason": "同一銘柄の重複候補(先頭のみ採用)"}
            )
            continue
        seen.add(instrument_id)
        unique_candidates.append(cand)

    for cand in unique_candidates[: cfg.max_candidates]:
        instrument_id = int(cand["instrument_id"])
        reason = _reject_reason(cand, instrument_id, candidates, held)
        if reason is None:
            reason = _evidence_reason(conn, cand.get("evidence_refs") or [], as_of)
        if reason is not None:
            rejected.append({"instrument_id": instrument_id, "reason": reason})
            continue
        intents.append(
            Intent(
                fm=FM,
                instrument_id=instrument_id,
                direction="buy",
                thesis_md=cand["thesis_md"],
                evidence_refs=list(cand["evidence_refs"]),
                invalidation_md=cand["invalidation_md"],
                model=model,
            )
        )

    for review in content.get("reviews") or []:
        if not review.get("invalidated"):
            continue
        instrument_id = int(review["instrument_id"])
        if instrument_id not in held:
            rejected.append({"instrument_id": instrument_id, "reason": "保有していない"})
            continue
        if not (review.get("rationale_md") or "").strip():
            rejected.append({"instrument_id": instrument_id, "reason": "見直しの理由が空"})
            continue
        reason = _evidence_reason(conn, review.get("evidence_refs") or [], as_of)
        if reason is not None:
            rejected.append({"instrument_id": instrument_id, "reason": reason})
            continue
        intents.append(
            Intent(
                fm=FM,
                instrument_id=instrument_id,
                direction="close",
                thesis_md=review["rationale_md"],
                evidence_refs=list(review["evidence_refs"]),
                invalidation_md=_CLOSE_INVALIDATION,
                model=model,
            )
        )
    return intents, rejected


def _reject_reason(
    cand: dict[str, Any],
    instrument_id: int,
    candidates: dict[int, Candidate],
    held: dict[int, Decimal],
) -> str | None:
    if instrument_id not in candidates:
        return "ユニバース外(マンデート違反 — 提案として記録しない)"
    if instrument_id in held:
        return "既に保有(スロット占有)"
    if not (cand.get("thesis_md") or "").strip():
        return "thesis が空"
    if not (cand.get("invalidation_md") or "").strip():
        return "反証条件(invalidation)が無い"
    if not (cand.get("evidence_refs") or []):
        return "証憑(evidence_refs)が無い"
    return None


def _evidence_reason(
    conn: psycopg.Connection, refs: list[dict[str, Any]], as_of: datetime
) -> str | None:
    """証憑を検証し、不備があれば理由文字列を返す(なければ None)。"""
    try:
        validate_evidence_refs(conn, refs, as_of=as_of)
    except ThesisError as exc:
        return str(exc)
    return None


# ── 週次実行 ──────────────────────────────────────────────────────────────────
def run_ben(
    conn: psycopg.Connection,
    run: Run,
    llm: StructuredLLM,
    *,
    model: str,
    book_id: str,
    as_of: datetime,
    cfg: BenConfig | None = None,
    ips: IPSConfig | None = None,
    mandates: dict[str, Mandate] | None = None,
) -> dict[str, Any]:
    """Ben の週次サイクル。ユニバースが空なら LLM を呼ばない(無駄な呼び出しを書かない)。"""
    cfg = cfg or BenConfig.load()
    if ips is None or mandates is None:
        loaded_ips, loaded_mandates = load_and_validate()
        ips = ips or loaded_ips
        mandates = mandates or loaded_mandates
    mandate = mandates[FM]

    universe = base.load_universe(conn, mandate, as_of=as_of)
    if not universe:
        return {"universe": 0, "skipped": "ユニバースが空(分類待ち)"}
    candidates = {c.instrument_id: c for c in universe}
    positions = base.load_positions(conn, book_id)
    held = held_positions(positions, FM)
    prices = base.load_prices(
        conn, sorted(set(candidates) | set(held)), as_of=as_of
    )

    result = llm.complete(
        system=build_system_prompt(conn, limit=cfg.recent_theses),
        user=build_user_prompt(
            as_of=as_of,
            universe=universe,
            prices=prices,
            holdings=_holdings_payload(conn, held),
            documents=_load_documents(conn, as_of=as_of, cfg=cfg),
            max_candidates=cfg.max_candidates,
        ),
        schema=BEN_SCHEMA,
        task_type=TASK_TYPE,
        model_tier=cfg.model_tier,
        model=model,
    )
    intents, rejected = _to_intents(
        conn, result.content, as_of=as_of, candidates=candidates,
        held=held, model=model, cfg=cfg,
    )
    submitted = base.submit_intents(
        conn, run, intents,
        mandate=mandate, max_slots=cfg.max_slots, candidates=candidates,
        producer=cfg.producer, book_id=book_id, as_of=as_of,
        ips=ips, mandates=mandates,
    )
    return {
        "universe": len(universe),
        "candidates": sum(1 for i in intents if i.direction == "buy"),
        "closes": sum(1 for i in intents if i.direction == "close"),
        "rejected": rejected,
        "cost_estimate": result.cost_estimate,
        **submitted.as_dict(),
    }


__all__ = [
    "FM",
    "PERSONA_ROLE",
    "TASK_TYPE",
    "build_system_prompt",
    "build_user_prompt",
    "run_ben",
]
