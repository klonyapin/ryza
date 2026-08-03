"""jim — 統計エッジ FM(**非 LLM**・日次)。T-017。

モデル階層の原則(CLAUDE.md 不変原則7「まず非 LLM で済むか検討」)どおり、Jim の第一版は
LLM を**一切呼ばない**。哲学(40-fund-managers.md: 価格データの統計的エッジ・物語を
信じない・小さな優位×多数)は次の決定論ルールとして表現する:

  規則 ``jim.sma_cross.v1``
    - 新規建て(enter): 20日 SMA が 60日 SMA を**上抜けた日**(前日は下、当日は上)
      かつ 当日出来高 ≥ 直近20日平均出来高 × ``min_volume_ratio``
    - 手仕舞い(exit) : 20日 SMA が 60日 SMA を**下抜けた日**(= 反証条件の成立)

パラメータと根拠は ``config/fm_jim.yaml``(ハードコード禁止)。出来高フィルタは執行
コストがエッジを食い潰すのを避けるため(E4 全コスト込み評価)。

thesis は自動生成テキスト+規則 ID、証憑はシグナル判定に使ったバー参照(``kind='bar'``)。
**確信度・スコアは出さない** — 出しても使い道がない(サイズは ``sizing`` が決める)し、
出せば誰かがサイズに使いたくなる(不変原則1)。

``compute_signal`` は純関数(DB に触れない)。数値検証は固定バー系列に対して行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg

from ryza.fm import base
from ryza.fm.base import Intent
from ryza.fm.config import JimConfig
from ryza.fm.sizing import held_positions
from ryza.ips import IPSConfig, Mandate, load_and_validate
from ryza.provenance import Run

FM = "jim"
RULE_ID = "jim.sma_cross.v1"

ENTER = "enter"
EXIT = "exit"


@dataclass(frozen=True)
class Bar:
    """シグナル計算に使う日足(必要な列だけ)。"""

    ts: datetime
    close: Decimal
    volume: Decimal | None


@dataclass(frozen=True)
class Signal:
    """1銘柄のシグナル。**サイズに使える値は持たない**(action と観測値のみ)。"""

    instrument_id: int
    action: str  # enter | exit
    rule_id: str
    bar_ts: datetime
    prev_bar_ts: datetime
    fast: Decimal
    slow: Decimal
    prev_fast: Decimal
    prev_slow: Decimal
    volume_ratio: Decimal | None


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def compute_signal(
    instrument_id: int, bars: list[Bar], cfg: JimConfig
) -> Signal | None:
    """日足系列(ts 昇順)から SMA クロスシグナルを判定する(純関数)。

    バー本数が足りない・出来高が欠測(entry のみ)・クロスが無い日は ``None``。
    """
    if len(bars) < cfg.min_bars:
        return None
    closes = [b.close for b in bars]
    fast = _mean(closes[-cfg.fast_window :])
    slow = _mean(closes[-cfg.slow_window :])
    prev_fast = _mean(closes[-cfg.fast_window - 1 : -1])
    prev_slow = _mean(closes[-cfg.slow_window - 1 : -1])

    last, prev = bars[-1], bars[-2]
    if prev_fast <= prev_slow and fast > slow:
        # 出来高フィルタ: 欠測は fail-closed(建てない)。
        window = [b.volume for b in bars[-cfg.volume_window :]]
        if any(v is None for v in window) or last.volume is None:
            return None
        avg_volume = _mean([v for v in window if v is not None])
        if avg_volume <= 0:
            return None
        ratio = last.volume / avg_volume
        if ratio < cfg.min_volume_ratio:
            return None
        return Signal(
            instrument_id=instrument_id, action=ENTER, rule_id=RULE_ID,
            bar_ts=last.ts, prev_bar_ts=prev.ts, fast=fast, slow=slow,
            prev_fast=prev_fast, prev_slow=prev_slow, volume_ratio=ratio,
        )
    if prev_fast >= prev_slow and fast < slow:
        return Signal(
            instrument_id=instrument_id, action=EXIT, rule_id=RULE_ID,
            bar_ts=last.ts, prev_bar_ts=prev.ts, fast=fast, slow=slow,
            prev_fast=prev_fast, prev_slow=prev_slow, volume_ratio=None,
        )
    return None


# ── thesis の自動生成(決定論の文字列組み立て。LLM は呼ばない)────────────────
def _evidence_refs(signal: Signal, cfg: JimConfig) -> list[dict[str, Any]]:
    """判定に使ったバー(当日・前日)への参照。いずれも as_of 以前で存在する。"""
    return [
        {
            "kind": "bar",
            "instrument_id": signal.instrument_id,
            "timeframe": cfg.timeframe,
            "ts": ts.isoformat(),
        }
        for ts in (signal.prev_bar_ts, signal.bar_ts)
    ]


def build_intent(signal: Signal, cfg: JimConfig) -> Intent:
    """シグナル → Intent(採否+論拠+反証条件)。数量は含まない。"""
    windows = f"{cfg.fast_window}日/{cfg.slow_window}日"
    if signal.action == ENTER:
        thesis = (
            f"規則 {signal.rule_id}: {windows} SMA のゴールデンクロス。"
            f"当日 SMA{cfg.fast_window}={signal.fast:.2f} > SMA{cfg.slow_window}="
            f"{signal.slow:.2f}(前日は {signal.prev_fast:.2f} ≤ {signal.prev_slow:.2f})。"
            f"出来高は直近{cfg.volume_window}日平均の {signal.volume_ratio:.2f} 倍で"
            f"フィルタ({cfg.min_volume_ratio})を満たす。"
            "銘柄固有の物語には依拠しない — 価格と出来高のみの統計的エッジであり、"
            "個別の当たり外れではなく多数の試行の期待値で評価する。"
        )
        invalidation = (
            f"SMA{cfg.fast_window} が SMA{cfg.slow_window} を下抜けた時点で降りる"
            "(デッドクロス = 本シグナルの前提であるトレンド継続の否定)。"
            "規則の改廃はバックテスト(E5/E9 の多重検定補正つき)によってのみ行い、"
            "個別銘柄の値動きを理由に例外を作らない。"
        )
        direction = "buy"
    else:
        thesis = (
            f"規則 {signal.rule_id}: {windows} SMA のデッドクロス。"
            f"当日 SMA{cfg.fast_window}={signal.fast:.2f} < SMA{cfg.slow_window}="
            f"{signal.slow:.2f}(前日は {signal.prev_fast:.2f} ≥ {signal.prev_slow:.2f})。"
            "建玉時の反証条件が成立したため全量手仕舞いする。"
        )
        invalidation = (
            "本手仕舞いの反証条件は無い(規則が定める解消であり裁量の余地を持たせない)。"
            "再度のゴールデンクロスが出れば新規建てとして改めて評価する。"
        )
        direction = "close"
    return Intent(
        fm=FM,
        instrument_id=signal.instrument_id,
        direction=direction,
        thesis_md=thesis,
        evidence_refs=_evidence_refs(signal, cfg),
        invalidation_md=invalidation,
        rule_id=signal.rule_id,
    )


# ── DB 読出し ─────────────────────────────────────────────────────────────────
def load_bars(
    conn: psycopg.Connection,
    instrument_id: int,
    *,
    as_of: datetime,
    cfg: JimConfig,
    lookback: int | None = None,
) -> list[Bar]:
    """point-in-time の日足系列(ts 昇順)。ts も as_of も判断時点以前のバーのみ。"""
    limit = lookback or cfg.min_bars
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts, close, volume FROM (
                SELECT DISTINCT ON (ts) ts, close, volume
                FROM market.bars
                WHERE instrument_id = %s AND timeframe = %s AND close IS NOT NULL
                  AND ts <= %s AND as_of <= %s
                ORDER BY ts DESC, as_of DESC
                LIMIT %s
            ) b ORDER BY ts
            """,
            (instrument_id, cfg.timeframe, as_of, as_of, limit),
        )
        return [
            Bar(
                ts=r[0],
                close=Decimal(r[1]),
                volume=None if r[2] is None else Decimal(r[2]),
            )
            for r in cur.fetchall()
        ]


