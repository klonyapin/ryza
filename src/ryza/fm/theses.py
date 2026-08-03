"""theses — FM の提案記録 ``trading.fm_theses`` と証憑の point-in-time 検証(T-017)。

役割は3つ:

1. **記録の唯一の入口** ``record_thesis``: 反証条件(invalidation)と証憑(evidence_refs)を
   欠いた提案を保存させない。スキーマ側の CHECK と二重の防御にするのは、アプリ層でしか
   検証できない **point-in-time**(証憑が as_of 以前か)をここで併せて弾くため
2. **point-in-time 検証** ``validate_evidence_refs``: 証憑1件ごとに参照先の ``as_of`` を
   引き、判断時点より新しい証憑を拒否する。未来情報の混入はバグではなく設計違反
   (不変原則4)。「存在しない証憑」と「as_of 超の証憑」は別メッセージで区別する
3. **判断履歴の注入** ``recent_theses``: FM 別・新しい順に、**ゲート判定の結果つき**で
   読み出す(orders.thesis_id → orders.status / compliance.gate_log)。block された案が
   次回プロンプトの学習材料になる(指示書6・7。governance.stances と同じ思想)

証憑参照(evidence_refs)の語彙 — いずれも ``kind`` で分岐する JSON オブジェクト:

- ``{"kind": "document", "doc_id": 12}``                       … docs.documents
- ``{"kind": "research_report", "report_id": 3}``              … docs.research_reports
- ``{"kind": "bar", "instrument_id": 1, "timeframe": "1d", "ts": "..."}`` … market.bars
- ``{"kind": "indicator", "series_code": "JP_CPI", "ts": "..."}``         … market.indicators

**direction は既定で long-only**(buy / close)。short を通すには ``allow_short=True`` を
明示する必要があり、第一陣の生成経路はこれを渡さない(テストで固定 — モジュール
``ryza.fm`` の docstring)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

# スキーマ(0018)の direction 語彙と、第一陣が生成してよい部分集合。
DIRECTIONS = ("buy", "close", "short")
LONG_ONLY_DIRECTIONS = ("buy", "close")

# 証憑参照の kind 語彙。
EVIDENCE_KINDS = ("document", "research_report", "bar", "indicator")


class ThesisError(ValueError):
    """提案の記録を拒否する(反証条件・証憑・direction の不備)。"""


class EvidenceError(ThesisError):
    """証憑参照が不正(欠落・存在しない・as_of 超)。違反を全件列挙する。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n".join(f"- {p}" for p in problems)
        super().__init__(f"証憑参照が不正({len(problems)}件):\n{joined}")


@dataclass(frozen=True)
class ThesisRecord:
    """``trading.fm_theses`` の1行 + そこから出た注文のゲート判定(あれば)。"""

    thesis_id: int
    fm: str
    instrument_id: int
    direction: str
    thesis_md: str
    invalidation_md: str
    as_of: datetime
    order_status: str | None = None  # passed|blocked|filled|... (注文が無ければ None)
    gate_verdict: str | None = None  # pass|warn|block
    gate_reasons: list[dict[str, Any]] | None = None


