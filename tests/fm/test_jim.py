"""fm.jim のテスト(T-017): 固定バー系列に対するシグナルの数値検証+日次実行。

シグナル判定は純関数(``compute_signal``)なので、期待値を手で言える系列を作って
検証する。テスト用の窓は短くして(fast=3 / slow=5)本数を人間が数えられるようにし、
発効 config(20/60)との整合は別テスト(``test_shipped_config``)で見る。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ryza.fm import jim
from ryza.fm.config import JimConfig
from ryza.fm.jim import ENTER, EXIT, Bar, compute_signal
from ryza.ips import load_and_validate


def _cfg(**overrides) -> JimConfig:
    defaults = dict(
        version="test", producer="test.jim",
        fast_window=3, slow_window=5, volume_window=3,
        min_volume_ratio=Decimal(1), timeframe="1d",
        max_slots=8, max_new_positions=3, max_universe=200,
    )
    defaults.update(overrides)
    return JimConfig(**defaults)


def _bars(closes, volumes=None) -> list[Bar]:
    from datetime import UTC, datetime, timedelta

    base_ts = datetime(2026, 6, 1, tzinfo=UTC)
    return [
        Bar(
            ts=base_ts + timedelta(days=i),
            close=Decimal(str(c)),
            volume=None if volumes is None else Decimal(str(volumes[i])),
        )
        for i, c in enumerate(closes)
    ]


# ── シグナルの数値検証 ────────────────────────────────────────────────────────
def test_golden_cross_enters():
    """下降後に急反発 → 3日 SMA が 5日 SMA を上抜けた日に enter。

    closes = [10,10,10,10,10,10,16] のとき
      当日: fast=(10+10+16)/3=12.0 > slow=(10*4+16)/5=11.2
      前日: fast=10.0 = slow=10.0(前日は上抜けていない)
    """
    cfg = _cfg()
    bars = _bars([10, 10, 10, 10, 10, 10, 16], volumes=[100] * 6 + [200])
    signal = compute_signal(1, bars, cfg)
    assert signal is not None and signal.action == ENTER
    assert signal.fast == Decimal(12) and signal.slow == Decimal("11.2")
    assert signal.prev_fast == signal.prev_slow == Decimal(10)
    assert signal.volume_ratio > 1
    assert signal.rule_id == jim.RULE_ID


def test_dead_cross_exits():
    """急落で 3日 SMA が 5日 SMA を下抜けた日に exit(出来高フィルタは掛からない)。"""
    cfg = _cfg()
    bars = _bars([10, 10, 10, 10, 10, 10, 4], volumes=[100] * 7)
    signal = compute_signal(1, bars, cfg)
    assert signal is not None and signal.action == EXIT
    assert signal.fast == Decimal(8) and signal.slow == Decimal("8.8")
    assert signal.volume_ratio is None


def test_no_cross_returns_none():
    """クロスが起きていない(ずっと横ばい)日はシグナルなし。"""
    cfg = _cfg()
    assert compute_signal(1, _bars([10] * 8, volumes=[100] * 8), cfg) is None


def test_trend_continuation_is_not_a_new_signal():
    """既にクロス済みで上昇が続いている日は enter しない(クロスした日だけ)。"""
    cfg = _cfg()
    bars = _bars([10, 10, 10, 10, 10, 16, 17], volumes=[100] * 7)
    signal = compute_signal(1, bars, cfg)
    assert signal is None


def test_volume_filter_blocks_entry():
    """ゴールデンクロスでも出来高が平均割れなら建てない(執行コスト — E4)。"""
    cfg = _cfg(min_volume_ratio=Decimal("1.5"))
    bars = _bars([10, 10, 10, 10, 10, 10, 16], volumes=[100] * 6 + [110])
    assert compute_signal(1, bars, cfg) is None


def test_missing_volume_is_fail_closed():
    """出来高欠測は「たぶん十分」とみなさず建てない(fail-closed)。"""
    cfg = _cfg()
    bars = _bars([10, 10, 10, 10, 10, 10, 16])  # volume なし
    assert compute_signal(1, bars, cfg) is None


def test_insufficient_history_returns_none():
    cfg = _cfg()
    assert compute_signal(1, _bars([10, 11, 12], volumes=[100] * 3), cfg) is None


def test_shipped_config_windows():
    """発効中の config/fm_jim.yaml が 20日/60日+出来高フィルタであること。"""
    cfg = JimConfig.load()
    assert (cfg.fast_window, cfg.slow_window) == (20, 60)
    assert cfg.volume_window == 20 and cfg.min_volume_ratio == Decimal(1)
    assert cfg.min_bars == 61


def test_fast_window_must_be_shorter(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "version: '1'\nsignal: {fast_window: 60, slow_window: 20, volume_window: 20,"
        " min_volume_ratio: 1}\nsizing: {max_slots: 8}\n"
        "max_new_positions: 3\nmax_universe: 200\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="fast_window"):
        JimConfig.load(path)


# ── Intent の生成(long-only の固定)─────────────────────────────────────────
def test_intents_are_long_only():
    """Jim が生成する direction は buy / close のみ。short は出さない(第一陣)。"""
    cfg = _cfg()
    enter = compute_signal(1, _bars([10, 10, 10, 10, 10, 10, 16], [100] * 6 + [200]), cfg)
    exit_ = compute_signal(1, _bars([10, 10, 10, 10, 10, 10, 4], [100] * 7), cfg)
    directions = {jim.build_intent(s, cfg).direction for s in (enter, exit_)}
    assert directions == {"buy", "close"}


def test_intent_carries_rule_and_invalidation():
    cfg = _cfg()
    signal = compute_signal(1, _bars([10, 10, 10, 10, 10, 10, 16], [100] * 6 + [200]), cfg)
    intent = jim.build_intent(signal, cfg)
    assert intent.rule_id == jim.RULE_ID and intent.model is None
    assert "降りる" in intent.invalidation_md
    assert len(intent.evidence_refs) == 2
    assert all(r["kind"] == "bar" for r in intent.evidence_refs)


# ── 日次実行(DB + ゲート連携)────────────────────────────────────────────────
def _cross_series(n_flat: int = 60) -> tuple[list[int], list[int]]:
    """末日にゴールデンクロスする終値・出来高系列(発効 config の窓に合わせる)。"""
    closes = [1000] * n_flat + [1600]
    volumes = [100_000] * n_flat + [500_000]
    return closes, volumes


def test_run_jim_end_to_end_passes_gate(
    conn, run, as_of, instrument, classify, insert_bars, nav_snapshot
):
    """シグナル → thesis 記録 → ゲート pass → orders 行(thesis_id つき)。"""
    nav_snapshot()
    iid = instrument(symbol="1301.T")
    classify(iid, universe_tags=("liquid_equity",))
    closes, volumes = _cross_series()
    insert_bars(iid, closes, volumes=volumes)

    ips, mandates = load_and_validate()
    result = jim.run_jim(
        conn, run, book_id="DEMO_FUND", as_of=as_of, ips=ips, mandates=mandates
    )
    assert result["universe"] == 1
    assert result["entries"] == 1 and result["passed"] == 1 and result["blocked"] == 0

    order = result["orders"][0]
    assert order["direction"] == "buy" and order["side"] == "buy"
    # 1スロット = ¥2,000,000 / 8 = ¥250,000、価格 ¥1,600・単元 100株 → 100 株。
    assert order["qty"] == "100"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, thesis_id, fm FROM trading.orders WHERE id = %s",
            (order["order_id"],),
        )
        status, thesis_id, fm = cur.fetchone()
    assert status == "passed" and thesis_id == order["thesis_id"] and fm == "jim"


def test_jim_duplicate_intents_produce_one_order(
    conn, run, as_of, instrument, classify, insert_bars, nav_snapshot
):
    """Jim 経路でも同一銘柄の重複提案は1本に潰れる(審査 C-1・base の共通防御)。"""
    from ryza.fm import base
    from ryza.ips import load_and_validate

    nav_snapshot()
    iid = instrument(symbol="1306.T")
    classify(iid, universe_tags=("liquid_equity",))
    closes, volumes = _cross_series()
    insert_bars(iid, closes, volumes=volumes)

    cfg = JimConfig.load()
    signal = jim.compute_signal(iid, jim.load_bars(conn, iid, as_of=as_of, cfg=cfg), cfg)
    assert signal is not None
    intents = [jim.build_intent(signal, cfg) for _ in range(3)]

    ips, mandates = load_and_validate()
    mandate = mandates[jim.FM]
    universe = base.load_universe(conn, mandate, as_of=as_of).candidates
    candidates = {c.instrument_id: c for c in universe}
    result = base.submit_intents(
        conn, run, intents,
        mandate=mandate, max_slots=cfg.max_slots, candidates=candidates,
        producer=cfg.producer, book_id="DEMO_FUND", as_of=as_of, ips=ips, mandates=mandates,
    )
    assert result.proposed == 1 and result.passed == 1
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), COALESCE(sum(qty * ref_price), 0)
            FROM trading.orders WHERE fm = 'jim' AND instrument_id = %s
            """,
            (iid,),
        )
        count, notional = cur.fetchone()
    # ポッド内集中度上限 20% × 仮想資本 ¥2,000,000 = ¥400,000 を超えない。
    assert count == 1 and Decimal(notional) <= Decimal(400_000)


