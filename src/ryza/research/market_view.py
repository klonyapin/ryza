"""market_view — 市場観ステートの更新規約の決定論実装(設計 20-research §5)。

**最重要の境界**: editor(LLM)が出すのは更新案(``MarketViewDiff``)にすぎない。
市場観ステート(``docs.market_view``)を実際に変更できるのは本モジュールの決定論ルールだけ。
LLM が確信度や magnitude を直接ステートに書くことは禁止(CLAUDE.md 不変原則1)。

更新規約:

1. **更新はすべて diff**。全 diff に根拠 refs 必須。
2. **慣性ルール**: regime の反転は複数ソース・複数日の証拠蓄積を要する。反転条件は
   ``config/market_view.yaml`` の宣言的閾値(``inertia``)で定義。単一日・単一ソースの
   反転提案は拒否され、``docs.regime_flip_evidence`` に証拠として追記だけされる。
3. **変化量スコア magnitude**(0-1)を適用差分から算出。閾値超で速報トリガ(フック発火)。
4. **日次スナップショット**(point-in-time・追記オンリー)。

DB 書き込みは渡された ``conn`` のトランザクションに参加し、本モジュールは commit しない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

from ryza.provenance import Run, record

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "market_view.yaml"


# ── config ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MarketViewConfig:
    """market_view.yaml の内容(慣性閾値・magnitude 重み・速報閾値)。"""

    version: str
    inertia: dict[str, float]
    magnitude: dict[str, float]
    flash_threshold: float

    @classmethod
    def load(cls, path: str | Path = _CONFIG_PATH) -> MarketViewConfig:
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            version=str(data.get("version", "1")),
            inertia=dict(data.get("inertia", {"min_days": 2, "min_sources": 2, "min_weight": 1.0})),
            magnitude=dict(data.get("magnitude", {})),
            flash_threshold=float(data.get("flash_threshold", 0.5)),
        )


# ── ステートと diff の型 ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class MarketViewState:
    """市場観の 1 版(``docs.market_view`` の 1 行)。"""

    view_id: int
    ts: datetime
    regime: dict[str, str]
    key_risks: list[dict[str, Any]]

    def regime_of(self, dimension: str) -> str | None:
        return self.regime.get(dimension)


@dataclass(frozen=True)
class RegimeChange:
    """1 次元の regime 変更提案。``refs`` は根拠 doc_id(必須・§5-1)。"""

    dimension: str
    to_regime: str
    refs: list[int]
    source_count: int = 1
    weight: float = 1.0  # この提案の証拠強度(0-1)。慣性の蓄積量に使う。


@dataclass(frozen=True)
class KeyRiskOp:
    """注目リスクへの操作。op = add | update_confidence | resolve。"""

    op: str
    risk_id: str
    refs: list[int]
    confidence: float | None = None
    statement: str | None = None
    observable: str | None = None


@dataclass(frozen=True)
class MarketViewDiff:
    """editor の更新案(提案)。全操作に根拠 refs を持つ。"""

    regime_changes: list[RegimeChange] = field(default_factory=list)
    key_risk_ops: list[KeyRiskOp] = field(default_factory=list)

    @classmethod
    def from_editor_scores(cls, scores: dict[str, Any]) -> MarketViewDiff:
        """editor の scores(EDITOR_SCHEMA 準拠)を diff に変換する。"""
        changes: list[RegimeChange] = []
        for dim, spec in (scores.get("regime_changes") or {}).items():
            refs = [int(r) for r in (spec.get("refs") or [])]
            changes.append(
                RegimeChange(
                    dimension=dim,
                    to_regime=str(spec["to"]),
                    refs=refs,
                    source_count=int(spec.get("source_count", 1)),
                    weight=float(spec.get("weight", 1.0)),
                )
            )
        ops: list[KeyRiskOp] = []
        for raw in scores.get("key_risk_ops") or []:
            ops.append(
                KeyRiskOp(
                    op=str(raw["op"]),
                    risk_id=str(raw["risk_id"]),
                    refs=[int(r) for r in (raw.get("refs") or [])],
                    confidence=(
                        float(raw["confidence"]) if raw.get("confidence") is not None else None
                    ),
                    statement=raw.get("statement"),
                    observable=raw.get("observable"),
                )
            )
        return cls(regime_changes=changes, key_risk_ops=ops)

    def all_refs(self) -> list[int]:
        refs: set[int] = set()
        for c in self.regime_changes:
            refs.update(c.refs)
        for o in self.key_risk_ops:
            refs.update(o.refs)
        return sorted(refs)


@dataclass
class AppliedChange:
    """適用/拒否された 1 変更の記録(changes jsonb・監査用)。"""

    kind: str  # regime_flip | regime_add | key_risk_add | key_risk_confidence | key_risk_resolve
    detail: dict[str, Any]
    magnitude: float
    accepted: bool
    reason: str = ""


@dataclass
class ApplyResult:
    """``apply_update`` の結果。"""

    view_id: int | None  # 新版が作られたら view_id、変化なしなら None
    magnitude: float
    changes: list[AppliedChange]
    flash_triggered: bool

    @property
    def applied(self) -> list[AppliedChange]:
        return [c for c in self.changes if c.accepted]

    @property
    def rejected(self) -> list[AppliedChange]:
        return [c for c in self.changes if not c.accepted]


# 速報フックの型: (view_id, magnitude, reason) を受ける任意の副作用。
FlashHook = Callable[[int, float, dict[str, Any]], None]


# ── ステートの読み書き ─────────────────────────────────────────────────────────
def load_current(conn: psycopg.Connection) -> MarketViewState | None:
    """最新の市場観版を返す(未初期化なら None)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT view_id, ts, regime, key_risks "
            "FROM docs.market_view ORDER BY view_id DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return MarketViewState(
        view_id=row[0], ts=row[1], regime=dict(row[2]), key_risks=list(row[3])
    )


