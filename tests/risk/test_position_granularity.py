"""ポジション粒度是正(A-12 F-6・T-021)のテスト。

- ``guardrail_usage`` の集計意味論(グロス系は行単位 Σ|value|、issuer は銘柄単位でネット後 abs)
- ``es95`` のネットゼロ銘柄除外(両建てが weights に残り判定保留を招かないこと)
- ``load_positions`` の行単位返却(GROUP BY 廃止)と時価欠落の**銘柄単位**重複排除

判定境界は config/ips.yaml の実値に対して当てる(保護領域のリグレッション検知)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from ryza.risk.daily import load_positions
from ryza.risk.engine import (
    RiskPosition,
    es95,
    guardrail_usage,
)

_AS_OF = datetime(2030, 2, 1, 0, 0, tzinfo=UTC)


def _returns_map(instrument_id, values, *, start=date(2029, 1, 1)):
    from datetime import timedelta

    return {instrument_id: {start + timedelta(days=i): v for i, v in enumerate(values)}}


# ── guardrail_usage: 測度ごとの集計意味論 ─────────────────────────────────────
def test_guardrail_usage_gross_measures_are_row_wise_on_offsetting_pods(ips):
    """両建て(ポッド A +q / ポッド B −q): グロス系は 2|q×price|/nav、issuer は 0。

    ネット後 abs の旧実装ではグロスも issuer も 0 になり過小計上だった(A-12 F-6)。
    """
    price = Decimal(1000)
    qty = Decimal(100)
    value = qty * price  # 100_000
    positions = [
        RiskPosition(1, "equity_jp", value, fm="ben"),
        RiskPosition(1, "equity_jp", -value, fm="jim"),
    ]
    nav = Decimal(1_000_000)
    usage = guardrail_usage(positions, nav, cash=Decimal(200_000), ips=ips)
    # gross: 行単位 Σ|value| = 200_000 → 0.2x
    assert usage["gross_leverage"]["value"] == 0.2
    # 資産クラスグロスも同じ理屈(equity_jp = 200_000)
    assert usage["single_asset_class_gross"]["value"] == 0.2
    assert usage["single_asset_class_gross"]["class"] == "equity_jp"
    # issuer: 銘柄単位でネット後 abs → 0(発行体リスクとしては相殺)
    assert usage["issuer_concentration"]["value"] == 0.0


def test_guardrail_usage_partial_offset_nets_issuer_and_gross_row_wise(ips):
    """部分相殺(+100 / -40): issuer=|60×price|/nav、gross=140×price/nav。"""
    price = Decimal(1000)
    positions = [
        RiskPosition(1, "equity_jp", Decimal(100) * price, fm="ben"),   # +100_000
        RiskPosition(1, "equity_jp", Decimal(-40) * price, fm="jim"),   # -40_000
    ]
    nav = Decimal(1_000_000)
    usage = guardrail_usage(positions, nav, cash=None, ips=ips)
    assert usage["issuer_concentration"]["value"] == float(Decimal(60_000) / nav)
    assert usage["gross_leverage"]["value"] == float(Decimal(140_000) / nav)
    assert usage["single_asset_class_gross"]["value"] == float(Decimal(140_000) / nav)


def test_guardrail_usage_issuer_uses_abs_after_signed_sum_across_instruments(ips):
    """同一銘柄で符号付き合算した後に abs をとる(異銘柄は独立)。"""
    price = Decimal(1000)
    positions = [
        RiskPosition(1, "equity_jp", Decimal(100) * price, fm="ben"),   # +100_000
        RiskPosition(1, "equity_jp", Decimal(-100) * price, fm="jim"),  # -100_000 → net 0
        RiskPosition(2, "equity_us", Decimal(50) * price, fm="ben"),    # +50_000
    ]
    nav = Decimal(1_000_000)
    usage = guardrail_usage(positions, nav, cash=None, ips=ips)
    # issuer top は銘柄 2(銘柄 1 は両建てで 0)
    assert usage["issuer_concentration"]["value"] == float(Decimal(50_000) / nav)
    # gross: 100_000 + 100_000 + 50_000 = 250_000
    assert usage["gross_leverage"]["value"] == float(Decimal(250_000) / nav)


# ── es95: ネット結果ゼロの銘柄を weights から落とす ────────────────────────────
def test_es95_drops_net_zero_instrument_after_weight_aggregation():
    """両建て(+q / -q)は weights 合算後 0 になり、共通観測日の計算に混入しない。

    落とさないと `included` に残り、その銘柄の観測日で `common_days` が縛られて
    余計に判定保留(no_common_days)が出る可能性がある。行=fm×instrument 入力を
    許容する上で必須の後処理(T-021)。
    """
    positions = [
        RiskPosition(1, "equity_jp", Decimal(5_000_000), fm="ben"),
        RiskPosition(1, "equity_jp", Decimal(-5_000_000), fm="jim"),
        RiskPosition(2, "equity_us", Decimal(5_000_000), fm="ben"),
    ]
    # 銘柄 1 と 2 は日付ずれ(共通観測日ゼロ)。銘柄 1 が weights から落ちれば
    # 銘柄 2 単独で測定が成立する。
    rets = _returns_map(1, [0.0] * 30, start=date(2029, 1, 1))
    rets.update(_returns_map(2, [0.0] * 30, start=date(2029, 6, 1)))
    result = es95(positions, Decimal(10_000_000), rets, min_obs=20)
    # 銘柄 1 は weights から落ちる → 除外にも入らない(そもそも測定候補ですらない)
    assert 1 not in result.excluded
    # 銘柄 2 単独で 30 観測 → 測定成立(保留しない)
    assert result.n_obs == 30
    assert not result.deferred


def test_es95_three_row_net_zero_is_dropped_without_float_residual():
    """3行ネットゼロ(4ポッド構成で到達可能)でも weights から確実に落ちる。

    独立役員審査 2026-08-04 条件1: float 段で合算すると weights が
    0.1 + 0.2 − 0.3 = 5.55e-17 型の残差を残し、ネットゼロ銘柄が included に
    生き残って偽の判定保留(no_common_days)を招く(乱択20万試行中23.5%で再現)。
    Decimal 段で銘柄合算→float 化すること(T-021 §4)。値は nav=10M に対して
    weights がちょうど 0.1 / 0.2 / −0.3 になるよう選び、残差を決定的に再現する。
    """
    nav = Decimal(10_000_000)
    positions = [
        RiskPosition(1, "equity_jp", Decimal(1_000_000), fm="ben"),   # w=0.1
        RiskPosition(1, "equity_jp", Decimal(2_000_000), fm="jim"),   # w=0.2
        RiskPosition(1, "equity_jp", Decimal(-3_000_000), fm="pam"),  # w=-0.3
        RiskPosition(2, "equity_us", Decimal(5_000_000), fm="ben"),
    ]
    # 銘柄 1 と 2 は観測日が交わらない。銘柄 1 が weights に残ると
    # common_days が空になり no_common_days で保留してしまう。
    rets = _returns_map(1, [0.0] * 30, start=date(2029, 1, 1))
    rets.update(_returns_map(2, [0.0] * 30, start=date(2029, 6, 1)))
    result = es95(positions, nav, rets, min_obs=20)
    assert 1 not in result.excluded
    assert result.n_obs == 30
    assert not result.deferred


def test_es95_all_positions_net_to_zero_returns_flat_zero():
    """全銘柄が両建てで消えるとポジション無しと同じ(判定保留にしない)。"""
    positions = [
        RiskPosition(1, "equity_jp", Decimal(5_000_000), fm="ben"),
        RiskPosition(1, "equity_jp", Decimal(-5_000_000), fm="jim"),
    ]
    rets = _returns_map(1, [0.0] * 30)
    result = es95(positions, Decimal(10_000_000), rets, min_obs=20)
    assert result.adopted == 0.0
    assert result.n_obs == 0
    assert not result.deferred  # 「保有無し」と等価 — 保留ではない


# ── load_positions: 行単位返却+時価欠落の銘柄単位重複排除 ─────────────────────
def _seed_book(conn, book="DEMO_FUND"):
    """テスト用の帳簿と評価勘定を用意する(既存の DEMO_FUND を使う想定だが念のため)。"""
    # DEMO_FUND は seed で作成済み(migrations/0006 系)。ここは何もしない。
    return book


def _seed_instrument(conn, symbol="MULTIPOD.T", asset_class="equity") -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instruments (symbol, asset_class, venue, currency, valid_from)
            VALUES (%s, %s, 'TSE', 'JPY', now())
            RETURNING instrument_id
            """,
            (symbol, asset_class),
        )
        return cur.fetchone()[0]


