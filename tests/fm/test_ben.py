"""fm.ben のテスト(T-017)。実 API を呼ばず ``FixtureProvider`` を注入する。

検証の中心は「LLM 出力の**採否だけ**を使い、不備のある出力を拒否すること」:
スキーマ適合(short を返せない)・証憑必須・反証条件必須・point-in-time・候補数上限、
そしてサイズが LLM の言い分と無関係であること。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from ryza.fm import ben
from ryza.fm.config import BenConfig
from ryza.gate.compliance import PositionState
from ryza.research.llm import FixtureProvider, StructuredLLM
from ryza.research.schemas import SchemaError

BOOK = "DEMO_FUND"
MODEL = "test-mid"


def _cfg(**overrides) -> BenConfig:
    defaults = dict(
        version="test", producer="test.ben", model_tier="mid", weekday=1,
        max_slots=5, max_candidates=2, max_documents=10, doc_body_chars=500,
        recent_theses=5,
    )
    defaults.update(overrides)
    return BenConfig(**defaults)


def _llm(responses) -> tuple[StructuredLLM, FixtureProvider]:
    provider = FixtureProvider(responses)
    return StructuredLLM(provider, None, dept_tag="fm.ben"), provider


def _candidate(instrument_id: int, doc_id: int, **overrides) -> dict:
    payload = {
        "instrument_id": instrument_id,
        "direction": "buy",
        "thesis_md": "PBR 0.6・自己資本比率 60%・営業利益率 10% の継続。安全域がある。",
        "evidence_refs": [{"kind": "document", "doc_id": doc_id}],
        "invalidation_md": "営業利益率が2四半期連続で 8% を下回ったら降りる。",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def ben_universe(instrument, classify, insert_bars, nav_snapshot):
    """Ben のユニバース(日本個別株)を1件用意して instrument_id を返す。"""

    def _make(symbol: str = "7203.T", close: float = 1000.0) -> int:
        nav_snapshot()
        iid = instrument(symbol=symbol)
        classify(iid, universe_tags=("jp_equity_cash",))
        insert_bars(iid, [close] * 3, volumes=[100_000] * 3)
        return iid

    return _make


# ── 正常系 ────────────────────────────────────────────────────────────────────
def test_ben_proposes_and_passes_gate(conn, run, as_of, ben_universe, insert_document):
    iid = ben_universe()
    doc_id = insert_document()
    llm, provider = _llm([{"candidates": [_candidate(iid, doc_id)], "reviews": []}])

    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["candidates"] == 1 and result["passed"] == 1
    order = result["orders"][0]
    # 1スロット = ¥2,000,000 / 5 = ¥400,000、価格 ¥1,000・単元 100株 → 400 株。
    assert order["qty"] == "400" and order["side"] == "buy"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT model, rule_id, direction FROM trading.fm_theses WHERE thesis_id = %s",
            (order["thesis_id"],),
        )
        assert cur.fetchone() == (MODEL, None, "buy")
    # 着任プロンプトに charter が含まれる(役職資産の読み込み)。
    assert "職務規程" in provider.calls[0]["system"]


def test_ben_size_is_independent_of_llm_wording(
    conn, run, as_of, ben_universe, insert_document
):
    """LLM が確信度を主張しても数量は変わらない(不変原則1)。"""
    iid = ben_universe()
    doc_id = insert_document()
    confident = _candidate(iid, doc_id)
    confident["confidence"] = 0.99  # スキーマ外の追加プロパティ(下流は読まない)
    confident["suggested_weight"] = 1.0
    llm, _ = _llm([{"candidates": [confident], "reviews": []}])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["orders"][0]["qty"] == "400"  # 確信度に依らず1スロット


# ── 出力の拒否 ────────────────────────────────────────────────────────────────
def test_short_direction_is_schema_violation(conn, run, as_of, ben_universe, insert_document):
    """LLM が short を返してもスキーマ検証で落ちる(第一陣は long-only)。"""
    iid = ben_universe()
    doc_id = insert_document()
    bad = _candidate(iid, doc_id, direction="short")
    llm, _ = _llm([{"candidates": [bad], "reviews": []}])
    with pytest.raises(SchemaError):
        ben.run_ben(conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg())


def test_missing_invalidation_is_schema_violation(
    conn, run, as_of, ben_universe, insert_document
):
    iid = ben_universe()
    doc_id = insert_document()
    bad = _candidate(iid, doc_id)
    del bad["invalidation_md"]
    llm, _ = _llm([{"candidates": [bad], "reviews": []}])
    with pytest.raises(SchemaError):
        ben.run_ben(conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg())


def test_missing_evidence_is_schema_violation(conn, run, as_of, ben_universe, insert_document):
    iid = ben_universe()
    doc_id = insert_document()
    bad = _candidate(iid, doc_id)
    del bad["evidence_refs"]
    llm, _ = _llm([{"candidates": [bad], "reviews": []}])
    with pytest.raises(SchemaError):
        ben.run_ben(conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg())


def test_empty_evidence_list_is_rejected(conn, run, as_of, ben_universe, insert_document):
    """スキーマは通る空配列も、候補としては拒否する(証憑ゼロの提案は作らない)。"""
    iid = ben_universe()
    doc_id = insert_document()
    llm, _ = _llm([
        {"candidates": [_candidate(iid, doc_id, evidence_refs=[])], "reviews": []}
    ])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["candidates"] == 0 and result["proposed"] == 0
    assert "証憑" in result["rejected"][0]["reason"]


def test_blank_invalidation_is_rejected(conn, run, as_of, ben_universe, insert_document):
    iid = ben_universe()
    doc_id = insert_document()
    llm, _ = _llm([
        {"candidates": [_candidate(iid, doc_id, invalidation_md="  ")], "reviews": []}
    ])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["candidates"] == 0
    assert "反証条件" in result["rejected"][0]["reason"]


def test_future_evidence_is_rejected(conn, run, as_of, ben_universe, insert_document):
    """as_of より新しい文書を証憑にした候補は拒否する(point-in-time — 不変原則4)。"""
    iid = ben_universe()
    future_doc = insert_document(as_of=as_of + timedelta(days=1))
    llm, _ = _llm([{"candidates": [_candidate(iid, future_doc)], "reviews": []}])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["candidates"] == 0
    assert "未来情報" in result["rejected"][0]["reason"]


def test_out_of_universe_candidate_is_rejected(conn, run, as_of, ben_universe, insert_document):
    ben_universe()
    doc_id = insert_document()
    llm, _ = _llm([{"candidates": [_candidate(999_999_999, doc_id)], "reviews": []}])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["candidates"] == 0
    assert "ユニバース外" in result["rejected"][0]["reason"]


def test_candidate_cap_is_enforced(conn, run, as_of, ben_universe, insert_document):
    """候補数上限を超えた分は決定論に切り捨てる。"""
    ids = [ben_universe(symbol=f"100{i}.T") for i in range(3)]
    doc_id = insert_document()
    llm, _ = _llm([
        {"candidates": [_candidate(i, doc_id) for i in ids], "reviews": []}
    ])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg(max_candidates=2)
    )
    assert result["candidates"] == 2 and result["passed"] == 2


# ── 保有の見直し(invalidation 成立チェック)──────────────────────────────────
def _hold(conn, run, instrument_id: int, qty: int = 400) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trading.positions
                (book_id, fm, instrument_id, asset_class, qty, avg_cost, run_id)
            VALUES (%s, 'ben', %s, 'equity_jp', %s, 1000, %s)
            """,
            (BOOK, instrument_id, qty, run.run_id),
        )


