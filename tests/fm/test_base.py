"""fm.base(提案 → ゲート経路)のテスト。

独立役員審査 2026-08-03 の是正の回帰テスト:

- **C-1**: 同一銘柄の重複提案でポッド内集中度上限を破れないこと(重複排除・実行内 held 更新・
  未約定の通過注文の占有)。ゲート G-3 は同一実行内の pending 注文を約定後想定に加算しない
  ため、この防御が破れると各注文は個別に上限内でも合計で上限を超える
- **C-8**: 生成側(LLM)の出力順がスロット配分の優先度を決めないこと
- **C-7**: 語彙外 direction を黙って落とさないこと
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ryza.fm import base
from ryza.fm.base import Intent
from ryza.ips import load_and_validate

BOOK = "DEMO_FUND"
FM = "ben"


def _intent(instrument_id: int, doc_id: int, direction: str = "buy") -> Intent:
    return Intent(
        fm=FM,
        instrument_id=instrument_id,
        direction=direction,
        thesis_md="安全域がある。",
        evidence_refs=[{"kind": "document", "doc_id": doc_id}],
        invalidation_md="営業利益率が2四半期連続で 8% を下回ったら降りる。",
        model="test-mid",
    )


@pytest.fixture
def universe(instrument, classify, insert_bars, nav_snapshot):
    """Ben のユニバース銘柄を作り (instrument_id, candidate 辞書) を返す。"""

    def _make(symbols: list[str], close: float = 1000.0):
        nav_snapshot()
        ids = []
        for symbol in symbols:
            iid = instrument(symbol=symbol)
            classify(iid, universe_tags=("jp_equity_cash",))
            insert_bars(iid, [close] * 3, volumes=[100_000] * 3)
            ids.append(iid)
        return ids

    return _make


@pytest.fixture
def mandates():
    return load_and_validate()[1]


def _submit(conn, run, intents, mandates, *, as_of, max_slots=5):
    mandate = mandates[FM]
    universe = base.load_universe(conn, mandate, as_of=as_of).candidates
    candidates = {c.instrument_id: c for c in universe}
    return base.submit_intents(
        conn, run, intents,
        mandate=mandate, max_slots=max_slots, candidates=candidates,
        producer="test.fm", book_id=BOOK, as_of=as_of,
    )


# ── C-1: 重複排除 ─────────────────────────────────────────────────────────────
def test_dedupe_intents_keeps_first():
    a, b, c = _intent(1, 1), _intent(1, 2), _intent(2, 3)
    kept, dropped = base.dedupe_intents([a, b, c])
    assert kept == [a, c]
    assert len(dropped) == 1 and dropped[0]["instrument_id"] == 1


def test_duplicate_candidates_cannot_break_pod_concentration(
    conn, run, as_of, universe, insert_document, mandates
):
    """同一銘柄×3 の提案でも注文は1本(合計がポッド内集中度上限を超えない)。"""
    (iid,) = universe(["7203.T"])
    doc_id = insert_document()
    result = _submit(
        conn, run, [_intent(iid, doc_id) for _ in range(3)], mandates, as_of=as_of
    )
    assert result.proposed == 1 and result.passed == 1
    assert sum("重複" in s["reason"] for s in result.skipped) == 2
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), COALESCE(sum(qty * ref_price), 0)
            FROM trading.orders WHERE fm = %s AND instrument_id = %s
            """,
            (FM, iid),
        )
        count, notional = cur.fetchone()
    # ¥2,000,000 × 40%(ポッド内集中度上限)= ¥800,000 を超えない。
    assert count == 1
    assert Decimal(notional) <= Decimal("0.4") * Decimal(2_000_000)