# ── point-in-time 証憑検証 ────────────────────────────────────────────────────
def _ref_lookup(ref: dict[str, Any]) -> tuple[str, tuple[Any, ...], str]:
    """証憑参照 → (SQL, パラメータ, 表示名)。語彙違反は ``ThesisError``。

    SQL はいずれも「参照先の最小 as_of」を1列で返す(行が無ければ NULL)。最小を採るのは
    改定(indicators の revision)・再取得(bars の as_of 複数)で最も早く知り得た時点を
    point-in-time の基準にするため。
    """
    kind = ref.get("kind")
    if kind == "document":
        doc_id = _require_int(ref, "doc_id")
        return (
            "SELECT min(as_of) FROM docs.documents WHERE doc_id = %s",
            (doc_id,),
            f"document(doc_id={doc_id})",
        )
    if kind == "research_report":
        report_id = _require_int(ref, "report_id")
        return (
            "SELECT min(as_of) FROM docs.research_reports WHERE report_id = %s",
            (report_id,),
            f"research_report(report_id={report_id})",
        )
    if kind == "bar":
        instrument_id = _require_int(ref, "instrument_id")
        timeframe = str(ref.get("timeframe") or "1d")
        ts = ref.get("ts")
        if ts is None:
            raise ThesisError(f"証憑参照 bar に ts が無い: {ref!r}")
        return (
            "SELECT min(as_of) FROM market.bars "
            "WHERE instrument_id = %s AND timeframe = %s AND ts = %s",
            (instrument_id, timeframe, ts),
            f"bar(instrument_id={instrument_id}, ts={ts})",
        )
    if kind == "indicator":
        series_code = ref.get("series_code")
        ts = ref.get("ts")
        if not series_code or ts is None:
            raise ThesisError(f"証憑参照 indicator に series_code/ts が無い: {ref!r}")
        return (
            "SELECT min(as_of) FROM market.indicators WHERE series_code = %s AND ts = %s",
            (str(series_code), ts),
            f"indicator({series_code} @ {ts})",
        )
    raise ThesisError(f"未知の証憑 kind {kind!r}(語彙: {list(EVIDENCE_KINDS)})")