def test_invalidated_holding_is_closed(conn, run, as_of, ben_universe, insert_document):
    iid = ben_universe()
    _hold(conn, run, iid)
    doc_id = insert_document()
    llm, provider = _llm([
        {
            "candidates": [],
            "reviews": [
                {
                    "instrument_id": iid,
                    "invalidated": True,
                    "rationale_md": "営業利益率が2四半期連続で 8% を下回った。",
                    "evidence_refs": [{"kind": "document", "doc_id": doc_id}],
                }
            ],
        }
    ])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["closes"] == 1
    order = result["orders"][0]
    assert order["direction"] == "close" and order["side"] == "sell"
    assert order["qty"] == "400"  # 全量手仕舞い
    # 保有の建玉根拠と反証条件がプロンプトに載っている(見直しの入力)。
    assert "holdings" in provider.calls[0]["user"]


def test_holding_not_invalidated_is_left_alone(
    conn, run, as_of, ben_universe, insert_document
):
    iid = ben_universe()
    _hold(conn, run, iid)
    doc_id = insert_document()
    llm, _ = _llm([
        {
            "candidates": [],
            "reviews": [
                {
                    "instrument_id": iid,
                    "invalidated": False,
                    "rationale_md": "論点は維持されている。",
                    "evidence_refs": [{"kind": "document", "doc_id": doc_id}],
                }
            ],
        }
    ])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["closes"] == 0 and result["proposed"] == 0