# ── 日次実行 ──────────────────────────────────────────────────────────────────
def run_jim(
    conn: psycopg.Connection,
    run: Run,
    *,
    book_id: str,
    as_of: datetime,
    cfg: JimConfig | None = None,
    ips: IPSConfig | None = None,
    mandates: dict[str, Mandate] | None = None,
) -> dict[str, Any]:
    """Jim の日次サイクル: ユニバース走査 → シグナル → 記録 → ゲート投入。

    - ユニバースはマンデート(``config/mandates/jim.yaml``: liquid_equity / etf /
      index_futures)に属し**決定論分類の行がある**銘柄のみ。分類ルール(T-015)は
      流動性系タグを付けないため、curated 分類が供給されるまで本番のユニバースは空に
      なる(= 発注ゼロ)。これは fail-closed の設計どおりの挙動で、タグを緩めて
      埋めることはしない
    - 保有銘柄は exit シグナルの有無を必ず評価する(反証条件の点検)
    """
    cfg = cfg or JimConfig.load()
    if ips is None or mandates is None:
        loaded_ips, loaded_mandates = load_and_validate()
        ips = ips or loaded_ips
        mandates = mandates or loaded_mandates
    mandate = mandates[FM]

    universe = base.load_universe(conn, mandate, as_of=as_of, limit=cfg.max_universe)
    candidates = {c.instrument_id: c for c in universe}
    positions = base.load_positions(conn, book_id)
    held = set(held_positions(positions, FM))

    entries: list[Intent] = []
    exits: list[Intent] = []
    scanned = 0
    for candidate in universe:
        bars = load_bars(conn, candidate.instrument_id, as_of=as_of, cfg=cfg)
        scanned += 1
        signal = compute_signal(candidate.instrument_id, bars, cfg)
        if signal is None:
            continue
        is_held = candidate.instrument_id in held
        if signal.action == EXIT and is_held:
            exits.append(build_intent(signal, cfg))
        elif signal.action == ENTER and not is_held:
            entries.append(build_intent(signal, cfg))

    entries = entries[: cfg.max_new_positions]
    result = base.submit_intents(
        conn, run, exits + entries,
        mandate=mandate, max_slots=cfg.max_slots, candidates=candidates,
        producer=cfg.producer, book_id=book_id, as_of=as_of,
        ips=ips, mandates=mandates,
    )
    return {
        "universe": len(universe),
        "scanned": scanned,
        "entries": len(entries),
        "exits": len(exits),
        **result.as_dict(),
    }


__all__ = [
    "ENTER",
    "EXIT",
    "FM",
    "RULE_ID",
    "Bar",
    "Signal",
    "build_intent",
    "compute_signal",
    "load_bars",
    "run_jim",
]
