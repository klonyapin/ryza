"""engine.evaluate の全キー・スナップショット固定(独立役員審査 2026-08-04 推奨)。

**目的は「判定が黙って変わらないこと」の恒久的な証拠**である。改定時の差分試験
(旧実装と新実装を同一入力で突き合わせる)はその場限りで、コードが置き換われば
再現できない。代わりにここでは、決定論の代表ケース集に対する ``evaluate`` の
**返り値の全キー**をゴールデンファイル(``engine_snapshot.json``)に固定する。
以後、フラグ・測定値・理由コード・注記のいずれが動いてもテスト差分に現れる。

ケース集は乱数ではなく**固定シードの決定論生成**(``random.Random(_SEED)``)で、
審査の差分試験と同じ次元をなぞる: NAV 1〜30 点・保有 0〜4 銘柄・観測数
0/1/5/10/19/20/25/40・銘柄ごとの日付ずれ有無。100 ケースで実行時間は数十 ms。

**ゴールデンの更新は判定の変更を意味する**。意図した変更なら::

    RYZA_UPDATE_ENGINE_SNAPSHOT=1 uv run pytest tests/risk/test_engine_invariance.py

で再生成し、**差分を意見書に添えて**保護領域の承認手続に載せること(定款第5条)。
差分が意図せず出た場合は実装のリグレッションである。
"""

from __future__ import annotations

import json
import os
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from ryza.risk.engine import NavPoint, RiskPosition, evaluate

_SEED = 20260804
_N_CASES = 100
_SNAPSHOT = Path(__file__).with_name("engine_snapshot.json")

#: 銘柄リターン系列の観測数(審査の差分試験と同じ刻み — min_obs=20 の境界を挟む)。
_OBS_CHOICES = (0, 1, 5, 10, 19, 20, 25, 40)
_ASSET_CLASSES = ("equity_jp", "equity_us", "etf_jp", "cash")


def _round(value: float | None, digits: int = 12) -> float | None:
    """浮動小数の丸め(プラットフォーム差の雑音を落とし、判定変更だけを残す)。"""
    return None if value is None else round(value, digits)


def _make_case(rng: random.Random, index: int) -> dict:
    """1 ケース分の入力(NAV 系列・保有・銘柄リターン)を決定論的に組む。"""
    n_points = rng.randint(1, 30)
    nav = Decimal(rng.randrange(100_000, 20_000_000))
    series: list[NavPoint] = []
    day = date(2029, 6, 1)
    for i in range(n_points):
        # 日次変化 ±3%(整数円に丸めて Decimal のまま持つ)。
        moved = nav * Decimal(str(1 + rng.uniform(-0.03, 0.03)))
        nav = max(Decimal(1), moved.quantize(Decimal(1)))
        flow_eop = Decimal(rng.choice((0, 0, 0, 200_000, -150_000)))
        flow_bop = Decimal(rng.choice((0, 0, 0, 300_000)))
        series.append(
            NavPoint(day=day + timedelta(days=i), nav=nav, flow_eop=flow_eop, flow_bop=flow_bop)
        )
    if index % 7 == 3 and len(series) > 1:
        # 7 ケースに 1 つは末尾に急落を入れる。±3% の乱歩だけでは dd_soft/dd_hard の
        # 分岐をほとんど踏まず、「フラグが立たないこと」しか固定できないため。
        last = series[-1]
        series[-1] = NavPoint(
            day=last.day,
            nav=(last.nav * Decimal("0.6")).quantize(Decimal(1)),
            flow_eop=last.flow_eop,
            flow_bop=last.flow_bop,
        )

    positions: list[RiskPosition] = []
    returns: dict[int, dict[date, float]] = {}
    for instrument_id in range(1, rng.randint(0, 4) + 1):
        value = Decimal(rng.randrange(-3_000_000, 3_000_000))
        positions.append(
            RiskPosition(
                instrument_id=instrument_id,
                asset_class=rng.choice(_ASSET_CLASSES),
                value=value,
            )
        )
        n_obs = rng.choice(_OBS_CHOICES)
        # 日付ずれ: 銘柄ごとに起点をずらし「共通観測日ゼロ」も出現させる。
        start = date(2029, 1, 1) + timedelta(days=rng.choice((0, 0, 1, 30, 200)))
        returns[instrument_id] = {
            start + timedelta(days=k): round(rng.uniform(-0.08, 0.08), 6)
            for k in range(n_obs)
        }
    return {"index": index, "series": series, "positions": positions, "returns": returns}


