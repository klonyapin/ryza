"""日次締め(closing.py)と財務諸表(statements.py)の統合検証。

受け入れ基準:
- フィクスチャで 記帳→締め→試算表がゼロバランス、BS 資産=負債+資本、NAV=資産-負債
- 照合一致で NAV confirmed、意図的に壊した snapshot で break_open + provisional のまま
- 未記帳の約定の検出(冪等: 記帳済み fill はスキップ)
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from ryza.ledger import _util, closing, posting, statements

D = Decimal
DAY = date(2026, 8, 3)


def _setup_two_positions(conn, run_id):
    """2 銘柄を買い建てる。1001: 100@500、1002: 50@1000(手数料100)。"""
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=100, price=500, entry_date=DAY, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1002, side="buy",
                      qty=50, price=1000, fee=100, entry_date=DAY, run_id=run_id)


def test_close_zero_balance_and_bs_identity(conn, run_id):
    _setup_two_positions(conn, run_id)
    price_source = {1001: 600, 1002: 900}  # 1001 値上がり、1002 値下がり
    snapshot = {
        "positions": {1001: 100, 1002: 50},
        "valuation": {1001: 60000, 1002: 45000},  # ours 帳簿価額と一致
    }
    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source=price_source,
        run_id=run_id, broker_snapshot=snapshot,
    )

    # 試算表がゼロバランス
    tb = statements.trial_balance(conn, "DEMO_FUND", DAY)
    total = tb[tb["account_id"] == "_TOTAL"].iloc[0]
    assert total["balance"] == D(0)
    assert total["debit"] == total["credit"]

    # BS 恒等式: 資産 = 負債 + 資本 + 当期純損益、NAV = 資産 - 負債
    t = statements.book_totals(conn, "DEMO_FUND", DAY)
    assert t["assets"] == t["liabilities"] + t["equity"] + t["net_income"]
    assert t["nav"] == t["assets"] - t["liabilities"]

    # 具体値: 未実現 = +10000(1001) -5000(1002) = 5000、手数料 100
    # NAV = 出資 10,000,000(0006+0011)+ 4900
    assert t["net_income"] == D(4900)
    assert t["nav"] == D(10004900)
    assert result["nav"] == D(10004900)

    # 照合一致 → confirmed
    assert result["status"] == "confirmed"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, nav FROM ledger.nav_snapshots WHERE book_id='DEMO_FUND' "
            "AND snap_date=%s", (DAY,),
        )
        status, nav = cur.fetchone()
    assert status == "confirmed"
    assert nav == D(10004900)


def test_close_broken_snapshot_stays_provisional(conn, run_id):
    _setup_two_positions(conn, run_id)
    price_source = {1001: 600, 1002: 900}
    broken = {
        "positions": {1001: 99, 1002: 50},  # 1001 の数量が不一致
        "valuation": {1001: 60000, 1002: 45000},
    }
    breaks_seen = []
    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source=price_source,
        run_id=run_id, broker_snapshot=broken, on_break=breaks_seen.append,
    )

    assert result["status"] == "provisional"
    assert not result["recon"].all_matched
    assert len(breaks_seen) == 1
    assert breaks_seen[0]["item"] == "position:1001"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM ledger.nav_snapshots WHERE book_id='DEMO_FUND' "
            "AND snap_date=%s", (DAY,),
        )
        assert cur.fetchone()[0] == "provisional"
        cur.execute(
            "SELECT count(*) FROM ledger.reconciliations WHERE status='break_open' "
            "AND book_id='DEMO_FUND'",
        )
        assert cur.fetchone()[0] == 1


def test_close_idempotent_fill_detection(conn, run_id):
    """trade.fills を仕込み、run_daily_close が未記帳の約定を1回だけ記帳することを確認。"""
    # trade チェーンを最小構成で作る。
    with conn.cursor() as cur:
        ev = _mk_evidence(cur)
        cur.execute(
            """INSERT INTO trade.signals
               (strategy_id, strategy_ver, instrument_id, direction, rationale_refs, ts, run_id)
               VALUES ('s','v1', 2001, 'long', '{}'::jsonb, %s, %s) RETURNING signal_id""",
            (datetime(2026, 8, 3, tzinfo=UTC), run_id),
        )
        signal_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO trade.order_intents
               (track, signal_ids, instrument_id, side, qty, order_type, sizing_calc,
                risk_snapshot, gate_verdict, gate_detail, ts, run_id)
               VALUES ('demo', %s, 2001, 'buy', 20, 'market', '{}'::jsonb, '{}'::jsonb,
                       'pass', '{}'::jsonb, %s, %s) RETURNING intent_id""",
            ([signal_id], datetime(2026, 8, 3, tzinfo=UTC), run_id),
        )
        intent_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO trade.orders
               (intent_id, track, broker, state, state_history, ts)
               VALUES (%s, 'demo', 'sim', 'filled', '[]'::jsonb, %s) RETURNING order_id""",
            (intent_id, datetime(2026, 8, 3, tzinfo=UTC)),
        )
        order_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO trade.fills (order_id, qty, price, fee, filled_at, evidence_id)
               VALUES (%s, 20, 300, 0, %s, %s) RETURNING fill_id""",
            (order_id, datetime(2026, 8, 3, tzinfo=UTC), ev),
        )

    price_source = {2001: 300}
    r1 = closing.run_daily_close(conn, book_id="DEMO_FUND", date=DAY,
                                 price_source=price_source, run_id=run_id)
    assert len(r1["fills_recorded"]) == 1  # 1 件記帳

    r2 = closing.run_daily_close(conn, book_id="DEMO_FUND", date=DAY,
                                 price_source=price_source, run_id=run_id)
    assert len(r2["fills_recorded"]) == 0  # 冪等: 既記帳はスキップ


# ── 再締め(独立審査 重要-2)と nav_snapshots のリネージ ───────────────────────
def _post_contribution(conn, run_id, day: date, amount: Decimal) -> int:
    """指定日付で出資を記帳する(拠出資本 = navflow が外部フローとして拾う勘定)。"""
    return posting.post_entry(
        conn,
        book_id="DEMO_FUND",
        entry_date=day,
        description="テスト用の出資",
        lines=[
            {"account_id": "cash", "debit": amount, "currency": "JPY"},
            {"account_id": "capital", "credit": amount, "currency": "JPY"},
        ],
        evidence={"kind": "decision", "payload": {"test": "reclose"}, "source": "test"},
        run_id=run_id,
        posted_by="test.ledger",
    )


def _snapshot(conn, day: date) -> tuple[Decimal, str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT nav, status, detail FROM ledger.nav_snapshots "
            "WHERE book_id = 'DEMO_FUND' AND snap_date = %s",
            (day,),
        )
        return cur.fetchone()