def test_held_instrument_is_not_bought_again(conn, run, as_of, ben_universe, insert_document):
    iid = ben_universe()
    _hold(conn, run, iid)
    doc_id = insert_document()
    llm, _ = _llm([{"candidates": [_candidate(iid, doc_id)], "reviews": []}])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["candidates"] == 0
    assert "保有" in result["rejected"][0]["reason"]


# ── スロット制 ────────────────────────────────────────────────────────────────
def test_no_free_slot_skips_entry(conn, run, as_of, ben_universe, insert_document):
    """空きスロットが無ければ新規建てを出さない(黙って落とさず理由を残す)。"""
    # ポッド内集中度上限 40%(ben)より狭いスロット設定は3以上 → 3スロット全てを埋める。
    held_ids = [ben_universe(symbol=f"200{i}.T") for i in range(3)]
    target = ben_universe(symbol="2099.T")
    for iid in held_ids:
        _hold(conn, run, iid)
    doc_id = insert_document()
    llm, _ = _llm([{"candidates": [_candidate(target, doc_id)], "reviews": []}])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg(max_slots=3)
    )
    assert result["passed"] == 0
    assert "空きスロットなし" in result["skip_reasons"]


def test_ben_skips_llm_when_universe_empty(conn, run, as_of, nav_snapshot):
    """ユニバースが空なら LLM を呼ばない(無駄な高位モデル呼び出しを書かない)。"""
    nav_snapshot()
    llm, provider = _llm([{"candidates": [], "reviews": []}])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg()
    )
    assert result["universe"] == 0 and provider.calls == []


def test_slot_value_uses_mandate_capital(conn, run, as_of, ben_universe, insert_document):
    """スロット金額はマンデートの仮想資本 ÷ スロット数(LLM は関与しない)。"""
    iid = ben_universe(close=2000.0)
    doc_id = insert_document()
    llm, _ = _llm([{"candidates": [_candidate(iid, doc_id)], "reviews": []}])
    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=as_of, cfg=_cfg(max_slots=4)
    )
    # ¥2,000,000 / 4 = ¥500,000、価格 ¥2,000・単元 100株 → 2 単元 = 200 株。
    assert result["orders"][0]["qty"] == "200"
    assert Decimal(result["orders"][0]["qty"]) * Decimal(2000) <= Decimal(500_000)


def test_positions_of_other_fm_do_not_consume_ben_slots(conn, run, as_of, ben_universe):
    """他ポッドの保有は Ben のスロットを消費しない(ポッドの独立)。"""
    iid = ben_universe()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trading.positions
                (book_id, fm, instrument_id, asset_class, qty, avg_cost, run_id)
            VALUES (%s, 'jim', %s, 'equity_jp', 100, 1000, %s)
            """,
            (BOOK, iid, run.run_id),
        )
    positions = (
        PositionState(fm="jim", instrument_id=iid, asset_class="equity_jp",
                      qty=Decimal(100), avg_cost=Decimal(1000)),
    )
    from ryza.fm.sizing import slot_plan
    from ryza.ips import load_and_validate

    plan = slot_plan(load_and_validate()[1]["ben"], max_slots=5, positions=positions)
    assert plan.free_slots == 5
