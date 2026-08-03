"""締め処理(close)の受け入れテスト。

E2E: gate_and_record(pass)→ runner → run_demo_close → risk.nav_daily 更新まで通し。
照合の一致・不一致検出(執行照合+ポジション照合)と provisional/confirmed の判定を
検証する。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from ryza.execution.close import reconcile_executions, run_demo_close
from ryza.execution.demo import DemoBroker
from ryza.execution.runner import run_pending
from ryza.gate.orders import advance_order_status, record_execution
from ryza.ledger import posting
from ryza.risk.engine import book_returns
from ryza.risk.navflow import (
    load_nav_flow_data,
    recon_invalidated_days,
    recon_invalidated_note,
)

from .conftest import JST, make_test_config


def _broker(conn, today) -> DemoBroker:
    return DemoBroker(conn, config=make_test_config(), trade_date=today)


def _fill_one(conn, run_id, passed_order, insert_bar, today):
    """買い 100 株を通し、約定まで済ませる(価格 1000.64・手数料 0)。"""
    insert_bar(1, today, close=Decimal(1000), volume=Decimal(1_000_000))
    order_id = passed_order()
    summary = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today), run_id=run_id
    )
    assert summary["filled"] == 1, summary
    return order_id


def _nav_daily_rows(conn, book_id="DEMO_FUND"):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT nav_date, nav, status, detail FROM risk.nav_daily
            WHERE book_id = %s ORDER BY nav_date
            """,
            (book_id,),
        )
        return cur.fetchall()