def _reclose(conn, run_id, through: date, price_source=closing.no_price):
    """既定は明示の縮退ソース(``no_price``)— 建玉を持たないシナリオ用。

    ``price_source`` は必須引数なので「渡し忘れ」は型で落ちる(独立審査 新-8)。
    評価替えを検証するテストは実際の終値を返すソースを明示的に渡すこと。
    """
    return closing.reclose_stale(
        conn, book_id="DEMO_FUND", through=through, run_id=run_id,
        price_source=price_source,
    )


def test_reclose_absorbs_entry_posted_after_close(conn, run_id):
    """締めの後に同日付で立った仕訳を、次の締めの再締めが NAV に取り込む(重要-2)。"""
    first = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    assert first["nav"] == D(10_000_000)  # 0006+0011 の出資のみ

    _post_contribution(conn, run_id, DAY, D(5_000_000))  # ← 締めの後に立った仕訳
    assert _snapshot(conn, DAY)[0] == D(10_000_000)  # 締め直後は古い NAV のまま

    changed = _reclose(conn, run_id, DAY + timedelta(days=1))
    assert [c["date"] for c in changed] == [DAY]
    assert (changed[0]["nav_before"], changed[0]["nav_after"]) == (
        D(10_000_000), D(15_000_000)
    )
    assert changed[0]["restated"] is True and changed[0]["late_entries"] is True

    nav, status, detail = _snapshot(conn, DAY)
    assert nav == D(15_000_000)
    assert status == "provisional"  # status = 締め時点の照合の結論(据え置き)
    assert D(detail["reclose"][0]["nav_before"]) == D(10_000_000)


def test_reclose_detects_by_watermark_not_by_recency(conn, run_id):
    """検出は水位で行う — どれだけ古い日でも遅延仕訳があれば対象、無ければ対象外。"""
    days = [DAY + timedelta(days=i) for i in range(4)]
    for d in days:
        closing.run_daily_close(
            conn, book_id="DEMO_FUND", date=d, price_source={}, run_id=run_id
        )
    _post_contribution(conn, run_id, days[0], D(5_000_000))  # 最古の日に遅れて記帳

    changed = _reclose(conn, run_id, days[-1])
    # 遅延仕訳の日以降すべてが stale(固定窓なら最古が落ちる — 再審査 再-1)。
    assert [c["date"] for c in changed] == days
    assert all(c["restated"] for c in changed)
    # 直後にもう一度走らせても、水位が最新なので何も拾わない(冪等)。
    assert _reclose(conn, run_id, days[-1]) == []


def test_reclose_counts_age_in_business_days_for_urgency(conn, run_id):
    """restatement の古さはスナップショットの実績数で数え、しきい値超えを urgent にする。"""
    days = [DAY + timedelta(days=i) for i in range(8)]
    for d in days:
        closing.run_daily_close(
            conn, book_id="DEMO_FUND", date=d, price_source={}, run_id=run_id
        )
    _post_contribution(conn, run_id, days[0], D(5_000_000))

    changed = _reclose(conn, run_id, days[-1])
    ages = {c["date"]: c["age_business_days"] for c in changed}
    assert ages[days[0]] == 7 and ages[days[-1]] == 0  # 最古 = 締め 7 回前

    urgent = closing.urgent_restatements(changed)
    threshold = closing.RESTATEMENT_URGENT_BUSINESS_DAYS
    assert [u["date"] for u in urgent] == [
        d for d in days if ages[d] > threshold
    ]
    assert len(urgent) == 2  # 締め 7 回前・6 回前(しきい値 5 営業日)


def test_reclose_chains_producer_and_reclose_history(conn, run_id):
    """2 回以上の訂正で最初の書き手・最初の nav_before が消えない(再審査 再-8)。"""
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    _post_contribution(conn, run_id, DAY, D(5_000_000))
    _reclose(conn, run_id, DAY + timedelta(days=1))
    _post_contribution(conn, run_id, DAY, D(1_000_000))
    _reclose(conn, run_id, DAY + timedelta(days=1))

    detail = _snapshot(conn, DAY)[2]
    assert [D(r["nav_before"]) for r in detail["reclose"]] == [
        D(10_000_000), D(15_000_000)
    ]
    assert [p["job"] for p in detail["producer_history"]] == [
        "ledger.closing.run_daily_close", "ledger.closing.reclose_stale"
    ]
    assert detail["producer"]["job"] == "ledger.closing.reclose_stale"


