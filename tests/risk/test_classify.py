"""銘柄マスタ由来の決定論分類(T-015 — T-014 引き継ぎ: G-2 配線と「空 vs 未取得」)。"""

from __future__ import annotations

from decimal import Decimal

from ryza.risk.classify import (
    Classification,
    classify_current_instruments,
    classify_instrument,
    load_classification,
    upsert_classification,
)


# ── ルール分類(純ロジック)────────────────────────────────────────────────────
def test_tse_equity_classification():
    c = classify_instrument(symbol="7203.T", asset_class="equity", venue="TSE")
    assert c == Classification(
        universe_tags=("jp_equity_cash",),
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
    )


def test_us_equity_classification():
    c = classify_instrument(symbol="AAPL", asset_class="equity", venue="NASDAQ")
    assert c is not None
    assert c.universe_tags == ("us_equity_cash",)
    assert c.is_single_name is True and c.unit_size is None


def test_fx_classification():
    c = classify_instrument(symbol="USD/JPY", asset_class="fx", venue="SAXO")
    assert c is not None
    assert c.universe_tags == ("fx",) and c.product == "exchange_fx"
    assert c.is_single_name is False


def test_etf_and_futures_not_rule_classified():
    """ETF はレバ/インバース該当をマスタから否定できない — フラグなし主張は fail-open の
    ため None(curated 供給のみ)。先物は指数/金利/商品の別がマスタに無い。"""
    assert classify_instrument(symbol="1570.T", asset_class="etf", venue="TSE") is None
    assert classify_instrument(symbol="NK225M", asset_class="future", venue="OSE") is None
    assert classify_instrument(symbol="BTC-PERP", asset_class="crypto", venue="DERIBIT") is None


def test_unknown_venue_equity_unclassified():
    assert classify_instrument(symbol="X", asset_class="equity", venue="LSE") is None


# ── DB: 空 vs 未取得の区別 ────────────────────────────────────────────────────
def _insert_instrument(conn, *, symbol, asset_class, venue):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instruments (symbol, asset_class, venue, currency, valid_from)
            VALUES (%s, %s, %s, 'JPY', now())
            RETURNING instrument_id
            """,
            (symbol, asset_class, venue),
        )
        return cur.fetchone()[0]


def test_no_row_means_unfetched(conn):
    """行なし=未取得 → None(呼び出し側はタグ空で提案を組み、G-2 が fail-closed block)。"""
    assert load_classification(conn, 999_999_999) is None


def test_empty_arrays_mean_fetched_none_applicable(conn, run_id):
    """行あり・配列空=「取得済み該当なし」— 未取得(None)と区別できる(審査条件7)。"""
    inst = _insert_instrument(conn, symbol="X1", asset_class="equity", venue="TSE")
    curated = Classification(
        universe_tags=(),
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
    )
    upsert_classification(conn, inst, curated, run_id=run_id, source="curated")
    loaded = load_classification(conn, inst)
    assert loaded is not None
    assert loaded.universe_tags == () and loaded.instrument_flags == ()


def test_classify_current_instruments_sweep(conn, run_id):
    tse = _insert_instrument(conn, symbol="7203.T", asset_class="equity", venue="TSE")
    fut = _insert_instrument(conn, symbol="NK225M", asset_class="future", venue="OSE")
    counts = classify_current_instruments(conn, run_id=run_id)
    assert counts["classified"] >= 1
    assert counts["unclassifiable"] >= 1
    assert load_classification(conn, tse) is not None
    assert load_classification(conn, fut) is None  # 未分類のまま(curated 待ち)
    # 再実行: 分類済みは対象外(冪等)、未分類は未分類のまま数え直される。
    counts2 = classify_current_instruments(conn, run_id=run_id)
    assert counts2["classified"] == 0
    assert counts2["already"] >= 1


def test_sweep_does_not_overwrite_curated(conn, run_id):
    inst = _insert_instrument(conn, symbol="9984.T", asset_class="equity", venue="TSE")
    curated = Classification(
        universe_tags=("jp_equity_cash", "liquid_equity"),
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
    )
    upsert_classification(conn, inst, curated, run_id=run_id, source="curated")
    classify_current_instruments(conn, run_id=run_id)
    loaded = load_classification(conn, inst)
    assert loaded is not None and "liquid_equity" in loaded.universe_tags