def test_pending_order_occupies_slot_across_runs(
    conn, run, as_of, universe, insert_document, mandates
):
    """未約定の通過注文がある銘柄には二度目の割り当てをしない(実行またぎの二重防止)。"""
    (iid,) = universe(["7203.T"])
    doc_id = insert_document()
    first = _submit(conn, run, [_intent(iid, doc_id)], mandates, as_of=as_of)
    assert first.passed == 1

    second = _submit(conn, run, [_intent(iid, doc_id)], mandates, as_of=as_of)
    assert second.proposed == 0
    assert "保有済み(スロット占有)" in [s["reason"] for s in second.skipped]


def test_pending_orders_consume_free_slots(
    conn, run, as_of, universe, insert_document, mandates
):
    """未約定の通過注文はスロットを消費する(空きスロット数の計算に入る)。"""
    ids = universe(["1001.T", "1002.T", "1003.T"])
    doc_id = insert_document()
    first = _submit(
        conn, run, [_intent(ids[0], doc_id)], mandates, as_of=as_of, max_slots=3
    )
    assert first.passed == 1

    # 残り2スロットに対し2件 → 通る。3件目(既に埋まっている)は空きなし。
    second = _submit(
        conn, run, [_intent(ids[1], doc_id), _intent(ids[2], doc_id)],
        mandates, as_of=as_of, max_slots=3,
    )
    assert second.passed == 2
    third_ids = universe(["1004.T"])
    third = _submit(
        conn, run, [_intent(third_ids[0], doc_id)], mandates, as_of=as_of, max_slots=3
    )
    assert third.proposed == 0
    assert "空きスロットなし" in [s["reason"] for s in third.skipped]


# ── C-8: 出力順が配分優先度を決めない ────────────────────────────────────────
def test_allocation_order_is_deterministic(
    conn, run, as_of, universe, insert_document, mandates
):
    """入力順を逆にしても、スロットは instrument_id 昇順に割り当てられる。"""
    ids = sorted(universe(["3001.T", "3002.T", "3003.T"]))
    doc_id = insert_document()
    reversed_intents = [_intent(i, doc_id) for i in reversed(ids)]
    result = _submit(
        conn, run, reversed_intents, mandates, as_of=as_of, max_slots=3
    )
    assert [o["instrument_id"] for o in result.orders] == ids


def test_closes_are_processed_before_buys(
    conn, run, as_of, universe, insert_document, mandates
):
    """クローズを先に処理してスロットを空けてから新規建てを評価する。"""
    ids = sorted(universe(["4001.T", "4002.T", "4003.T", "4004.T"]))
    held_ids, new_id = ids[:3], ids[3]
    doc_id = insert_document()
    with conn.cursor() as cur:
        for held_id in held_ids:
            cur.execute(
                """
                INSERT INTO trading.positions
                    (book_id, fm, instrument_id, asset_class, qty, avg_cost, run_id)
                VALUES (%s, %s, %s, 'equity_jp', 400, 1000, %s)
                """,
                (BOOK, FM, held_id, run.run_id),
            )
    intents = [_intent(new_id, doc_id), _intent(held_ids[0], doc_id, direction="close")]
    result = _submit(conn, run, intents, mandates, as_of=as_of, max_slots=3)
    # スロット3・保有3 → クローズが先に処理されなければ新規建ては通らない。
    assert [o["direction"] for o in result.orders] == ["close", "buy"]
    assert result.passed == 2


# ── C-7: 語彙外 direction ─────────────────────────────────────────────────────
def test_unknown_direction_is_skipped_with_reason(
    conn, run, as_of, universe, insert_document, mandates
):
    """第一陣が扱わない direction は黙って落とさず理由を残す。"""
    (iid,) = universe(["7203.T"])
    doc_id = insert_document()
    result = _submit(
        conn, run, [_intent(iid, doc_id, direction="short")], mandates, as_of=as_of
    )
    assert result.proposed == 0
    assert any("direction" in s["reason"] for s in result.skipped)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trading.fm_theses WHERE instrument_id = %s", (iid,))
        assert cur.fetchone()[0] == 0