def test_reclose_backfills_lineage_without_claiming_restatement(conn, run_id):
    """水位を持たない旧スナップショットは、値が同じなら訂正扱いにしない。"""
    with conn.cursor() as cur:  # 本機能より前に書かれた行を模す
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES ('DEMO_FUND', %s, 10000000, 'confirmed', '{}'::jsonb)
            """,
            (DAY,),
        )
    changed = _reclose(conn, run_id, DAY)
    assert [c["date"] for c in changed] == [DAY]
    assert changed[0]["restated"] is False and changed[0]["late_entries"] is False

    detail = _snapshot(conn, DAY)[2]
    assert "restated" not in detail  # 起きていない訂正を主張しない
    assert detail["producer"]["input_refs"][closing.WATERMARK_KEY] is not None
    assert _reclose(conn, run_id, DAY) == []  # 水位が埋まったので以後は静か


def test_nav_snapshot_records_producer_lineage(conn, run_id):
    """nav_snapshots.detail.producer に producer_job/code_version/as_of/input_refs(不変原則3)。"""
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    producer = _snapshot(conn, DAY)[2]["producer"]
    assert producer["job"] == "ledger.closing.run_daily_close"
    assert producer["run_id"] == run_id
    assert producer["as_of"] == DAY.isoformat()
    assert producer["input_refs"][closing.WATERMARK_KEY] is not None
    with conn.cursor() as cur:
        cur.execute("SELECT code_version FROM meta.runs WHERE run_id = %s", (run_id,))
        assert producer["code_version"] == cur.fetchone()[0]

    # 再締めで書き換えた値は水位が進んでいる = 後から立った仕訳を見た値だと辿れる。
    _post_contribution(conn, run_id, DAY, D(5_000_000))
    _reclose(conn, run_id, DAY + timedelta(days=1))
    after = _snapshot(conn, DAY)[2]["producer"]
    assert after["job"] == "ledger.closing.reclose_stale"
    assert after["input_refs"][closing.WATERMARK_KEY] > producer["input_refs"][
        closing.WATERMARK_KEY
    ]


# ── as_of リプレイと過去日の MTM 再適用(独立審査 新-3)────────────────────────
def _max_entry_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(entry_id), 0) FROM ledger.journal_entries")
        return cur.fetchone()[0]


def _late_fill_scenario(conn, run_id) -> tuple[date, date]:
    """審査 新-3 の実測ケースを再現する。

    d0 を締めた**後**に d0 付けで 1000 株@1000 が記帳される(遅延約定)。市場の終値は
    両日とも 1200 なので、d0 の建玉を原価のまま残すと d0 の NAV だけが 200,000 低く、
    翌日 d1 の締めが時価に打ち直した瞬間に +2% の偽リターンが立つ(真値は 0%)。
    """
    d0, d1 = DAY, DAY + timedelta(days=1)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d0, price_source={}, run_id=run_id
    )
    posting.post_fill(
        conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
        qty=1000, price=1000, entry_date=d0, run_id=run_id,
    )
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={1001: 1200}, run_id=run_id
    )
    return d0, d1


def _close_1200(instrument_id: int, day: date) -> Decimal:
    return D(1200)


def test_reclose_leaves_the_false_return_when_the_days_bar_is_missing(conn, run_id):
    """(対照)**当日バー欠測**で再適用できない日は原価のまま = 審査実測の +2% を再現する。

    固定するのは「価格ソースを渡し忘れた経路」ではなく「その日のバーが無い」現実的な
    縮退である(独立審査 新-8: 渡し忘れ経路を対照に使うとそれを仕様として追認する)。
    """
    d0, d1 = _late_fill_scenario(conn, run_id)
    changed = _reclose(conn, run_id, d1, price_source=closing.no_price)

    item = next(c for c in changed if c["date"] == d0)
    assert item["recon_invalidated"] is True and item["mtm_reapplied"] is False
    assert item["mtm_pending"] is True and item["mtm_carried_forward"] is False
    nav_d0, nav_d1 = _snapshot(conn, d0)[0], _snapshot(conn, d1)[0]
    assert (nav_d0, nav_d1) == (D(10_000_000), D(10_200_000))
    assert nav_d1 / nav_d0 - 1 == D("0.02")  # 恒久的な偽リターン
    assert _snapshot(conn, d0)[2]["mtm_not_reapplied"] is True


def test_reclose_reapplies_mtm_with_as_of_positions(conn, run_id):
    """遅延約定日の建玉を as_of リプレイで復元し、その日の終値で評価替えする(新-3)。"""
    d0, d1 = _late_fill_scenario(conn, run_id)
    before = _max_entry_id(conn)

    changed = closing.reclose_stale(
        conn, book_id="DEMO_FUND", through=d1, run_id=run_id,
        price_source=_close_1200,
    )
    item = next(c for c in changed if c["date"] == d0)
    assert item["mtm_reapplied"] is True and item["restated"] is True

    nav_d0, nav_d1 = _snapshot(conn, d0)[0], _snapshot(conn, d1)[0]
    assert (nav_d0, nav_d1) == (D(10_200_000), D(10_200_000))
    assert nav_d1 / nav_d0 - 1 == D(0)  # 偽リターンが消える

    detail = _snapshot(conn, d0)[2]
    assert detail["mtm_not_reapplied"] is False
    assert detail["mtm_reapplied"]["delta"] == "200000"
    assert detail["mtm_reapplied"]["priced_at"] == d0.isoformat()
    assert detail["mtm_reapplied"]["positions"]["1001"] == {
        "qty": "1000", "price": "1200",
        "market_value": "1200000", "book_value": "1000000",
    }
    assert D(detail["assets"]) == D(10_200_000)  # 集計値も評価替え後で揃う
    # 仕訳集計そのままの NAV を残し、nav = nav_from_journals + delta を検証可能にする(新-9)。
    assert D(detail["nav_from_journals"]) == D(10_000_000)
    assert D(detail["nav_from_journals"]) + D(detail["mtm_reapplied"]["delta"]) == nav_d0
    assert D(detail["nav_from_journals"]) == statements.book_totals(
        conn, "DEMO_FUND", d0
    )["nav"]

    # 仕訳は 1 本も書かない(過去日付への新規記帳の経路は作らない)。
    assert _max_entry_id(conn) == before
    # 冪等: 水位も NAV も動かないので次の再締めは同じ日を拾わない。
    assert closing.reclose_stale(
        conn, book_id="DEMO_FUND", through=d1, run_id=run_id, price_source=_close_1200
    ) == []


def test_reclose_keeps_reapplied_mtm_on_a_later_reclose(conn, run_id):
    """2 回目の再締めが前回の評価替えを取りこぼさない(再適用は仕訳を残さないため)。

    同じ日に今度は**建玉を動かさない**仕訳(出資)が遅れて立つ。今回の遅延仕訳だけを見て
    「建玉は動いていない」と判断して集計だけをやり直すと、前回の評価替えが NAV から
    消えて偽リターンが復活する。
    """
    d0, d1 = _late_fill_scenario(conn, run_id)
    closing.reclose_stale(
        conn, book_id="DEMO_FUND", through=d1, run_id=run_id, price_source=_close_1200
    )
    assert _snapshot(conn, d0)[0] == D(10_200_000)

    _post_contribution(conn, run_id, d0, D(1_000_000))  # 建玉を動かさない遅延仕訳
    changed = closing.reclose_stale(
        conn, book_id="DEMO_FUND", through=d1, run_id=run_id, price_source=_close_1200
    )
    item = next(c for c in changed if c["date"] == d0)
    # 戻り値の recon_invalidated は「今回の遅延仕訳が建玉を動かしたか」なので False。
    # detail 側のフラグ(一度立ったら下ろさない)が再適用の根拠になる。
    assert item["recon_invalidated"] is False
    assert _snapshot(conn, d0)[2]["recon_invalidated"] is True
    assert item["mtm_reapplied"] is True
    assert _snapshot(conn, d0)[0] == D(11_200_000)  # 原価へ戻らない


def test_reclose_carries_forward_mtm_when_the_bar_disappears(conn, run_id):
    """一度再適用した日は、後の再締めで価格を引けなくても**原価へ戻さない**(新-7)。

    再適用は仕訳を残さないので、引き継がずに集計だけをやり直すと NAV が取得原価へ
    revert し、一度消した偽リターン(+2%)が復活する(審査実測)。
    """
    d0, d1 = _late_fill_scenario(conn, run_id)
    closing.reclose_stale(
        conn, book_id="DEMO_FUND", through=d1, run_id=run_id, price_source=_close_1200
    )
    assert _snapshot(conn, d0)[0] == D(10_200_000)

    _post_contribution(conn, run_id, d0, D(1_000_000))  # 再締めを起こす遅延仕訳
    changed = closing.reclose_stale(  # ← 今回はその日のバーが引けない
        conn, book_id="DEMO_FUND", through=d1, run_id=run_id,
        price_source=closing.no_price,
    )
    item = next(c for c in changed if c["date"] == d0)
    assert item["mtm_carried_forward"] is True
    assert item["mtm_reapplied"] is False and item["mtm_pending"] is False
    assert _snapshot(conn, d0)[0] == D(11_200_000)  # 原価(11,000,000)へ戻らない

    detail = _snapshot(conn, d0)[2]
    assert detail["mtm_not_reapplied"] is False
    assert detail["mtm_reapplied"]["carried_forward"] is True
    assert detail["mtm_reapplied"]["delta"] == "200000"
    assert D(detail["nav_from_journals"]) == D(11_000_000)


def test_reclose_stays_at_cost_when_nothing_was_ever_reapplied(conn, run_id):
    """引き継ぐ値も無い日は取得原価のまま(縮退の下限 — 部分適用しない)。"""
    d0, d1 = _late_fill_scenario(conn, run_id)

    changed = _reclose(conn, run_id, d1, price_source=closing.no_price)
    item = next(c for c in changed if c["date"] == d0)
    assert (item["mtm_reapplied"], item["mtm_carried_forward"], item["mtm_pending"]) == (
        False, False, True
    )
    assert _snapshot(conn, d0)[0] == D(10_000_000)
    detail = _snapshot(conn, d0)[2]
    assert detail["mtm_not_reapplied"] is True and "mtm_reapplied" not in detail


def test_reclose_leaves_capital_only_days_to_the_aggregate(conn, run_id):
    """建玉が動いていない日は評価替えを打ち直さない(対象は recon_invalidated と同集合)。"""
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    _post_contribution(conn, run_id, DAY, D(5_000_000))

    changed = closing.reclose_stale(
        conn, book_id="DEMO_FUND", through=DAY + timedelta(days=1), run_id=run_id,
        price_source=_close_1200,
    )
    assert changed[0]["recon_invalidated"] is False
    assert changed[0]["mtm_reapplied"] is False
    assert _snapshot(conn, DAY)[0] == D(15_000_000)


def test_replay_position_as_of_bounds_by_entry_date(conn, run_id):
    """as_of は当日の仕訳を含み翌日の仕訳を除く。既定(None)は従来どおり全期間。"""
    d0, d1 = DAY, DAY + timedelta(days=1)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=100, price=500, entry_date=d0, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=200, price=600, entry_date=d1, run_id=run_id)

    replay = _util.replay_position
    assert replay(conn, "DEMO_FUND", 1001, as_of=d0 - timedelta(days=1)) == (D(0), D(0))
    assert replay(conn, "DEMO_FUND", 1001, as_of=d0) == (D(100), D(50_000))
    assert replay(conn, "DEMO_FUND", 1001, as_of=d1) == (D(300), D(170_000))
    assert replay(conn, "DEMO_FUND", 1001) == (D(300), D(170_000))  # 既存呼び出しの互換

    # 売りの取り崩し(移動平均法)も as_of で切れる。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="sell",
                      qty=150, price=700, entry_date=d1 + timedelta(days=1),
                      run_id=run_id)
    assert replay(conn, "DEMO_FUND", 1001, as_of=d1) == (D(300), D(170_000))
    assert replay(conn, "DEMO_FUND", 1001)[0] == D(150)


def test_replay_position_as_of_cuts_reversals_on_the_same_boundary(conn, run_id):
    """逆仕訳も as_of で切る — securities_book_value と日付境界を揃える(差分計算の整合)。"""
    d0, d1 = DAY, DAY + timedelta(days=1)
    entry_id = posting.post_fill(
        conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
        qty=100, price=500, entry_date=d0, run_id=run_id,
    )
    posting.reverse_entry(
        conn, entry_id=entry_id, reason="誤記帳の訂正(テスト)", run_id=run_id,
        entry_date=d1,
    )

    book_value = _util.securities_book_value
    # d0 時点: 逆仕訳はまだ立っていない → 数量も帳簿価額も生きている
    assert _util.replay_position(conn, "DEMO_FUND", 1001, as_of=d0) == (D(100), D(50_000))
    assert book_value(conn, "DEMO_FUND", 1001, as_of=d0) == D(50_000)
    # d1 時点: 両方が消える(片側だけ消えると評価替えの差分が壊れる)
    assert _util.replay_position(conn, "DEMO_FUND", 1001, as_of=d1) == (D(0), D(0))
    assert book_value(conn, "DEMO_FUND", 1001, as_of=d1) == D(0)


# ── 全売却後の評価残渣(独立審査 新-10)────────────────────────────────────────
def _full_sell_scenario(conn, run_id) -> tuple[date, date]:
    """審査 新-10 の実測ケース: 1000株@1000 買い → 終値 1200 で評価替え → 翌日 1200 で全売り。

    d0 の締めで securities は時価 1,200,000 になるが、売りは**取得原価ぶん**(1,000,000)
    しか取り崩さない。d1 の締めが数量ゼロを理由に評価替えをスキップすると、評価益
    200,000 が資産に残り NAV が恒久的に過大になる(修正前の実測: d1 NAV 10,400,000 /
    securities 残高 200,000 / 数量 0 → returns [+0.0196, 0.0]、真値 [0.0, 0.0])。
    """
    d0, d1 = DAY, DAY + timedelta(days=1)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=1000, price=1000, entry_date=d0, run_id=run_id)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d0, price_source={1001: 1200}, run_id=run_id
    )
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="sell",
                      qty=1000, price=1200, entry_date=d1, run_id=run_id)
    return d0, d1


def test_close_writes_off_the_residue_of_a_fully_sold_position(conn, run_id):
    """全売却した銘柄の残渣を締めがゼロへ洗い替える(偽リターン +1.96% が消える)。"""
    d0, d1 = _full_sell_scenario(conn, run_id)

    # 価格ソースは 1001 を**持たない**: 建玉ゼロの銘柄の終値は引かない(引く設計だと
    # 上場廃止・バー欠測で締めそのものが落ちる)。
    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={}, run_id=run_id
    )

    assert _util.replay_position(conn, "DEMO_FUND", 1001) == (D(0), D(0))
    assert _util.securities_book_value(conn, "DEMO_FUND", 1001, as_of=d1) == D(0)

    nav_d0, nav_d1 = _snapshot(conn, d0)[0], _snapshot(conn, d1)[0]
    assert (nav_d0, nav_d1) == (D(10_200_000), D(10_200_000))  # 修正前は d1 が 10,400,000
    assert nav_d1 / nav_d0 - 1 == D(0)  # 修正前 +0.0196 の恒久的な偽リターン
    assert result["nav"] == D(10_200_000)

    # 純損益は実現益 200,000 のみ(未実現は洗い替えで戻る)。試算表もゼロバランス。
    t = statements.book_totals(conn, "DEMO_FUND", d1)
    assert t["net_income"] == D(200_000)
    assert t["assets"] == t["liabilities"] + t["equity"] + t["net_income"]
    tb = statements.trial_balance(conn, "DEMO_FUND", d1)
    assert tb[tb["account_id"] == "_TOTAL"].iloc[0]["balance"] == D(0)

    # 洗い替えは positions と語彙を分けて証憑に残す(数量ゼロなので建玉明細ではない)。
    detail = _snapshot(conn, d1)[2]
    assert detail["positions"] == {}
    assert detail["zero_qty_writeoffs"]["1001"] == {
        "qty": "0", "price": None, "market_value": "0", "book_value": "200000",
        "entry_id": result["marked"][0],
    }
    assert "unexplained_residue" not in detail  # 説明のつく残渣(評価替えぶん)だった

    # 価格を引いていないので証憑の price は null(「終値 0 円で評価した」と書かない)。
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.payload_ref FROM ledger.journal_entries je
               JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
               WHERE je.entry_id = %s""",
            (result["marked"][0],),
        )
        payload = json.loads(cur.fetchone()[0])
    assert payload["price"] is None and payload["zero_qty_writeoff"] is True
    assert payload["qty"] == "0" and payload["market_value"] == "0"

    # 冪等: 残渣が無い日は仕訳を書かない。
    again = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={}, run_id=run_id
    )
    assert again["marked"] == [] and "zero_qty_writeoffs" not in _snapshot(conn, d1)[2]


