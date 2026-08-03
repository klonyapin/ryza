"""銘柄マスタ由来の決定論分類(T-015 — T-014 引き継ぎ: G-2 配線と「空 vs 未取得」)。

後半は **point-in-time 履歴化**(0026・独立役員審査 T-017 C-4 の是正)の回帰:
分類の変更が過去に漏れないこと(look-ahead 排除)・履歴が追記オンリーであること・
履歴がカバーしていない as_of には E6 未達の但し書きが付くこと。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from ryza.risk.classify import (
    Classification,
    classification_pit_status,
    classify_current_instruments,
    classify_instrument,
    history_coverage_since,
    load_classification,
    load_classification_at,
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


# ── point-in-time 履歴(0026 — 審査 C-4)──────────────────────────────────────
def _tagged(*tags: str) -> Classification:
    return Classification(
        universe_tags=tags,
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
    )


def _history(conn, instrument_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT universe_tags, as_of, source, backfilled
            FROM market.instrument_classification_history
            WHERE instrument_id = %s ORDER BY history_id
            """,
            (instrument_id,),
        )
        return cur.fetchall()


def _expect_rejected(conn, sql: str, error: type[Exception]) -> None:
    """SQL が拒否されることを確認する(SAVEPOINT でトランザクションを守る)。"""
    with pytest.raises(error), conn.transaction():
        with conn.cursor() as cur:
            cur.execute(sql)


def test_upsert_writes_history_and_current(conn, run_id):
    """分類確定は履歴への追記と現在値の更新を同時に行う(同一トランザクション)。"""
    inst = _insert_instrument(conn, symbol="H1.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=30)
    upsert_classification(conn, inst, _tagged("jp_equity_cash"), run_id=run_id, as_of=t1)

    rows = _history(conn, inst)
    assert len(rows) == 1
    assert rows[0][0] == ["jp_equity_cash"] and rows[0][1] == t1
    assert rows[0][3] is False  # バックフィル行ではない(実時刻の記録)
    assert load_classification(conn, inst) == _tagged("jp_equity_cash")


def test_history_appends_only_on_change(conn, run_id):
    """同内容の再分類では履歴を増やさない(日次スイープの再実行で膨らませない)。"""
    inst = _insert_instrument(conn, symbol="H2.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=30)
    same = _tagged("jp_equity_cash")
    upsert_classification(conn, inst, same, run_id=run_id, as_of=t1)
    upsert_classification(conn, inst, same, run_id=run_id, as_of=t1 + timedelta(days=1))
    assert len(_history(conn, inst)) == 1

    # 内容が変われば追記される(上書きではない)。
    upsert_classification(
        conn, inst, _tagged("jp_equity_cash", "liquid_equity"),
        run_id=run_id, as_of=t1 + timedelta(days=2),
    )
    rows = _history(conn, inst)
    assert len(rows) == 2
    assert rows[0][0] == ["jp_equity_cash"]
    assert rows[1][0] == ["jp_equity_cash", "liquid_equity"]


def test_classification_at_past_as_of_ignores_later_change(conn, run_id):
    """**look-ahead 排除**: 過去 as_of は変更前の分類を見る(不変原則4)。"""
    inst = _insert_instrument(conn, symbol="H3.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=30)
    t2 = datetime.now(UTC) - timedelta(days=10)
    upsert_classification(conn, inst, _tagged("jp_equity_cash"), run_id=run_id, as_of=t1)
    upsert_classification(
        conn, inst, _tagged("jp_equity_cash", "liquid_equity"), run_id=run_id, as_of=t2
    )

    before = load_classification_at(conn, inst, as_of=t1 - timedelta(days=1))
    assert before is None  # まだ分類されていない時点(= 未分類 → ゲートが block)

    middle = load_classification_at(conn, inst, as_of=t2 - timedelta(days=1))
    assert middle is not None and middle.universe_tags == ("jp_equity_cash",)

    after = load_classification_at(conn, inst, as_of=datetime.now(UTC))
    assert after is not None and "liquid_equity" in after.universe_tags


def test_history_is_append_only(conn, run_id):
    """履歴は UPDATE・DELETE・TRUNCATE のいずれも拒む(0015/0018/0023 と同基準)。"""
    inst = _insert_instrument(conn, symbol="H4.T", asset_class="equity", venue="TSE")
    upsert_classification(conn, inst, _tagged("jp_equity_cash"), run_id=run_id)
    table = "market.instrument_classification_history"
    _expect_rejected(
        conn,
        f"UPDATE {table} SET universe_tags = '{{}}' WHERE instrument_id = {inst}",  # noqa: S608
        psycopg.errors.RaiseException,
    )
    _expect_rejected(
        conn,
        f"DELETE FROM {table} WHERE instrument_id = {inst}",  # noqa: S608
        psycopg.errors.RaiseException,
    )
    _expect_rejected(conn, f"TRUNCATE {table}", psycopg.errors.RaiseException)
    assert len(_history(conn, inst)) == 1


def test_pit_status_uncovered_before_history_starts(conn):
    """履歴の記録開始より前の as_of は E6 未達のまま(移行前を達成と偽らない)。"""
    since = history_coverage_since(conn)
    assert since is not None, "0026 が未適用(または migration のファイル名が変わった)"

    covered = classification_pit_status(conn, as_of=since + timedelta(seconds=1))
    assert covered["covered"] is True and covered["note"] is None

    uncovered = classification_pit_status(conn, as_of=since - timedelta(days=1))
    assert uncovered["covered"] is False
    assert "E6" in uncovered["note"] and since.isoformat() in uncovered["note"]
