"""リプレイモード(過去 as_of での一巡動作)のテスト(T-017 受け入れ基準)。

J-Quants Light 未加入の間、FM は「12週前 as_of の過去リプレイ」で検証する(指示書の
データ前提)。**as_of を全経路で一貫させれば point-in-time 原則は満たされる**ことを、
「as_of より後のデータを DB に置いた状態で一巡させ、結果が as_of 以前のデータだけで
決まる」ことによって確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ryza.fm import ben, jim
from ryza.fm.config import BenConfig
from ryza.research.llm import FixtureProvider, StructuredLLM
from ryza.risk.classify import history_coverage_since

BOOK = "DEMO_FUND"
MODEL = "test-mid"

# 12週前(指示書のリプレイ既定)。
REPLAY_DELTA = timedelta(weeks=12)


@pytest.fixture
def replay_as_of() -> datetime:
    return datetime.now(UTC) - REPLAY_DELTA


def _assert_e6_disclosure(conn, result: dict, as_of: datetime) -> None:
    """リプレイ結果は E6(point-in-time ユニバース)の充足状況を必ず持つ(審査 C-4)。

    本テストの分類は**当時に記録された**(``recorded_at`` を as_of に合わせた)ため、
    履歴は as_of をカバーしており但し書きは外れる。CI では migration が毎回新規適用
    されるため「カバー済みリプレイ」の経路が一度も通らない、という審査 C-21 の指摘に
    対応して、ここは分岐で吸収せず ``e6_covered=True`` を固定する(未カバー側は
    tests/fm/test_universe_pit.py が固定する)。
    """
    status = result["pit_universe"]
    since = history_coverage_since(conn)
    assert since is not None and as_of >= since
    assert status["replay"] is True and status["source"] == "history"
    assert status["e6_covered"] is True
    assert status["note"] is None


def _ben_cfg(**overrides) -> BenConfig:
    defaults = dict(
        version="test", producer="test.ben", model_tier="mid", weekday=1,
        max_slots=5, max_candidates=2, max_documents=10, doc_body_chars=500,
        recent_theses=5,
    )
    defaults.update(overrides)
    return BenConfig(**defaults)


def test_jim_replay_cycle(
    conn, run, replay_as_of, instrument, classify, insert_bars, nav_snapshot,
    backdated_capital,
):
    """12週前 as_of で一巡: シグナル → thesis → ゲート pass → orders。

    as_of より後の「暴落バー」を DB に置いても結果は変わらない(未来を見ない)。
    """
    day = replay_as_of.date()
    backdated_capital(day - timedelta(days=2))
    nav_snapshot(day=day - timedelta(days=1))
    iid = instrument(symbol="1401.T")
    classify(iid, universe_tags=("liquid_equity",), as_of=replay_as_of - timedelta(days=1))
    # as_of 以前: 60日横ばい → 末日にゴールデンクロス。
    insert_bars(
        iid, [1000] * 60 + [1600],
        volumes=[100_000] * 60 + [500_000],
        last_day=day - timedelta(days=1),
    )
    # as_of より後: 暴落(リプレイでは見えてはならない)。
    insert_bars(
        iid, [200] * 5, volumes=[100_000] * 5,
        last_day=datetime.now(UTC).date(),
    )

    result = jim.run_jim(conn, run, book_id=BOOK, as_of=replay_as_of)
    _assert_e6_disclosure(conn, result, replay_as_of)
    assert result["universe"] == 1 and result["entries"] == 1
    assert result["passed"] == 1 and result["blocked"] == 0
    order = result["orders"][0]
    # 判定価格は as_of 以前の最新終値 ¥1,600(暴落後の ¥200 ではない)。
    assert order["qty"] == "100"
    with conn.cursor() as cur:
        cur.execute("SELECT ref_price FROM trading.orders WHERE id = %s", (order["order_id"],))
        assert Decimal(cur.fetchone()[0]) == Decimal(1600)
        cur.execute(
            "SELECT as_of FROM trading.fm_theses WHERE thesis_id = %s", (order["thesis_id"],)
        )
        assert cur.fetchone()[0] == replay_as_of


def test_ben_replay_cycle(
    conn, run, replay_as_of, instrument, classify, insert_bars, nav_snapshot,
    insert_document, backdated_capital,
):
    """Ben も同じ as_of で一巡する。プロンプトに未来の文書が載らない。"""
    day = replay_as_of.date()
    backdated_capital(day - timedelta(days=2))
    nav_snapshot(day=day - timedelta(days=1))
    iid = instrument(symbol="7203.T")
    classify(iid, universe_tags=("jp_equity_cash",), as_of=replay_as_of - timedelta(days=1))
    insert_bars(iid, [1000] * 3, volumes=[100_000] * 3, last_day=day - timedelta(days=1))

    past_doc = insert_document(title="過去の開示", as_of=replay_as_of - timedelta(days=2))
    insert_document(title="未来のニュース", as_of=datetime.now(UTC))

    provider = FixtureProvider([
        {
            "candidates": [
                {
                    "instrument_id": iid,
                    "direction": "buy",
                    "thesis_md": "PBR 0.6・自己資本比率 60%。安全域がある。",
                    "evidence_refs": [{"kind": "document", "doc_id": past_doc}],
                    "invalidation_md": "営業利益率が2四半期連続で 8% を下回ったら降りる。",
                }
            ],
            "reviews": [],
        }
    ])
    llm = StructuredLLM(provider, None, dept_tag="fm.ben")

    result = ben.run_ben(
        conn, run, llm, model=MODEL, book_id=BOOK, as_of=replay_as_of, cfg=_ben_cfg()
    )
    _assert_e6_disclosure(conn, result, replay_as_of)
    assert result["candidates"] == 1 and result["passed"] == 1
    prompt = provider.calls[0]["user"]
    assert "過去の開示" in prompt and "未来のニュース" not in prompt