def test_close_keeps_marking_partially_sold_positions(conn, run_id):
    """一部売却(qty>0)の評価替えは従来どおり — 残数量の時価に一致する(不変の確認)。"""
    d0, d1 = DAY, DAY + timedelta(days=1)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=1000, price=1000, entry_date=d0, run_id=run_id)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d0, price_source={1001: 1200}, run_id=run_id
    )
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="sell",
                      qty=400, price=1200, entry_date=d1, run_id=run_id)

    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={1001: 1300}, run_id=run_id
    )
    assert _util.securities_book_value(conn, "DEMO_FUND", 1001, as_of=d1) == D(780_000)
    detail = _snapshot(conn, d1)[2]
    assert detail["positions"]["1001"] == {
        "qty": "600", "price": "1300", "market_value": "780000"
    }
    assert "zero_qty_writeoffs" not in detail
    # 現金 9,480,000 + 建玉 780,000。実現益 80,000 + 未実現 180,000。
    assert _snapshot(conn, d1)[0] == D(10_260_000)


def test_close_remarks_after_repurchase(conn, run_id):
    """全売却 → 洗い替え → 買い直しのサイクルで評価替えが正しく再開する。"""
    d0, d1 = _full_sell_scenario(conn, run_id)
    d2 = d1 + timedelta(days=1)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={}, run_id=run_id
    )
    # 修正前は残渣 200,000 が d1 に残り(NAV 10,400,000)、買い直した d2 の評価替えで
    # 相殺されて消える = 日次リターンに +1.96% → −1.4% の偽の往復が立つ。
    assert _snapshot(conn, d1)[0] == D(10_200_000)

    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=500, price=1400, entry_date=d2, run_id=run_id)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d2, price_source={1001: 1500}, run_id=run_id
    )

    assert _util.securities_book_value(conn, "DEMO_FUND", 1001, as_of=d2) == D(750_000)
    detail = _snapshot(conn, d2)[2]
    assert detail["positions"]["1001"]["qty"] == "500"
    assert "zero_qty_writeoffs" not in detail  # 建玉が戻ったので洗い替えは無い
    # 現金 9,500,000 + 建玉 750,000(実現益 200,000 + 未実現 50,000)。
    assert _snapshot(conn, d2)[0] == D(10_250_000)