# ── E2E: ゲート → 執行 → 締め → NAV 確定 ─────────────────────────────────────
def test_close_end_to_end_confirmed(conn, run_id, passed_order, insert_bar, today_jst):
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    breaks: list[dict] = []
    result = run_demo_close(
        conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id, on_break=breaks.append
    )
    # NAV = 現金 (10,000,000 − 100,064) + 証券時価 (100×1000) = 9,999,936。
    assert result["nav"] == Decimal("9999936.00")
    assert result["status"] == "confirmed"
    assert result["exec_recon"]["matched"] is True
    assert breaks == []

    rows = _nav_daily_rows(conn)
    assert len(rows) == 1
    nav_date, nav, status, detail = rows[0]
    assert (nav_date, Decimal(nav), status) == (today_jst, Decimal("9999936.00"), "confirmed")
    assert detail["exec_recon"]["matched"] is True
    assert detail["positions"] == {"1": "100"}

    # ledger.nav_snapshots(既存 API 経由)も confirmed になっている。
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT nav, status FROM ledger.nav_snapshots
            WHERE book_id = 'DEMO_FUND' AND snap_date = %s
            """,
            (today_jst,),
        )
        snap = cur.fetchone()
    assert (Decimal(snap[0]), snap[1]) == (Decimal("9999936.00"), "confirmed")


# ── 再締め: 審査シナリオ G と再審査の対照実験 ─────────────────────────────────
# 独立審査 重要-2 / 再-1(docs/reviews/risk-navflow-rollforward-independent-review.md):
# 締めが走った**後**に同じ日付で立った仕訳は当日のスナップショットに入らない。navflow は
# その仕訳を当日の flow_eop として NAV から引くため、当日に偽の下振れ・翌日に同額の偽の
# 上振れという ±X% の対が立つ。判定は必ず book_returns(観測量)で行う — NAV 値だけを
# 固定すると窓境界の偽リターンを「仕様」として固定してしまう(再審査の指摘)。
_G_DAYS = [date(2026, 9, d) for d in range(1, 7)]  # シード出資(8/2・8/3)より後


def _post_contribution(conn, run_id, day, amount: Decimal) -> None:
    """指定日付で出資を記帳する(navflow が外部フローとして拾う拠出資本勘定)。"""
    posting.post_entry(
        conn,
        book_id="DEMO_FUND",
        entry_date=day,
        description="テスト用の出資(締め後)",
        lines=[
            {"account_id": "cash", "debit": amount, "currency": "JPY"},
            {"account_id": "capital", "credit": amount, "currency": "JPY"},
        ],
        evidence={"kind": "decision", "payload": {"test": "reclose"}, "source": "test"},
        run_id=run_id,
        posted_by="test.execution",
    )


def _returns(conn) -> list[float]:
    """navflow の NAV 点から帳簿リターン(外部フロー調整済み TWR)を測る。"""
    return book_returns(load_nav_flow_data(conn, "DEMO_FUND").points)


def _close(conn, run_id, day):
    return run_demo_close(conn, book_id="DEMO_FUND", date=day, run_id=run_id)


def test_reclose_resolves_false_return_pair(conn, run_id):
    """シナリオ G: 締め後の同日出資 → 翌日の締めで ±50% の偽リターン対が消える。"""
    d0, d1, d2 = _G_DAYS[0], _G_DAYS[1], _G_DAYS[2]
    _close(conn, run_id, d0)
    _close(conn, run_id, d1)
    _post_contribution(conn, run_id, d1, Decimal(5_000_000))  # ← d1 の締めの後に立つ
    # 出資が NAV に入らないまま flow_eop だけ引かれる = 偽の −50%(不具合の再現)。
    assert _returns(conn) == [-0.5]

    result = _close(conn, run_id, d2)
    assert _returns(conn) == [0.0, 0.0]
    restated = [r for r in result["reclose"] if r["restated"]]
    assert [(r["date"], r["nav_before"], r["nav_after"]) for r in restated] == [
        (d1, Decimal(10_000_000), Decimal(15_000_000))
    ]
    assert restated[0]["age_business_days"] == 1  # 締め 1 回前 = urgent ではない


def test_reclose_catches_entries_older_than_any_fixed_window(conn, run_id):
    """再審査 再-1 の対照実験: 固定窓 N=3 なら窓外に落ちる古い遅延仕訳も水位で拾う。

    d0〜d4 を締めた**後**に d1 付けの出資が立つ(4 営業日遅れの記帳)。固定窓 N=3 の
    実装は d2〜d4 だけを書き換えて d1 を残すため、窓境界に恒久的な +50% を作った
    (審査実測 ``[-0.5, +0.5, 0, 0, 0]``)。水位検出は d1 以降すべてを stale と判定する。
    """
    days = _G_DAYS[:5]
    for day in days:
        _close(conn, run_id, day)
    _post_contribution(conn, run_id, days[1], Decimal(5_000_000))
    assert _returns(conn) == [-0.5, 0.0, 0.0, 0.0]  # 窓外の偽リターン(修正前)

    result = _close(conn, run_id, _G_DAYS[5])
    assert _returns(conn) == [0.0, 0.0, 0.0, 0.0, 0.0]  # 窓の縁が存在しない
    restated = [r["date"] for r in result["reclose"] if r["restated"]]
    assert restated == days[1:]  # d0 は出資日より前なので対象外


def test_reclose_advances_watermark_for_nav_neutral_entries(conn, run_id):
    """再審査 再-4: NAV 中立の遅延仕訳でも水位を進め、翌日以降の再検出を止める。"""
    d0, d1, d2 = _G_DAYS[0], _G_DAYS[1], _G_DAYS[2]
    _close(conn, run_id, d0)
    # 資産の振替のみ(現金 → 証券・手数料ゼロ)= NAV は動かないが仕訳は立つ。
    posting.post_entry(
        conn, book_id="DEMO_FUND", entry_date=d0,
        description="NAV 中立の遅延記帳(テスト)",
        lines=[
            {"account_id": "securities", "debit": Decimal(1000), "currency": "JPY",
             "instrument_id": 1},
            {"account_id": "cash", "credit": Decimal(1000), "currency": "JPY"},
        ],
        evidence={"kind": "decision", "payload": {"test": "neutral"}, "source": "test"},
        run_id=run_id, posted_by="test.execution",
    )

    first = _close(conn, run_id, d1)
    detected = [r for r in first["reclose"] if r["date"] == d0]
    assert len(detected) == 1
    assert detected[0]["late_entries"] is True
    assert detected[0]["restated"] is False  # NAV は動いていない
    assert detected[0]["watermark_after"] > detected[0]["watermark_before"]

    # 2 回目の締めでは水位が最新なので同じ日を再検出しない(偽検出の永続化を防ぐ)。
    second = _close(conn, run_id, d2)
    assert [r["date"] for r in second["reclose"]] == []


def test_reclose_invalidates_stale_positions_detail(conn, run_id):
    """再審査 再-2/再-3: 訂正した日の建玉明細は残さず、restated の事実を書く。"""
    d0, d1 = _G_DAYS[0], _G_DAYS[1]
    _close(conn, run_id, d0)
    _post_contribution(conn, run_id, d0, Decimal(5_000_000))
    _close(conn, run_id, d1)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, detail FROM ledger.nav_snapshots "
            "WHERE book_id = 'DEMO_FUND' AND snap_date = %s",
            (d0,),
        )
        status, detail = cur.fetchone()
    assert status == "confirmed"  # status は締め時点の照合の結論(据え置き)
    assert detail["restated"] is True  # restated はその後の会計訂正
    assert detail["positions_stale"] is True and "positions" not in detail
    assert detail["mtm_not_reapplied"] is True
    assert Decimal(detail["assets"]) == Decimal(15_000_000)  # 集計値は訂正後に揃う

    # risk.nav_daily 側も同じ扱い(建玉・価格を落とし、元の run を残す)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT nav, detail, run_id FROM risk.nav_daily "
            "WHERE book_id = 'DEMO_FUND' AND nav_date = %s",
            (d0,),
        )
        nav, nd_detail, row_run_id = cur.fetchone()
    assert Decimal(nav) == Decimal(15_000_000)
    assert "positions" not in nd_detail and nd_detail["positions_stale"] is True
    assert nd_detail["reclose"][0]["previous_run_id"] == run_id
    assert row_run_id == run_id


def test_reclose_keeps_recon_valid_for_capital_only_delay(conn, run_id):
    """拠出資本だけが遅れた日は照合の結論を動かさない(recon_invalidated を立てない)。"""
    d0, d1 = _G_DAYS[0], _G_DAYS[1]
    _close(conn, run_id, d0)
    _post_contribution(conn, run_id, d0, Decimal(5_000_000))

    result = _close(conn, run_id, d1)
    item = next(r for r in result["reclose"] if r["date"] == d0)
    assert item["restated"] is True and item["recon_invalidated"] is False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM ledger.nav_snapshots "
            "WHERE book_id = 'DEMO_FUND' AND snap_date = %s",
            (d0,),
        )
        assert "recon_invalidated" not in cur.fetchone()[0]


def test_reclose_invalidates_recon_when_trade_entries_are_late(
    conn, run_id, passed_order, insert_bar
):
    """遅延**約定**が混じった日は照合結論を無効化する(再-2 の裁定)。

    ``status`` 列は語彙を動かさず confirmed のまま、``recon_invalidated`` を機械可読で
    立てる。リスク日次はこの日を breaks 相当として urgent に上げる。
    """
    d0, d1 = _G_DAYS[0], _G_DAYS[1]
    insert_bar(1, d0, close=Decimal(1000), volume=Decimal(1_000_000))
    first = _close(conn, run_id, d0)
    assert first["status"] == "confirmed"

    # 締めの後に d0 付けの約定が記帳される(_record_unrecorded_fills と同じ経路)。
    posting.post_fill(
        conn, book_id="DEMO_FUND", instrument_id=1, side="buy",
        qty=Decimal(100), price=Decimal(1000), fee=Decimal(0),
        entry_date=d0, run_id=run_id, fill_id=999_001, source="test",
    )
    insert_bar(1, d1, close=Decimal(1000), volume=Decimal(1_000_000))

    result = _close(conn, run_id, d1)
    item = next(r for r in result["reclose"] if r["date"] == d0)
    assert item["recon_invalidated"] is True

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, detail FROM ledger.nav_snapshots "
            "WHERE book_id = 'DEMO_FUND' AND snap_date = %s",
            (d0,),
        )
        status, detail = cur.fetchone()
    assert status == "confirmed"  # 列の語彙は動かさない
    assert detail["recon_invalidated"] is True
    assert detail["positions_stale"] is True

    # risk.nav_daily 側にも写り、navflow 経由でリスク・ダッシュボードから読める。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM risk.nav_daily "
            "WHERE book_id = 'DEMO_FUND' AND nav_date = %s",
            (d0,),
        )
        assert cur.fetchone()[0]["recon_invalidated"] is True
    points = load_nav_flow_data(conn, "DEMO_FUND").points
    point = next(p for p in points if p.day == d0)
    assert point.recon_invalidated is True and point.status == "confirmed"
    assert point.recon_invalidated_by_run == run_id  # 鮮度判定の手がかり(新-2)
    assert recon_invalidated_days(points, by_run=run_id) == [d0]
    assert "新たに無効化された日" in (
        recon_invalidated_note(recon_invalidated_days(points), fresh=True) or ""
    )


def test_reclose_reapplies_mtm_from_bars_and_kills_the_false_return(
    conn, run_id, insert_bar
):
    """独立審査 新-3 の実測ケース: 遅延約定日の建玉を当日終値で評価替えし直す。

    d0 を締めた後に d0 付けで 1000 株@1000 が記帳される。市場の終値は両日 1200 なので、
    d0 を取得原価のまま残すと翌日の締めが時価に打ち直した瞬間に **+2% の恒久的な偽
    リターン**が立った(審査実測: 真値 ``[0.0]`` に対し観測 ``[+0.020]``)。再締めが
    ``market.bars``(当日の締めと同じソース)からその日の終値を引いて再適用すれば消える。
    """
    d0, d1 = _G_DAYS[0], _G_DAYS[1]
    insert_bar(1, d0, close=Decimal(1200), volume=Decimal(1_000_000))
    insert_bar(1, d1, close=Decimal(1200), volume=Decimal(1_000_000))
    _close(conn, run_id, d0)

    posting.post_fill(  # ← d0 の締めの後に d0 付けで立つ遅延約定
        conn, book_id="DEMO_FUND", instrument_id=1, side="buy",
        qty=Decimal(1000), price=Decimal(1000), fee=Decimal(0),
        entry_date=d0, run_id=run_id, fill_id=999_002, source="test",
    )

    result = _close(conn, run_id, d1)
    item = next(r for r in result["reclose"] if r["date"] == d0)
    assert item["recon_invalidated"] is True and item["mtm_reapplied"] is True
    assert (item["nav_before"], item["nav_after"]) == (
        Decimal(10_000_000), Decimal(10_200_000)
    )
    assert result["nav"] == Decimal(10_200_000)  # d1 は時価 = 同額
    assert _returns(conn) == [0.0]  # 偽リターン(+0.02)が消える

    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM risk.nav_daily "
            "WHERE book_id = 'DEMO_FUND' AND nav_date = %s",
            (d0,),
        )
        assert cur.fetchone()[0]["mtm_reapplied"] is True


def test_reclose_does_not_price_a_day_with_another_days_bar(conn, run_id, insert_bar):
    """**当日バー欠測**の日は前日以前の終値で評価しない(独立審査 新-6)。

    遡り取得(``latest_close``)を使うと、別日の終値でその日を評価しながら ``priced_at``
    にはその日を書く**虚偽の証憑**ができ、しかも当該日は以後 stale でないため誤価格が
    恒久固定される(審査実測: 前日終値 900 で NAV 9,900,000・returns ``[-0.010, +0.030]``)。
    再締めの価格ソースは ``close_on``(その日のバーだけ)を使う。
    """
    d0, d1 = _G_DAYS[0], _G_DAYS[1]
    insert_bar(1, d0 - timedelta(days=1), close=Decimal(900), volume=Decimal(1_000_000))
    _close(conn, run_id, d0)  # d0 のバー無し(建玉も無いので締めは通る)
    posting.post_fill(
        conn, book_id="DEMO_FUND", instrument_id=1, side="buy",
        qty=Decimal(1000), price=Decimal(1000), fee=Decimal(0),
        entry_date=d0, run_id=run_id, fill_id=999_003, source="test",
    )
    insert_bar(1, d1, close=Decimal(1200), volume=Decimal(1_000_000))

    result = _close(conn, run_id, d1)
    item = next(r for r in result["reclose"] if r["date"] == d0)
    assert item["recon_invalidated"] is True
    assert item["mtm_reapplied"] is False and item["mtm_pending"] is True
    assert item["nav_after"] == Decimal(10_000_000)  # 前日終値 900 で評価しない
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM ledger.nav_snapshots "
            "WHERE book_id = 'DEMO_FUND' AND snap_date = %s",
            (d0,),
        )
        detail = cur.fetchone()[0]
    assert detail["mtm_not_reapplied"] is True and "mtm_reapplied" not in detail
    # nav_daily 側も同期して立てっぱなしにしない(独立審査 新-11)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM risk.nav_daily "
            "WHERE book_id = 'DEMO_FUND' AND nav_date = %s",
            (d0,),
        )
        assert "mtm_reapplied" not in cur.fetchone()[0]


def _post_late_entry(conn, run_id, day, lines, description):
    posting.post_entry(
        conn, book_id="DEMO_FUND", entry_date=day, description=description, lines=lines,
        evidence={"kind": "decision", "payload": {"test": "late"}, "source": "test"},
        run_id=run_id, posted_by="test.execution",
    )


def test_recon_invalidation_follows_position_lines_not_account_category(conn, run_id):
    """判定は行レベルの建玉性で行う(独立審査 新-1 の 4 ケース分離)。

    仕訳単位で「拠出資本に触れない仕訳か」を見る述語は両方向に誤る:
    現物拠出(securities 借 / capital 貸)は建玉が増えるのに拠出資本行を持つため漏れ、
    費用アクルーアルは建玉を動かさないのに拠出資本に触れないだけで無効判定になる。
    """
    d0, d1 = _G_DAYS[0], _G_DAYS[1]
    _close(conn, run_id, d0)
    # 現物拠出: 1 仕訳に capital 行を含むが、建玉(instrument_id 付き)は増える。
    _post_late_entry(
        conn, run_id, d0,
        [
            {"account_id": "securities", "debit": Decimal(1_000_000), "currency": "JPY",
             "instrument_id": 1},
            {"account_id": "capital", "credit": Decimal(1_000_000), "currency": "JPY"},
        ],
        "現物拠出(遅延記帳)",
    )
    result = _close(conn, run_id, d1)
    item = next(r for r in result["reclose"] if r["date"] == d0)
    assert item["restated"] is True
    assert item["recon_invalidated"] is True  # 建玉が動いた → 照合結論は無効


def test_recon_invalidation_ignores_expense_accruals(conn, run_id):
    """費用アクルーアル(建玉を動かさない)は照合結論を無効化しない(新-1 の逆方向)。"""
    d0, d1 = _G_DAYS[0], _G_DAYS[1]
    _close(conn, run_id, d0)
    _post_late_entry(
        conn, run_id, d0,
        [
            {"account_id": "interest_expense", "debit": Decimal(500), "currency": "JPY"},
            {"account_id": "cash", "credit": Decimal(500), "currency": "JPY"},
        ],
        "支払利息の遅延アクルーアル",
    )
    result = _close(conn, run_id, d1)
    item = next(r for r in result["reclose"] if r["date"] == d0)
    assert item["restated"] is True  # NAV は動く
    assert item["recon_invalidated"] is False  # が、建玉は動いていない


def test_reclose_fills_watermark_for_snapshots_without_entries(conn, run_id):
    """仕訳が 1 本も無い日も初回パスで水位を埋め、恒久 stale を止める(新-4)。"""
    old_day = date(2020, 1, 6)  # シード仕訳(2026)より前 = entry_date <= old_day が空
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES ('DEMO_FUND', %s, 0, 'confirmed', '{}'::jsonb)
            """,
            (old_day,),
        )
    first = _close(conn, run_id, _G_DAYS[0])
    assert old_day in [r["date"] for r in first["reclose"]]

    second = _close(conn, run_id, _G_DAYS[1])
    assert old_day not in [r["date"] for r in second["reclose"]]  # 2 回目は対象外
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM ledger.nav_snapshots "
            "WHERE book_id = 'DEMO_FUND' AND snap_date = %s",
            (old_day,),
        )
        detail = cur.fetchone()[0]
    assert detail["producer"]["input_refs"]["ledger.journal_entries.max_entry_id"] == 0
    # 締めのたびに 1 件ずつ伸びない(元の行に producer が無いため履歴も生えない)。
    assert detail.get("producer_history", []) == []