def _seed_bar(conn, instrument_id, close, *, run_id, ts=None):
    ts = ts or datetime(2030, 1, 30, 6, tzinfo=UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.bars
                (instrument_id, ts, timeframe, close, source, as_of, run_id)
            VALUES (%s, %s, '1d', %s, 'test', %s, %s)
            """,
            (instrument_id, ts, Decimal(str(close)), ts, run_id),
        )


def _seed_position(conn, instrument_id, fm, qty, *, run_id,
                   book="DEMO_FUND", asset_class="equity_jp"):
    """複数 fm(PK 一部)で同じ instrument を保有できる。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trading.positions
                (book_id, fm, instrument_id, asset_class, qty, avg_cost, run_id)
            VALUES (%s, %s, %s, %s, %s, 1000, %s)
            """,
            (book, fm, instrument_id, asset_class, qty, run_id),
        )


def test_load_positions_returns_rows_per_fm_without_aggregation(conn, run_id):
    """同一銘柄をポッド A +100・ポッド B −100 で保有 → **2 行**返る(ネットしない)。"""
    _seed_book(conn)
    inst = _seed_instrument(conn)
    _seed_bar(conn, inst, 1000, run_id=run_id)
    _seed_position(conn, inst, "ben", 100, run_id=run_id)
    _seed_position(conn, inst, "jim", -100, run_id=run_id)

    positions, notes, exclusions = load_positions(conn, "DEMO_FUND", as_of=_AS_OF)
    # 対象銘柄の行を抜き出す(seed の他銘柄には触れない)
    rows = [p for p in positions if p.instrument_id == inst]
    assert len(rows) == 2  # ネットせず 2 行のまま
    fms = sorted(p.fm for p in rows)
    assert fms == ["ben", "jim"]
    # 符号もそのまま保持されている(fm × instrument の粒度で符号付き value)
    values = sorted(int(p.value) for p in rows)
    assert values == [-100_000, 100_000]
    assert notes == []
    assert exclusions == []


def test_load_positions_deduplicates_missing_price_exclusion_across_pods(conn, run_id):
    """同一銘柄を 2 ポッドが保有し時価欠落 → Exclusion / notes は 1 件だけ(T-021)。"""
    _seed_book(conn)
    inst = _seed_instrument(conn, symbol="NOPRICE_MULTI.T")
    # バーは入れない = 時価欠落
    _seed_position(conn, inst, "ben", 100, run_id=run_id)
    _seed_position(conn, inst, "jim", 50, run_id=run_id)

    positions, notes, exclusions = load_positions(conn, "DEMO_FUND", as_of=_AS_OF)
    # 対象銘柄は評価から外れる(全ポッド分)
    assert not any(p.instrument_id == inst for p in positions)
    # notes / exclusions は当該銘柄について 1 件のみ
    my_notes = [n for n in notes if f"instrument {inst}" in n]
    my_excl = [e for e in exclusions if e.instrument_id == inst]
    assert len(my_notes) == 1
    assert len(my_excl) == 1
    assert my_excl[0].measure == "valuation"
    assert my_excl[0].reason == "missing_price"


def test_load_positions_skips_zero_qty_rows(conn, run_id):
    """行内で qty=0 のポジション(消滅ポジション)は落とす(GROUP BY 廃止でも維持)。"""
    _seed_book(conn)
    inst = _seed_instrument(conn, symbol="ZEROQTY.T")
    _seed_bar(conn, inst, 1000, run_id=run_id)
    _seed_position(conn, inst, "ben", 0, run_id=run_id)  # 消滅

    positions, notes, exclusions = load_positions(conn, "DEMO_FUND", as_of=_AS_OF)
    assert not any(p.instrument_id == inst for p in positions)
    assert not any(f"instrument {inst}" in n for n in notes)
    assert not any(e.instrument_id == inst for e in exclusions)