def _post_in_kind(conn, run_id, day: date, instrument_id: int, amount: Decimal):
    """**数量つき証憑を伴わない**原価勘定への直接記帳(Dr securities / Cr capital)。

    0034 の原価勘定ガード(``ledger.check_cost_line``)以降、これは**書込時に拒否される**。
    数量を再生できない行が原価勘定に載ると、恒等式にも洗い替えにも掛からないまま NAV に
    居座るためである(独立審査 新-20 の実測: instrument_id NULL の 2,000,000 が
    ``held_instruments()==[]`` で一度も検査されず NAV 12,000,000 が恒久残存した)。
    """
    return posting.post_entry(
        conn,
        book_id="DEMO_FUND",
        entry_date=day,
        description="現物拠出(テスト)",
        lines=[
            {"account_id": "securities", "debit": amount, "currency": "JPY",
             "instrument_id": instrument_id},
            {"account_id": "capital", "credit": amount, "currency": "JPY"},
        ],
        evidence={"kind": "decision", "payload": {"test": "in_kind"}, "source": "test"},
        run_id=run_id,
        posted_by="test.ledger",
    )


def test_cost_account_refuses_postings_without_a_quantity_evidence(conn, run_id):
    """原価勘定に載る行は必ず数量再生の対象(独立審査 新-20 — 検出でなく拒否で塞ぐ)。

    検出器を足しても「検出器の視界の外」は残る。審査の実測では instrument_id を持たない
    ``Dr securities 2,000,000 / Cr capital`` が ``held_instruments()==[]`` により恒等式に
    一度も掛からず、NAV 12,000,000 が**無言で恒久残存**した。ガードは (a) instrument_id
    必須 (b) 親証憑の kind ∈ ``POSITION_EVIDENCE_KINDS`` の 2 条件で視界の外に置けなくする。
    """
    # (a) 数量を再生できない証憑(kind='decision')— 銘柄付きでも拒否。
    with pytest.raises(psycopg.errors.RaiseException, match="数量を再生できる証憑"):
        with conn.transaction():
            _post_in_kind(conn, run_id, DAY, 1005, D(1_000_000))

    # (b) instrument_id NULL(新-20 の実測ケースそのもの)。
    with pytest.raises(psycopg.errors.RaiseException, match="instrument_id"):
        with conn.transaction():
            posting.post_entry(
                conn, book_id="DEMO_FUND", entry_date=DAY, description="銘柄なし建玉",
                lines=[
                    {"account_id": "securities", "debit": D(2_000_000), "currency": "JPY"},
                    {"account_id": "capital", "credit": D(2_000_000), "currency": "JPY"},
                ],
                evidence={"kind": "in_kind_contribution", "payload": {}, "source": "test"},
                run_id=run_id, posted_by="ops.capital_ops",
            )

    # 正規の API は通り、NAV も建玉も期待どおりに立つ(拒否が広すぎないことの対照)。
    posting.post_in_kind_contribution(
        conn, book_id="DEMO_FUND", instrument_id=1005, qty=1000, price=1000,
        entry_date=DAY, run_id=run_id,
    )
    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={1005: 1000}, run_id=run_id
    )
    assert result["unexplained_residue"] == {}
    assert _snapshot(conn, DAY)[0] == D(11_000_000)