def test_reclose_reports_missing_nav_daily_row(conn, run_id):
    """nav_snapshots だけ動いて nav_daily の行が無い日は、合成せず痕跡を返す。"""
    d0, d1 = _G_DAYS[0], _G_DAYS[1]
    _close(conn, run_id, d0)
    with conn.cursor() as cur:  # 執行段を経ていない日を模す
        cur.execute("DELETE FROM risk.nav_daily WHERE nav_date = %s", (d0,))
    _post_contribution(conn, run_id, d0, Decimal(5_000_000))

    result = _close(conn, run_id, d1)
    restated = [r for r in result["reclose"] if r["restated"]]
    assert restated[0]["nav_daily_missing"] is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM risk.nav_daily WHERE nav_date = %s", (d0,))
        assert cur.fetchone()[0] == 0  # 合成しない(執行照合を経ていない confirmed を作らない)


def test_close_no_positions_confirms_seed_nav(conn, run_id, today_jst):
    """注文が無い日も締めは走り、NAV(出資金のみ)を確定する(daily の no-op 経路)。"""
    result = run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    assert result["nav"] == Decimal(10_000_000)
    assert result["status"] == "confirmed"
    rows = _nav_daily_rows(conn)
    assert len(rows) == 1 and rows[0][2] == "confirmed"