def test_run_jim_records_blocked_proposals(
    conn, run, as_of, instrument, classify, insert_bars, nav_snapshot
):
    """ゲート block でも thesis は残り、注文は blocked として記録される(学習材料)。"""
    nav_snapshot()
    with conn.cursor() as cur:  # Kill Switch 相当(G-0 で block)
        cur.execute("UPDATE ops.trading_state SET state = 'frozen'")
    iid = instrument(symbol="1302.T")
    classify(iid, universe_tags=("liquid_equity",))
    closes, volumes = _cross_series()
    insert_bars(iid, closes, volumes=volumes)

    result = jim.run_jim(conn, run, book_id="DEMO_FUND", as_of=as_of)
    assert result["blocked"] == 1 and result["passed"] == 0
    order = result["orders"][0]
    assert order["verdict"] == "block"
    from ryza.fm.theses import recent_theses

    rows = recent_theses(conn, "jim", limit=5)
    assert rows[0].thesis_id == order["thesis_id"]
    assert rows[0].order_status == "blocked" and rows[0].gate_verdict == "block"


def test_run_jim_universe_is_fail_closed_without_classification(
    conn, run, as_of, instrument, insert_bars, nav_snapshot
):
    """決定論分類の行が無い銘柄はユニバースに入らない(タグを緩めて埋めない)。"""
    nav_snapshot()
    iid = instrument(symbol="1303.T")
    closes, volumes = _cross_series()
    insert_bars(iid, closes, volumes=volumes)
    result = jim.run_jim(conn, run, book_id="DEMO_FUND", as_of=as_of)
    assert result["universe"] == 0 and result["proposed"] == 0