def _cases() -> list[dict]:
    rng = random.Random(_SEED)
    return [_make_case(rng, i) for i in range(_N_CASES)]


def _snapshot(case: dict, ips) -> dict:
    """1 ケースの ``evaluate`` 返り値を全キー JSON 化する(丸め以外は無加工)。"""
    state = evaluate(case["series"], case["positions"], case["returns"], ips)
    return {
        "index": case["index"],
        "input": {
            "n_points": len(case["series"]),
            "last_nav": str(case["series"][-1].nav),
            "positions": [
                {"instrument_id": p.instrument_id, "asset_class": p.asset_class,
                 "value": str(p.value)}
                for p in case["positions"]
            ],
            "n_obs": {str(i): len(r) for i, r in sorted(case["returns"].items())},
        },
        "as_of_day": state.as_of_day.isoformat(),
        "nav": str(state.nav),
        "peak_nav": str(state.peak_nav),
        "drawdown": str(state.drawdown),
        "n_returns": state.n_returns,
        "sufficient": state.sufficient,
        "ewma_vol_annual": _round(state.ewma_vol_annual),
        "es95": {
            "historical": _round(state.es95.historical),
            "parametric": _round(state.es95.parametric),
            "adopted": _round(state.es95.adopted),
            "n_obs": state.es95.n_obs,
            "excluded": list(state.es95.excluded),
            "deferral_reason": state.es95.deferral_reason,
        },
        "dd_soft": state.dd_soft,
        "dd_hard": state.dd_hard,
        "vol_exceeded": state.vol_exceeded,
        "es_exceeded": state.es_exceeded,
        "deferred": [
            {"metric": d.metric, "reason": d.reason,
             "observed": d.observed, "required": d.required}
            for d in state.deferred
        ],
        "excluded": [
            {"instrument_id": e.instrument_id, "measure": e.measure, "reason": e.reason,
             "observed": e.observed, "required": e.required}
            for e in state.excluded
        ],
        "notes": list(state.notes),
    }


def _snapshots(ips) -> list[dict]:
    return [_snapshot(case, ips) for case in _cases()]


def test_case_generation_is_deterministic():
    """ケース集がシード固定で再現する(ゴールデンの前提 — 生成が揺れたら無意味)。"""
    first, second = _cases(), _cases()
    assert [str(c["series"][-1].nav) for c in first] == [
        str(c["series"][-1].nav) for c in second
    ]
    assert [sorted(c["returns"]) for c in first] == [sorted(c["returns"]) for c in second]


def test_snapshot_covers_the_reviewed_dimensions(ips):
    """代表ケース集が判定の分岐を実際に踏んでいる(全部 unknown では証拠にならない)。"""
    snaps = _snapshots(ips)
    assert len(snaps) == _N_CASES
    # 保留理由 5 種すべてを踏む(重大-1 で分けた no_common_days を含む)。
    assert {d["reason"] for s in snaps for d in s["deferred"]} == {
        "insufficient_returns", "insufficient_obs", "no_observations",
        "majority_excluded", "no_common_days",
    }
    # フラグは「立つ側」も固定する(立たないことだけを固定しても回帰を捕まえない)。
    for flag in ("dd_soft", "dd_hard", "vol_exceeded", "es_exceeded"):
        assert any(s[flag] for s in snaps), flag
    assert any(s["sufficient"] for s in snaps) and any(not s["sufficient"] for s in snaps)
    assert any(s["es95"]["excluded"] for s in snaps)
    assert any(s["es95"]["n_obs"] > 0 for s in snaps)


def test_evaluate_matches_the_frozen_snapshot(ips):
    """``evaluate`` の全キーがゴールデンと一致する(判定変更は必ず差分に出る)。

    更新するときは意図した判定変更であることを意見書に残すこと(モジュール docstring)。
    """
    actual = _snapshots(ips)
    if os.environ.get("RYZA_UPDATE_ENGINE_SNAPSHOT"):
        _SNAPSHOT.write_text(
            json.dumps(actual, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    expected = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        assert got == want, f"ケース {want['index']} の評価結果が固定値と違う"