def test_close_upsert_same_day(conn, run_id, passed_order, insert_bar, today_jst):
    """同日再締めは上書き(1 行のまま)。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    assert len(_nav_daily_rows(conn)) == 1


# ── 照合: 一致・不一致の検出 ─────────────────────────────────────────────────
def test_reconcile_executions_matched(conn, run_id, passed_order, insert_bar, today_jst):
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    recon = reconcile_executions(conn, book_id="DEMO_FUND", date=today_jst)
    assert recon["matched"] is True
    assert recon["executions"]["count"] == 1 and recon["ledger"]["count"] == 1
    assert recon["executions"]["gross"] == Decimal("100064.00")
    assert recon["ledger"]["gross"] == Decimal("100064.00")


def test_reconcile_detects_unposted_execution(
    conn, run_id, passed_order, insert_bar, today_jst
):
    """ledger 仕訳の無い約定(記帳漏れ)を件数・金額ブレイクとして検出する。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    # 2 件目: record_execution だけ行い post_fill を意図的に飛ばす(記帳漏れの再現)。
    order2 = passed_order(ref_price=Decimal(1000))
    advance_order_status(conn, order2, "submitted")
    record_execution(
        conn, order_id=order2, qty=Decimal(100), price=Decimal(1000),
        executed_at=datetime.combine(today_jst, time(15, 30), tzinfo=JST),
        run_id=run_id,
    )

    breaks: list[dict] = []
    recon = reconcile_executions(
        conn, book_id="DEMO_FUND", date=today_jst, on_break=breaks.append
    )
    assert recon["matched"] is False
    items = {b["item"] for b in recon["breaks"]}
    assert items == {"exec_count", "exec_gross"}  # fee は両側 0 で一致
    assert len(breaks) == 2 and breaks[0]["book_id"] == "DEMO_FUND"