def test_close_writes_off_only_the_mtm_share_when_both_coexist(conn, run_id):
    """原価側に説明不能な残高が同居しても、洗い替えるのは評価調整勘定ぶんだけ。

    0034 の原価勘定ガード以降、原価側に「再生できない残高」を作れるのは**逆仕訳経由**
    だけである(新-15 の実測ケース: 買いだけを取り消して売りを残すオペミス)。洗い替えが
    原価勘定に触れないことは勘定分離で構造的に保証されるが、両者が同じ銘柄に同居した日に
    「評価調整だけが消え、原価側は名指しされて残る」ことを固定する。
    """
    d0, d1 = _full_sell_scenario(conn, run_id)  # 1001: 評価替え残渣 200,000 を作る
    with conn.cursor() as cur:  # 買い約定だけを逆仕訳(売りは残す)
        cur.execute(
            """SELECT je.entry_id FROM ledger.journal_entries je
               JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
               WHERE je.book_id = 'DEMO_FUND' AND e.kind = 'broker_fill'
               ORDER BY je.entry_id LIMIT 1"""
        )
        buy_entry_id = cur.fetchone()[0]
    posting.reverse_entry(conn, entry_id=buy_entry_id, reason="オペミス(テスト)",
                          run_id=run_id, entry_date=d1)

    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={}, run_id=run_id
    )
    # 評価調整 200,000 は消え、原価側の −1,000,000 は残って名指しされる。
    assert _util.mtm_book_value(conn, "DEMO_FUND", 1001, as_of=d1) == D(0)
    assert _util.securities_cost_value(conn, "DEMO_FUND", 1001, as_of=d1) == D(-1_000_000)
    assert result["zero_qty_writeoffs"]["1001"]["book_value"] == "200000"
    assert result["unexplained_residue"]["1001"]["reason"] == "zero_qty_residue"


def test_reclose_writes_off_the_residue_of_a_late_full_sell(conn, run_id):
    """再締めも数量ゼロの残渣を消す — 当日経路と定義を揃える(片方だけだと NAV が食い違う)。

    価格ソースは終値を返さない(``no_price``)。建玉ゼロの銘柄の時価は価格に依らずゼロ
    なので、バーが無くてもその日の再適用を諦めない。
    """
    d0, d1 = DAY, DAY + timedelta(days=1)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=1000, price=1000, entry_date=d0, run_id=run_id)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d0, price_source={1001: 1200}, run_id=run_id
    )
    assert _snapshot(conn, d0)[0] == D(10_200_000)

    # d0 の締めの**後**に d0 付けで全売りが記帳される(遅延約定)。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="sell",
                      qty=1000, price=1200, entry_date=d0, run_id=run_id)
    before = _max_entry_id(conn)

    changed = _reclose(conn, run_id, d1, price_source=closing.no_price)
    item = next(c for c in changed if c["date"] == d0)
    assert item["mtm_reapplied"] is True and item["mtm_pending"] is False

    detail = _snapshot(conn, d0)[2]
    assert detail["mtm_reapplied"]["delta"] == "-200000"
    # 数量ゼロは positions(建玉明細)に混ぜず、当日経路と同じスキーマで別キーに出す(新-16)。
    assert detail["mtm_reapplied"]["positions"] == {}
    assert detail["mtm_reapplied"]["zero_qty_writeoffs"]["1001"] == {
        "qty": "0", "price": None, "market_value": "0", "book_value": "200000",
        "entry_id": None,  # 再締めは仕訳を書かない
    }
    # 仕訳集計そのままの NAV(残渣込み 10,400,000)+ delta = 真の NAV(新-9 の不変式)。
    assert D(detail["nav_from_journals"]) == D(10_400_000)
    assert _snapshot(conn, d0)[0] == D(10_200_000)
    assert _max_entry_id(conn) == before  # 再締めは仕訳を書かない


def test_close_keeps_a_live_position_when_a_future_dated_sell_is_recorded_early(conn, run_id):
    """将来日付の売りが先に記帳されていても、その日に実在する建玉を消さない(新-13)。

    数量を全期間再生・帳簿価額を as_of で取る非対称のまま「数量ゼロ ⇒ 残渣」を判定すると、
    d2 付けの全売りを先に記帳した状態で d1 を締めたとき **NAV 10,200,000 → 10,000,000**、
    returns `[-0.019608]`(真値 `[0.0]`)になった。符号が逆なだけで新-10 と同じ偽リターン。
    """
    d0, d1, d2 = DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=1000, price=1000, entry_date=d0, run_id=run_id)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d0, price_source={1001: 1200}, run_id=run_id
    )
    # 将来日付(d2)の売りが先に記帳される。d1 時点ではまだ建玉 1000 株を持っている。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="sell",
                      qty=1000, price=1200, entry_date=d2, run_id=run_id)

    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={1001: 1200}, run_id=run_id
    )
    nav_d0, nav_d1 = _snapshot(conn, d0)[0], _snapshot(conn, d1)[0]
    assert (nav_d0, nav_d1) == (D(10_200_000), D(10_200_000))
    assert nav_d1 / nav_d0 - 1 == D(0)
    assert _util.securities_book_value(conn, "DEMO_FUND", 1001, as_of=d1) == D(1_200_000)
    assert result["zero_qty_writeoffs"] == {}  # 建玉があるので洗い替えではない
    assert _snapshot(conn, d1)[2]["positions"]["1001"]["qty"] == "1000"


def _post_forged_mtm(conn, run_id, day: date, lines: list[dict]) -> int:
    """評価替えを騙る手仕訳(kind='price_snapshot' だが締めジョブ由来ではない)。"""
    return posting.post_entry(
        conn, book_id="DEMO_FUND", entry_date=day, description="偽装された評価替え",
        lines=lines,
        evidence={"kind": "price_snapshot", "payload": {"forged": True}, "source": "test"},
        run_id=run_id, posted_by="test.ledger",
    )


def test_close_ignores_forged_price_snapshot_entries(conn, run_id):
    """評価替えを騙る手仕訳は**書込時に拒否される**(独立審査 新-14 の攻撃 2 種)。

    洗い替えは NAV を双方向に動かす原始操作なので、判定子が evidence の自由文字列だけだと
    (a) 借方に立てた手仕訳は実在資産を全額消され、(b) 貸方に立てると NAV を無から増やせた。
    0034 以降、この 2 種はどちらも原価勘定ガード(数量を再生できる証憑のみ)に掛かって
    そもそも記帳できない — 検出ではなく拒否で塞ぐ。
    """
    # (a) Dr securities / Cr capital を kind='price_snapshot' で立てる。
    with pytest.raises(psycopg.errors.RaiseException, match="数量を再生できる証憑"):
        with conn.transaction():
            _post_forged_mtm(
                conn, run_id, DAY,
                [{"account_id": "securities", "debit": D(3_000_000), "currency": "JPY",
                  "instrument_id": 1006},
                 {"account_id": "capital", "credit": D(3_000_000), "currency": "JPY"}],
            )
    # (b) 貸方に立てて NAV を増やさせようとする(現金と相殺 = それ自体は NAV 中立)。
    with pytest.raises(psycopg.errors.RaiseException, match="数量を再生できる証憑"):
        with conn.transaction():
            _post_forged_mtm(
                conn, run_id, DAY,
                [{"account_id": "cash", "debit": D(500_000), "currency": "JPY"},
                 {"account_id": "securities", "credit": D(500_000), "currency": "JPY",
                  "instrument_id": 1007}],
            )

    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    assert result["marked"] == [] and result["zero_qty_writeoffs"] == {}
    assert result["unexplained_residue"] == {}
    assert _snapshot(conn, DAY)[0] == D(10_000_000)  # 手仕訳ぶんは 1 円も入らない