def _require_int(ref: dict[str, Any], key: str) -> int:
    value = ref.get(key)
    if value is None:
        raise ThesisError(f"証憑参照 {ref.get('kind')!r} に {key} が無い: {ref!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ThesisError(f"証憑参照の {key} が整数でない: {value!r}") from exc


def validate_evidence_refs(
    conn: psycopg.Connection, refs: list[dict[str, Any]], *, as_of: datetime
) -> list[dict[str, Any]]:
    """証憑参照を検証して正規化リストを返す(全件 as_of 以前に存在すること)。

    空リストは拒否(証憑なしの提案は作らない)。違反は ``EvidenceError`` に全件列挙する
    — 1件目で止めないのは、LLM(Ben)の出力を1回で全て直せる形で返すため。
    """
    if not refs:
        raise EvidenceError(["証憑参照が空(evidence_refs は必須 — 不変原則3)"])
    problems: list[str] = []
    normalized: list[dict[str, Any]] = []
    for raw in refs:
        if not isinstance(raw, dict):
            problems.append(f"証憑参照がオブジェクトでない: {raw!r}")
            continue
        try:
            sql, params, label = _ref_lookup(raw)
        except ThesisError as exc:
            problems.append(str(exc))
            continue
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        found = row[0] if row else None
        if found is None:
            problems.append(f"{label}: 証憑が存在しない(参照先の行なし)")
            continue
        if found > as_of:
            problems.append(
                f"{label}: 証憑の as_of {found.isoformat()} が判断時点 "
                f"{as_of.isoformat()} より新しい(未来情報の混入 — 不変原則4)"
            )
            continue
        normalized.append(dict(raw))
    if problems:
        raise EvidenceError(problems)
    return normalized


# ── 記録 ──────────────────────────────────────────────────────────────────────
def record_thesis(
    conn: psycopg.Connection,
    *,
    fm: str,
    book_id: str,
    instrument_id: int,
    direction: str,
    thesis_md: str,
    evidence_refs: list[dict[str, Any]],
    invalidation_md: str,
    producer: str,
    as_of: datetime,
    run_id: int,
    rule_id: str | None = None,
    model: str | None = None,
    allow_short: bool = False,
) -> int:
    """提案を ``trading.fm_theses`` に追記して thesis_id を返す(記録の唯一の入口)。

    拒否条件(``ThesisError`` / ``EvidenceError``):

    - direction が語彙外、または long-only 期に short(``allow_short=False``)
    - thesis_md / invalidation_md が空(反証条件の欠落 = 40 §制約1 違反)
    - evidence_refs が空・参照先が存在しない・as_of 超(point-in-time 違反)
    - rule_id と model が両方 None、または両方指定(出所の曖昧化)
    """
    if direction not in DIRECTIONS:
        raise ThesisError(f"direction は {list(DIRECTIONS)} のいずれか: {direction!r}")
    if not allow_short and direction not in LONG_ONLY_DIRECTIONS:
        raise ThesisError(
            f"第一陣(Ben/Jim)は long-only のため direction={direction!r} は生成できない"
            "(ledger の空売り記帳が未対応 — execution/runner.py)"
        )
    if not (thesis_md or "").strip():
        raise ThesisError("thesis_md が空(論拠のない提案は記録しない)")
    if not (invalidation_md or "").strip():
        raise ThesisError(
            "invalidation_md が空(『この論点が崩れたら降りる』は全提案の義務 — "
            "40-fund-managers.md §制約1)"
        )
    if (rule_id is None) == (model is None):
        raise ThesisError(
            "出所は rule_id(決定論シグナル)か model(LLM)のどちらか一方を指定する"
        )
    normalized = validate_evidence_refs(conn, evidence_refs, as_of=as_of)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trading.fm_theses
                (fm, book_id, instrument_id, direction, thesis_md, evidence_refs,
                 invalidation_md, producer, rule_id, model, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING thesis_id
            """,
            (
                fm,
                book_id,
                instrument_id,
                direction,
                thesis_md.strip(),
                Jsonb(normalized),
                invalidation_md.strip(),
                producer,
                rule_id,
                model,
                as_of,
                run_id,
            ),
        )
        return cur.fetchone()[0]


# ── 読出し(次回プロンプトへの注入)────────────────────────────────────────────
def recent_theses(
    conn: psycopg.Connection, fm: str, *, limit: int = 20
) -> list[ThesisRecord]:
    """当該 FM の直近提案を新しい順に、ゲート判定の結果つきで返す。

    ゲート判定は ``trading.fm_theses`` には書き戻さない(追記オンリー)ため、
    orders.thesis_id → orders.status / compliance.gate_log を辿って合成する。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.thesis_id, t.fm, t.instrument_id, t.direction, t.thesis_md,
                   t.invalidation_md, t.as_of, o.status, g.verdict, g.reasons
            FROM trading.fm_theses t
            LEFT JOIN trading.orders o ON o.thesis_id = t.thesis_id
            LEFT JOIN compliance.gate_log g ON g.id = o.gate_log_id
            WHERE t.fm = %s
            ORDER BY t.thesis_id DESC
            LIMIT %s
            """,
            (fm, limit),
        )
        rows = cur.fetchall()
    return [
        ThesisRecord(
            thesis_id=r[0], fm=r[1], instrument_id=r[2], direction=r[3],
            thesis_md=r[4], invalidation_md=r[5], as_of=r[6],
            order_status=r[7], gate_verdict=r[8], gate_reasons=r[9],
        )
        for r in rows
    ]


def open_theses_by_instrument(
    conn: psycopg.Connection, fm: str, instrument_ids: list[int]
) -> dict[int, ThesisRecord]:
    """保有銘柄ごとの「最後に建てた根拠」(direction='buy' の最新 thesis)。

    Ben の保有見直し(invalidation 成立チェック)の入力。約定に至らなかった提案も
    含み得るが、保有中の銘柄に限って引くため実務上は建玉の根拠になる。
    """
    if not instrument_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (t.instrument_id)
                   t.thesis_id, t.fm, t.instrument_id, t.direction, t.thesis_md,
                   t.invalidation_md, t.as_of
            FROM trading.fm_theses t
            WHERE t.fm = %s AND t.instrument_id = ANY(%s) AND t.direction = 'buy'
            ORDER BY t.instrument_id, t.thesis_id DESC
            """,
            (fm, list(instrument_ids)),
        )
        rows = cur.fetchall()
    return {
        r[2]: ThesisRecord(
            thesis_id=r[0], fm=r[1], instrument_id=r[2], direction=r[3],
            thesis_md=r[4], invalidation_md=r[5], as_of=r[6],
        )
        for r in rows
    }


__all__ = [
    "DIRECTIONS",
    "EVIDENCE_KINDS",
    "LONG_ONLY_DIRECTIONS",
    "EvidenceError",
    "ThesisError",
    "ThesisRecord",
    "open_theses_by_instrument",
    "record_thesis",
    "recent_theses",
    "validate_evidence_refs",
]