def test_close_break_leaves_nav_provisional(
    conn, run_id, passed_order, insert_bar, today_jst
):
    """照合ブレイク時は risk.nav_daily を provisional に留める(NAV 確定しない)。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    order2 = passed_order(ref_price=Decimal(1000))
    advance_order_status(conn, order2, "submitted")
    record_execution(  # 記帳漏れ + ポジション乖離(executions 側だけ 200 株になる)
        conn, order_id=order2, qty=Decimal(100), price=Decimal(1000),
        executed_at=datetime.combine(today_jst, time(15, 30), tzinfo=JST),
        run_id=run_id,
    )

    breaks: list[dict] = []
    result = run_demo_close(
        conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id, on_break=breaks.append
    )
    assert result["status"] == "provisional"
    assert result["exec_recon"]["matched"] is False
    assert breaks  # 執行照合+ポジション照合の両方から通知が出る
    rows = _nav_daily_rows(conn)
    assert rows[0][2] == "provisional"


def test_close_fails_loudly_without_price(conn, run_id, passed_order, insert_bar, today_jst):
    """保有銘柄の終値が無ければ締めは明確な例外で失敗する(黙ってスキップしない)。"""
    import pytest

    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    with conn.cursor() as cur:  # 当日バーを消して評価不能にする(rollback で巻き戻る)
        cur.execute("DELETE FROM market.bars WHERE instrument_id = 1")
    with pytest.raises(ValueError, match="終値が無い"):
        run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)


def test_reconcile_scopes_by_date_and_book(conn, run_id, passed_order, insert_bar, today_jst):
    """照合は JST 日付と帳簿でスコープされる(他日・他帳簿の約定を混ぜない)。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    from datetime import timedelta

    other_day = today_jst - timedelta(days=1)
    recon = reconcile_executions(conn, book_id="DEMO_FUND", date=other_day)
    assert recon["executions"]["count"] == 0 and recon["ledger"]["count"] == 0
    assert recon["matched"] is True