def test_mtm_account_forgery_is_not_covered_by_the_cost_identity(conn, run_id):
    """**恒等式は評価調整勘定を直接叩く偽装を検出しない**(独立審査 新-19 の限界固定)。

    設計文書 11 §5.2-1 が「分離は新-14 を塞がない」と書いているとおり、締めジョブ名を
    騙った ``Dr securities_mtm / Cr capital`` は次の締めで全額洗い替えられ NAV を落とす。
    この経路の防御は ``post_mark_to_market`` の posted_by 検証と 0034 の DB トリガであって
    恒等式ではない — その事実をテストで固定し、「恒等式が全部を覆う」という誤読を防ぐ。
    """
    posting.post_entry(
        conn, book_id="DEMO_FUND", entry_date=DAY, description="締めジョブ名を騙る評価替え",
        lines=[
            {"account_id": "securities_mtm", "debit": D(3_000_000), "currency": "JPY",
             "instrument_id": 1006},
            {"account_id": "capital", "credit": D(3_000_000), "currency": "JPY"},
        ],
        evidence={"kind": "price_snapshot", "payload": {"forged": True}, "source": "test"},
        run_id=run_id, posted_by="ledger.closing",  # ← 騙れる(申告制)
    )
    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={}, run_id=run_id
    )
    # 洗い替えられて NAV が落ち、偽の未実現損が立つ。恒等式(原価勘定側)は**無音**。
    assert result["zero_qty_writeoffs"]["1006"]["book_value"] == "3000000"
    assert result["unexplained_residue"] == {}
    assert _snapshot(conn, DAY)[0] == D(10_000_000)
    assert statements.book_totals(conn, "DEMO_FUND", DAY)["net_income"] == D(-3_000_000)


def test_close_reports_residue_left_by_a_reversal_mistake(conn, run_id):
    """買いだけを逆仕訳して売りを残すオペミスも検出する(新-15 の実測ケース)。

    数量ゼロで `securities` が **−1,000,000** のまま締めは無言で通り、試算表はゼロバランス
    なので気づけない。洗い替えの対象外(評価替え由来ではない)ぶんを毎締めで名指しする。
    """
    d0, d1 = _full_sell_scenario(conn, run_id)
    d2 = d1 + timedelta(days=1)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={}, run_id=run_id
    )
    with conn.cursor() as cur:  # 買い約定の entry_id(最初の broker_fill 仕訳)
        cur.execute(
            """SELECT je.entry_id FROM ledger.journal_entries je
               JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
               WHERE je.book_id = 'DEMO_FUND' AND e.kind = 'broker_fill'
               ORDER BY je.entry_id LIMIT 1"""
        )
        buy_entry_id = cur.fetchone()[0]
    posting.reverse_entry(conn, entry_id=buy_entry_id, reason="オペミス(テスト)",
                          run_id=run_id, entry_date=d2)

    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d2, price_source={}, run_id=run_id
    )
    assert result["unexplained_residue"] == {
        "1001": {"book_value": "-1000000", "replay_cost": "0", "qty": "0",
                 "reason": "zero_qty_residue"}
    }
    tb = statements.trial_balance(conn, "DEMO_FUND", d2)  # 試算表は通る = 気づけない
    assert tb[tb["account_id"] == "_TOTAL"].iloc[0]["balance"] == D(0)


def test_zero_qty_writeoff_schema_is_shared_by_close_and_reclose(conn, run_id):
    """当日経路と再締め経路が同じキー・同じ行スキーマで洗い替えを記録する(新-16)。"""
    d0, d1, d2 = DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)
    for iid in (1001, 1002):
        posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                          qty=1000, price=1000, entry_date=d0, run_id=run_id)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d0, price_source={1001: 1200, 1002: 1200},
        run_id=run_id,
    )
    # 1001 は通常どおり全売り → 当日経路の洗い替え。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="sell",
                      qty=1000, price=1200, entry_date=d1, run_id=run_id)
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={1002: 1200}, run_id=run_id
    )
    # 1002 は締めの**後**に d1 付けで全売り(遅延約定)→ 再締め経路の洗い替え。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1002, side="sell",
                      qty=1000, price=1200, entry_date=d1, run_id=run_id)
    _reclose(conn, run_id, d2, price_source=_close_1200)

    detail = _snapshot(conn, d1)[2]
    from_close = detail["zero_qty_writeoffs"]["1001"]
    from_reclose = detail["mtm_reapplied"]["zero_qty_writeoffs"]["1002"]
    assert from_close.keys() == from_reclose.keys()
    assert from_close["book_value"] == from_reclose["book_value"] == "200000"
    assert from_close["entry_id"] is not None  # 当日経路は仕訳を書く
    assert from_reclose["entry_id"] is None  # 再締めは書かない(null で明示)
    assert detail["mtm_reapplied"]["positions"] == {}  # 幽霊行を建玉明細に混ぜない


def _mk_evidence(cur) -> int:
    cur.execute(
        """INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
           VALUES ('broker_fill', 'inline://trade', sha256('x'::bytea), 'test', now())
           RETURNING evidence_id"""
    )
    return cur.fetchone()[0]


# ── 勘定分離(0034)と現物拠出の評価替え(独立審査 新-14 / 新-17)─────────────
#
# 0034 は評価替えを ``securities`` から ``securities_mtm`` へ分離した。分離の**等価性**
# (NAV・帳簿価額・日次リターンが 1 円も変わらないこと)は既存の新-10 / 新-13 / 新-16 の
# 回帰テスト群がそのまま対照になっている — それらは NAV とリターンを直接固定しており、
# 分離後も無改変で通る。以下は分離が**新たに可能にした検査**と、現物拠出の評価替えを見る。