def initialize(
    conn: psycopg.Connection,
    run: Run,
    *,
    regime: dict[str, str],
    key_risks: list[dict[str, Any]] | None = None,
    basis_refs: list[int] | None = None,
    as_of: datetime | None = None,
) -> MarketViewState:
    """初期の市場観版を作る(ブートストラップ)。既存があっても新版を追記する。"""
    ts = as_of or datetime.now(UTC)
    view_id = _insert_view(
        conn, run, regime=regime, key_risks=key_risks or [],
        changes={"kind": "initialize"}, basis_refs=basis_refs or [], ts=ts,
    )
    return MarketViewState(
        view_id=view_id, ts=ts, regime=dict(regime), key_risks=list(key_risks or [])
    )


def _insert_view(
    conn: psycopg.Connection,
    run: Run,
    *,
    regime: dict[str, str],
    key_risks: list[dict[str, Any]],
    changes: dict[str, Any],
    basis_refs: list[int],
    ts: datetime,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.market_view (ts, regime, key_risks, changes, basis_refs, run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING view_id
            """,
            (ts, Jsonb(regime), Jsonb(key_risks), Jsonb(changes), basis_refs, run.run_id),
        )
        view_id = cur.fetchone()[0]
    # リネージ: 版は根拠文書に依存する。
    if basis_refs:
        record(conn, run, [("market_view", view_id)], [("documents", d) for d in basis_refs])
    return view_id


# ── 慣性ルール(regime 反転)───────────────────────────────────────────────────
def _accumulated_flip_evidence(
    conn: psycopg.Connection, dimension: str, from_regime: str, to_regime: str
) -> tuple[int, int, float]:
    """(相異なる日数, ソース合計, weight 合計) を返す。

    現在 regime が ``from_regime`` に一致する間の蓄積のみが「生きている」。反転が適用されると
    現在 regime が変わり、古い (from,to) 行はこのクエリの対象から外れる(自動リセット)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT evidence_day), COALESCE(SUM(source_count), 0),
                   COALESCE(SUM(weight), 0)
            FROM docs.regime_flip_evidence
            WHERE dimension = %s AND from_regime = %s AND to_regime = %s
              AND applied = false
            """,
            (dimension, from_regime, to_regime),
        )
        days, sources, weight = cur.fetchone()
    return int(days), int(sources), float(weight)


def _append_flip_evidence(
    conn: psycopg.Connection,
    run: Run,
    change: RegimeChange,
    from_regime: str,
    *,
    evidence_day: date,
    as_of: datetime,
    applied: bool,
    report_id: int | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.regime_flip_evidence
                (dimension, from_regime, to_regime, weight, evidence_day,
                 source_count, report_id, applied, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                change.dimension, from_regime, change.to_regime, change.weight,
                evidence_day, change.source_count, report_id, applied, as_of, run.run_id,
            ),
        )


def _inertia_ok(
    days: int, sources: int, weight: float, cfg: MarketViewConfig
) -> bool:
    return (
        days >= cfg.inertia.get("min_days", 2)
        and sources >= cfg.inertia.get("min_sources", 2)
        and weight >= cfg.inertia.get("min_weight", 1.0)
    )


# ── 更新の適用 ─────────────────────────────────────────────────────────────────
def apply_update(
    conn: psycopg.Connection,
    run: Run,
    diff: MarketViewDiff,
    *,
    config: MarketViewConfig | None = None,
    as_of: datetime | None = None,
    report_id: int | None = None,
    flash_hook: FlashHook | None = None,
) -> ApplyResult:
    """editor の更新案を決定論ルールで適用し、必要なら新版を作る。

    - regime 反転は慣性ルール(§5-2)を満たすときだけ適用。満たさなくても証拠は台帳に追記。
    - 存在しない次元への regime 設定は「追加」(反転ではない)として即適用。
    - key_risk 操作は即適用。
    - magnitude を算出し、``config.flash_threshold`` 超で ``flash_hook`` を発火する。
    """
    config = config or MarketViewConfig.load()
    as_of = as_of or datetime.now(UTC)
    ev_day = as_of.date()

    current = load_current(conn)
    if current is None:
        raise RuntimeError("市場観が未初期化。先に initialize() を呼ぶこと。")

    new_regime = dict(current.regime)
    new_risks = [dict(r) for r in current.key_risks]
    changes: list[AppliedChange] = []
    mag = config.magnitude

    # --- regime 変更 ---
    for ch in diff.regime_changes:
        if not ch.refs:
            raise ValueError(f"regime 変更 '{ch.dimension}' に根拠 refs が無い(§5-1)")
        cur_regime = current.regime_of(ch.dimension)
        if cur_regime is None:
            # 新規次元の追加(反転ではない)。即適用・小 magnitude。
            new_regime[ch.dimension] = ch.to_regime
            changes.append(AppliedChange(
                "regime_add",
                {"dimension": ch.dimension, "to": ch.to_regime, "refs": ch.refs},
                mag.get("regime_add", 0.2), accepted=True,
            ))
            continue
        if cur_regime == ch.to_regime:
            # 変化なし(冪等)。無視。
            continue
        # 反転提案 → 証拠を追記してから慣性判定。
        _append_flip_evidence(
            conn, run, ch, cur_regime, evidence_day=ev_day, as_of=as_of,
            applied=False, report_id=report_id,
        )
        days, sources, weight = _accumulated_flip_evidence(
            conn, ch.dimension, cur_regime, ch.to_regime
        )
        detail = {
            "dimension": ch.dimension, "from": cur_regime, "to": ch.to_regime,
            "refs": ch.refs, "accumulated_days": days,
            "accumulated_sources": sources, "accumulated_weight": weight,
        }
        if _inertia_ok(days, sources, weight, config):
            new_regime[ch.dimension] = ch.to_regime
            # 蓄積した (from,to) 証拠を「適用済み」に落として二重適用を防ぐ。
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE docs.regime_flip_evidence SET applied = true "
                    "WHERE dimension = %s AND from_regime = %s AND to_regime = %s "
                    "AND applied = false",
                    (ch.dimension, cur_regime, ch.to_regime),
                )
            changes.append(AppliedChange(
                "regime_flip", detail, mag.get("regime_flip", 0.7), accepted=True,
            ))
        else:
            changes.append(AppliedChange(
                "regime_flip", detail, 0.0, accepted=False,
                reason="慣性ルール未達(複数日・複数ソース・weight のいずれか不足)",
            ))

    # --- key_risk 操作 ---
    for op in diff.key_risk_ops:
        if not op.refs:
            raise ValueError(f"key_risk 操作 '{op.risk_id}' に根拠 refs が無い(§5-1)")
        changes.append(_apply_key_risk(op, new_risks, mag))

    # --- magnitude 算出 & 新版作成 ---
    magnitude = _clamp(sum(c.magnitude for c in changes if c.accepted))
    accepted = [c for c in changes if c.accepted]
    if not accepted:
        return ApplyResult(
            view_id=None, magnitude=magnitude, changes=changes, flash_triggered=False
        )

    basis_refs = diff.all_refs()
    changes_json = {
        "magnitude": magnitude,
        "applied": [
            {"kind": c.kind, "detail": c.detail, "magnitude": c.magnitude} for c in accepted
        ],
        "rejected": [
            {"kind": c.kind, "detail": c.detail, "reason": c.reason}
            for c in changes if not c.accepted
        ],
    }
    view_id = _insert_view(
        conn, run, regime=new_regime, key_risks=new_risks,
        changes=changes_json, basis_refs=basis_refs, ts=as_of,
    )
    # editor レポートがこの版を生んだ、というリネージ。
    if report_id is not None:
        record(conn, run, [("market_view", view_id)], [("research_reports", report_id)])

    flash = magnitude >= config.flash_threshold
    if flash:
        _record_flash(conn, run, view_id, magnitude, changes_json, as_of)
        if flash_hook is not None:
            flash_hook(view_id, magnitude, changes_json)

    return ApplyResult(view_id=view_id, magnitude=magnitude, changes=changes, flash_triggered=flash)


def _apply_key_risk(
    op: KeyRiskOp, risks: list[dict[str, Any]], mag: dict[str, float]
) -> AppliedChange:
    idx = next((i for i, r in enumerate(risks) if r.get("risk_id") == op.risk_id), None)
    if op.op == "add":
        risks.append({
            "risk_id": op.risk_id,
            "statement": op.statement or "",
            "confidence": op.confidence if op.confidence is not None else 0.5,
            "observable": op.observable or "",
            "refs": op.refs,
        })
        return AppliedChange(
            "key_risk_add", {"risk_id": op.risk_id, "refs": op.refs},
            mag.get("key_risk_add", 0.15), accepted=True,
        )
    if op.op == "update_confidence":
        if idx is None:
            return AppliedChange(
                "key_risk_confidence", {"risk_id": op.risk_id}, 0.0, accepted=False,
                reason="対象リスクが存在しない",
            )
        old = float(risks[idx].get("confidence", 0.5))
        new = op.confidence if op.confidence is not None else old
        risks[idx]["confidence"] = new
        risks[idx]["refs"] = op.refs
        delta = abs(new - old)
        return AppliedChange(
            "key_risk_confidence",
            {"risk_id": op.risk_id, "from": old, "to": new, "refs": op.refs},
            _clamp(delta * mag.get("key_risk_confidence_scale", 0.5)), accepted=True,
        )
    if op.op == "resolve":
        if idx is None:
            return AppliedChange(
                "key_risk_resolve", {"risk_id": op.risk_id}, 0.0, accepted=False,
                reason="対象リスクが存在しない",
            )
        risks.pop(idx)
        return AppliedChange(
            "key_risk_resolve", {"risk_id": op.risk_id, "refs": op.refs},
            mag.get("key_risk_resolve", 0.2), accepted=True,
        )
    return AppliedChange("key_risk_unknown", {"op": op.op}, 0.0, accepted=False, reason="未知の op")


def _record_flash(
    conn: psycopg.Connection,
    run: Run,
    view_id: int,
    magnitude: float,
    reason: dict[str, Any],
    as_of: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.flash_triggers (view_id, magnitude, reason, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (view_id, magnitude, Jsonb(reason), as_of, run.run_id),
        )


# ── 日次スナップショット ────────────────────────────────────────────────────────
def snapshot_daily(
    conn: psycopg.Connection,
    run: Run,
    *,
    snapshot_date: date | None = None,
    as_of: datetime | None = None,
) -> int | None:
    """現行版を当日のスナップショットとして追記する(point-in-time・§5-4)。

    未初期化なら None。同日に複数回呼んでも追記し、``docs.market_view_daily`` が最新を返す。
    """
    as_of = as_of or datetime.now(UTC)
    snap_date = snapshot_date or as_of.date()
    current = load_current(conn)
    if current is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs.market_view_snapshots (snapshot_date, view_id, ts, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING snapshot_id
            """,
            (snap_date, current.view_id, current.ts, as_of, run.run_id),
        )
        return cur.fetchone()[0]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