def test_nav_daily_requires_known_book(conn, run_id, today_jst):
    """risk.nav_daily は ledger.books への FK — 帳簿語彙の外には書けない。"""
    import psycopg
    import pytest

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO risk.nav_daily (book_id, nav_date, nav, status, run_id)
                    VALUES ('NO_SUCH_BOOK', %s, 0, 'provisional', %s)
                    """,
                    (today_jst, run_id),
                )


def test_close_records_mtm_via_ledger_api(conn, run_id, passed_order, insert_bar, today_jst):
    """評価差損益は ledger の期末評価替え(post_mark_to_market)流儀で記帳される。"""
    _fill_one(conn, run_id, passed_order, insert_bar, today_jst)
    run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    with conn.cursor() as cur:
        # 取得 100,064 → 時価 100,000: unrealized_pnl 借方 64(評価損)。
        cur.execute(
            """
            SELECT COALESCE(sum(debit - credit), 0) FROM ledger.journal_lines
            WHERE book_id = 'DEMO_FUND' AND account_id = 'unrealized_pnl'
            """
        )
        assert Decimal(cur.fetchone()[0]) == Decimal("64.00")
        # 証券勘定は時価に一致。
        cur.execute(
            """
            SELECT COALESCE(sum(debit - credit), 0) FROM ledger.journal_lines
            WHERE book_id = 'DEMO_FUND' AND account_id = 'securities'
            """
        )
        assert Decimal(cur.fetchone()[0]) == Decimal("100000.00")


def test_close_uses_utc_now_not_needed(conn, run_id, today_jst):
    """(回帰)締めは date 引数のみに依存し、実行時刻に依存しない。"""
    r1 = run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    r2 = run_demo_close(conn, book_id="DEMO_FUND", date=today_jst, run_id=run_id)
    assert r1["nav"] == r2["nav"] and r1["status"] == r2["status"]