def test_in_kind_contribution_is_marked_to_market(conn, run_id):
    """現物拠出した建玉が時価評価される(独立審査 新-17 の是正)。

    是正前は ``replay_position`` が ``broker_fill`` しか再生しなかったため、拠出建玉は
    数量ゼロに見え**一度も評価替えされなかった**(審査実測: 終値 1500/2000/500 を渡しても
    残高は拠出額 1,000,000 のまま、``detail.positions`` にも現れない)。建玉数量の真実を
    拠出証憑(``in_kind_contribution``)に持たせ、再生対象に含めることで解消する。
    """
    d0, d1 = DAY, DAY + timedelta(days=1)
    posting.post_in_kind_contribution(
        conn, book_id="DEMO_FUND", instrument_id=1010, qty=1000, price=1000,
        entry_date=d0, run_id=run_id,
    )
    # 拠出日: 時価 = 拠出価額なので評価替えの仕訳は立たない(delta 0)。
    r0 = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d0, price_source={1010: 1000}, run_id=run_id
    )
    assert r0["marked"] == [] and r0["unexplained_residue"] == {}
    assert _snapshot(conn, d0)[0] == D(11_000_000)
    assert _snapshot(conn, d0)[2]["positions"]["1010"]["qty"] == "1000"

    # 翌日 1500 円: 是正前はここが動かなかった。
    r1 = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={1010: 1500}, run_id=run_id
    )
    assert len(r1["marked"]) == 1
    assert _snapshot(conn, d1)[2]["positions"]["1010"] == {
        "qty": "1000", "price": "1500", "market_value": "1500000"
    }
    assert _snapshot(conn, d1)[0] == D(11_500_000)
    # 内訳: 原価勘定は拠出価額のまま、評価差額は評価調整勘定に乗る(0034)。
    assert _util.securities_cost_value(conn, "DEMO_FUND", 1010, as_of=d1) == D(1_000_000)
    assert _util.mtm_book_value(conn, "DEMO_FUND", 1010, as_of=d1) == D(500_000)
    assert r1["unexplained_residue"] == {}


def test_in_kind_and_fills_share_one_moving_average(conn, run_id):
    """拠出建玉と約定建玉が同じ銘柄に同居しても移動平均法が一貫する(混在ケース)。

    拠出 1000@1000 → 買い 1000@2000(平均原価 1500)→ 500 株を 2500 で売却。実現損益は
    500×(2500−1500)=500,000 でなければならない。拠出を再生に含めないと平均原価が 2000 に
    なり、実現損益・原価恒等式の双方が狂う。
    """
    iid = 1011
    posting.post_in_kind_contribution(
        conn, book_id="DEMO_FUND", instrument_id=iid, qty=1000, price=1000,
        entry_date=DAY, run_id=run_id,
    )
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=1000, price=2000, entry_date=DAY, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=500, price=2500, entry_date=DAY, run_id=run_id)

    qty, cost = _util.replay_position(conn, "DEMO_FUND", iid, as_of=DAY)
    assert (qty, cost) == (D(1500), D(2_250_000))  # 平均原価 1500
    # 原価恒等式: 原価勘定の残高 = 再生した取得原価。
    assert _util.securities_cost_value(conn, "DEMO_FUND", iid, as_of=DAY) == cost

    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={iid: 2400}, run_id=run_id
    )
    assert result["unexplained_residue"] == {}
    # 実現益 500,000 + 未実現 1,350,000(= 1500×2400 − 2,250,000)+ 拠出 1,000,000。
    t = statements.book_totals(conn, "DEMO_FUND", DAY)
    assert t["nav"] == D(12_850_000) == result["nav"]
    assert _util.mtm_book_value(conn, "DEMO_FUND", iid, as_of=DAY) == D(1_350_000)


def test_reversing_an_in_kind_contribution_unwinds_the_position(conn, run_id):
    """拠出の逆仕訳で建玉が消え、評価替えの残渣も洗い替えられる(訂正経路)。

    逆仕訳は ``journal_entries`` の唯一の訂正手段(0005 は UPDATE/DELETE を禁じる)なので、
    拠出を再生対象に加える以上、取り消しも再生から落ちなければならない。
    """
    d0, d1 = DAY, DAY + timedelta(days=1)
    entry_id = posting.post_in_kind_contribution(
        conn, book_id="DEMO_FUND", instrument_id=1012, qty=1000, price=1000,
        entry_date=d0, run_id=run_id,
    )
    closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d0, price_source={1012: 1500}, run_id=run_id
    )
    assert _snapshot(conn, d0)[0] == D(11_500_000)

    posting.reverse_entry(conn, entry_id=entry_id, reason="拠出の取消(テスト)",
                          run_id=run_id, entry_date=d1)
    # 建玉ゼロなので終値は要らない(価格ソースは空でよい)。
    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=d1, price_source={}, run_id=run_id
    )
    assert _util.replay_position(conn, "DEMO_FUND", 1012, as_of=d1) == (D(0), D(0))
    assert result["zero_qty_writeoffs"]["1012"]["book_value"] == "500000"
    assert _util.securities_book_value(conn, "DEMO_FUND", 1012, as_of=d1) == D(0)
    assert result["unexplained_residue"] == {}  # 原価恒等式は保たれる
    assert _snapshot(conn, d1)[0] == D(10_000_000)


def test_close_names_a_cost_identity_break_on_a_live_position(conn, run_id):
    """建玉が残っていても、申告と帳簿が食い違えば原価恒等式の破れとして名指しされる。

    0034 の原価勘定ガードは「数量を再生できる証憑を持つこと」しか要求できない(kind も
    証憑の自由記入列である — 新-21 と同じ限界)。ここでは ``broker_fill`` を騙りつつ
    payload の数量をゼロにした申告を立て、**申告(再生原価 50,000)と帳簿(350,000)の
    食い違い**が恒等式に出ることを固定する。分離前はこの検査が書けなかった —
    ``securities`` に原価と評価調整が同居しており、突合には評価調整ぶんを推定で差し引く
    必要があった(その推定子こそ新-14 が騙したものである)。
    """
    iid = 1013
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=DAY, run_id=run_id)
    # 数量ゼロを申告する偽の約定証憑(ガードは通るが再生原価は増えない)。
    posting.post_entry(
        conn, book_id="DEMO_FUND", entry_date=DAY, description="数量ゼロを申告する偽約定",
        lines=[
            {"account_id": "securities", "debit": D(300_000), "currency": "JPY",
             "instrument_id": iid},
            {"account_id": "capital", "credit": D(300_000), "currency": "JPY"},
        ],
        evidence={
            "kind": "broker_fill",
            "payload": {"instrument_id": iid, "side": "buy", "qty": "0", "price": "0"},
            "source": "test",
        },
        run_id=run_id, posted_by="test.ledger",
    )

    result = closing.run_daily_close(
        conn, book_id="DEMO_FUND", date=DAY, price_source={iid: 500}, run_id=run_id
    )
    assert result["unexplained_residue"] == {
        str(iid): {"book_value": "350000", "replay_cost": "50000", "qty": "100",
                   "reason": "cost_identity_broken"},
    }
    # 数量ゼロの残渣ではないので洗い替えは起きない(建玉のある銘柄には触れない)。
    assert result["zero_qty_writeoffs"] == {}