def test_run_jim_ignores_instruments_outside_mandate_universe(
    conn, run, as_of, instrument, classify, insert_bars, nav_snapshot
):
    """Ben のユニバース(jp_equity_cash)だけの銘柄は Jim の走査対象にならない。"""
    nav_snapshot()
    iid = instrument(symbol="1304.T")
    classify(iid, universe_tags=("jp_equity_cash",))
    closes, volumes = _cross_series()
    insert_bars(iid, closes, volumes=volumes)
    result = jim.run_jim(conn, run, book_id="DEMO_FUND", as_of=as_of)
    assert result["universe"] == 0


def test_run_jim_no_future_bars(
    conn, run, as_of, instrument, classify, insert_bars, nav_snapshot
):
    """as_of より後に知り得たバー(as_of 列が未来)はシグナル計算に混ざらない。"""
    from datetime import timedelta

    nav_snapshot()
    iid = instrument(symbol="1305.T")
    classify(iid, universe_tags=("liquid_equity",))
    closes, volumes = _cross_series()
    # 全バーの「知り得た時点」を判断時点より後にする → 使えるバーが 0 本。
    insert_bars(iid, closes, volumes=volumes, as_of=as_of + timedelta(days=1))
    result = jim.run_jim(conn, run, book_id="DEMO_FUND", as_of=as_of)
    assert result["entries"] == 0 and result["proposed"] == 0
