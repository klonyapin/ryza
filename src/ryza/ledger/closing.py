"""日次締め(設計書 §5 のシーケンス)。

run_daily_close:
  1. 未記帳の約定を検出して記帳(冪等: 記帳済み fill はスキップ)
  2. 全ポジションを終値で評価替え(price_snapshot を evidence 化)。全売却済みの銘柄も
     対象にして前日 MTM の残渣をゼロへ落とす(独立審査 新-10)
  3. アクルーアル(当面は手数料のみ。金利は TODO)
  4. NAV 算出 → nav_snapshots に provisional で保存
  5. recon の照合結果が全件 matched なら confirmed に更新、不一致なら provisional のまま

reclose_stale(独立審査 重要-2 / 再審査 再-1):
  締めが走った**後**に同じ日付で立った仕訳は当日のスナップショットに入らない。対象日を
  「直近 N 営業日」という固定窓で決めると、窓の外に落ちた仕訳が窓境界に恒久的な偽
  リターンを立てる(再審査の実測: 対照 `[0,0,0]` に対し固定窓 N=3 は `[+0.5,0,0]`)。
  そこで**水位検出**を採る — 各スナップショットは自分が見た仕訳の水位
  (``detail.producer.input_refs``)を持つので、水位より後ろの ``entry_id`` を持つ
  遅延仕訳がある日を 1 クエリで列挙し、その日だけ再計算する。窓の縁が存在しない。

nav_snapshots のリネージ(不変原則3):
  ``detail`` は jsonb であり、``detail.producer`` に producer_job / run_id /
  code_version / as_of / input_refs(仕訳の水位)を書く。既存列で足りるためスキーマ
  変更(保護領域)は不要。水位は遅延仕訳の検出器そのものでもある(再審査 再-5)。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

from ryza.ledger import _util, posting, recon, statements

_log = logging.getLogger(__name__)

# 帳簿 -> trade.order_intents.track の対応
_BOOK_TRACK = {"DEMO_FUND": "demo", "LIVE_FUND": "live"}

# nav_snapshots.detail.producer.job に記録するジョブ名(リネージの追跡単位)。
_JOB_DAILY_CLOSE = "ledger.closing.run_daily_close"
_JOB_RECLOSE = "ledger.closing.reclose_stale"

#: ``detail.producer.input_refs`` の水位キー。``entry_id`` は IDENTITY(単調増加)なので
#: 「そのスナップショットが見た仕訳」と「後から立った仕訳」を厳密に切り分けられる
#: (タイムスタンプより強い — 独立審査 再-5)。
WATERMARK_KEY = "ledger.journal_entries.max_entry_id"

#: 確定 NAV の書き換え(restatement)を urgent で上げる古さ(営業日)。当日〜数日の訂正は
#: 締めの正常な運用だが、これより古い日の書き換えは「既に外部へ報告済みの値が動く」意味を
#: 持つため必ず目立たせる(上限や承認は設けない — 是正を止めるより可視化を優先する)。
#: 営業日は祝日カレンダーではなくスナップショットの実績数で数える(締めが走った日=営業日)。
RESTATEMENT_URGENT_BUSINESS_DAYS = 5

# price_source は callable(instrument_id)->price、または dict{instrument_id: price}
PriceSource = Callable[[int], Any] | dict[int, Any]

#: 再締め用の**過去日**価格ソース: (instrument_id, date) -> **その日の**終値 | None。
#: 当日の締め(``PriceSource``)と違い日付を取る — 再締めは過去日の評価替えを
#: その日の終値で打ち直すため。実装は**その日のバーだけ**を見ること(遡り取得は
#: 別日の終値で評価しながら priced_at にその日を書く虚偽の証憑を作る — 独立審査 新-6)。
#: 終値が無いときは例外ではなく ``None`` を返すこと(当日の締めは評価不能を例外で
#: 止めるが、再締めが過去日の欠測で当日の締めごと落ちるのは fail-safe の向きが逆)。
HistoricalPriceSource = Callable[[int, _date], Any | None]

#: 価格ソースを持たない呼び出し用の明示的な縮退ソース。**既定値ではない** —
#: ``reclose_stale`` の ``price_source`` は必須引数であり、渡し忘れが是正済みの日を
#: 黙って取得原価へ戻すことを型で防ぐ(独立審査 新-8)。評価替えを打ちようがない
#: 文脈(価格を持たないテスト・OPS 帳簿)では、これを**明示的に**渡す。
def no_price(instrument_id: int, day: _date) -> None:
    """常に None を返す価格ソース(明示的な縮退経路)。"""
    return None


def _price_of(price_source: PriceSource, instrument_id: int) -> Any:
    if callable(price_source):
        return price_source(instrument_id)
    return price_source[instrument_id]


def _recorded_fill_ids(conn: psycopg.Connection, book_id: str) -> set[int]:
    """既に記帳済みの trade fill_id の集合(broker_fill 証憑の payload から抽出)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.payload_ref
            FROM ledger.journal_entries je
            JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
            WHERE je.book_id = %s AND e.kind = 'broker_fill'
            """,
            (book_id,),
        )
        recorded: set[int] = set()
        for (text,) in cur.fetchall():
            try:
                fid = json.loads(text).get("fill_id")
            except (ValueError, TypeError):
                continue
            if fid is not None:
                recorded.add(int(fid))
    return recorded


def _record_unrecorded_fills(
    conn: psycopg.Connection, book_id: str, date: _date, run_id: int
) -> list[int]:
    """trade.fills のうち未記帳のものを検出して記帳する。冪等。記帳した entry_id を返す。

    fill -> order -> intent の連鎖で track(=帳簿)と instrument/side を解決する。
    OPS 帳簿や、track 対応の無い帳簿では何もしない。
    """
    track = _BOOK_TRACK.get(book_id)
    if track is None:
        return []

    recorded = _recorded_fill_ids(conn, book_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.fill_id, oi.instrument_id, oi.side, f.qty, f.price, f.fee,
                   f.filled_at::date
            FROM trade.fills f
            JOIN trade.orders o ON o.order_id = f.order_id
            JOIN trade.order_intents oi ON oi.intent_id = o.intent_id
            WHERE oi.track = %s
            ORDER BY f.fill_id
            """,
            (track,),
        )
        pending = cur.fetchall()

    entry_ids: list[int] = []
    for fill_id, instrument_id, side, qty, price, fee, filled_date in pending:
        if fill_id in recorded:
            continue
        norm_side = "buy" if side in ("buy", "long") else "sell"
        entry_ids.append(
            posting.post_fill(
                conn,
                book_id=book_id,
                instrument_id=instrument_id,
                side=norm_side,
                qty=qty,
                price=price,
                fee=fee or 0,
                entry_date=filled_date or date,
                run_id=run_id,
                fill_id=fill_id,
                source="trade.fills",
                posted_by="ledger.closing",
            )
        )
    return entry_ids


def _zero_qty_writeoff_row(book_value: Decimal, entry_id: int | None) -> dict[str, Any]:
    """全売却後の残渣を洗い替えた記録(当日経路・再締め経路で**共通のスキーマ**)。

    独立審査 新-16: 同じ事象を当日は ``detail.zero_qty_writeoffs``、再締めは
    ``mtm_reapplied.positions`` の中の数量ゼロ行という別々の語彙で書いていたため、
    ``positions`` を建玉明細として読む下流が再締め経路でだけ幽霊行を見ていた。
    ``positions`` は**建玉のある銘柄だけ**にし、洗い替えは両経路ともこのキーへ出す。

    ``entry_id`` は仕訳を書いた経路(当日締め)だけが持ち、再締めは ``None``
    (再締めは仕訳を書かず集計内で調整する — 再-6)。null は「書いていない」の明示。
    """
    return {
        "qty": "0",
        "price": None,  # 数量ゼロは終値を引いていない(存在しない価格を書かない)
        "market_value": "0",
        "book_value": str(book_value),  # 評価替えが作った残高 = 洗い替えた額
        "entry_id": entry_id,
    }


def run_daily_close(
    conn: psycopg.Connection,
    *,
    book_id: str,
    date: _date,
    price_source: PriceSource,
    run_id: int,
    broker_snapshot: dict[str, Any] | None = None,
    broker: str = "sim",
    on_break: recon.BreakCallback | None = None,
) -> dict[str, Any]:
    """日次締めを実行し、要約 dict を返す。

    戻り値: {nav, status, marked, fills_recorded, recon, zero_qty_writeoffs,
    unexplained_residue}。``unexplained_residue`` が空でない日は呼び出し側が通知すること
    (会計の説明不能な残高であり、放置すると偽リターンになる — 独立審査 新-15)。
    その中身は**原価恒等式の破れ**である: 各銘柄について「原価勘定 ``securities`` の残高」と
    「建玉イベントの再生が返す取得原価」が一致しない状態であり、``reason`` は数量ゼロなら
    ``zero_qty_residue``、建玉が残っているなら ``cost_identity_broken``。
    """
    bt = _util.book_type(conn, book_id)

    # 1. 未記帳の約定を検出して記帳(冪等)
    fills_recorded = _record_unrecorded_fills(conn, book_id, date, run_id)

    # 2. 全ポジションを終値で評価替え(ファンド帳簿のみ)
    marked: list[int] = []
    positions_detail: dict[str, Any] = {}
    writeoffs: dict[str, Any] = {}
    unexplained: dict[str, Any] = {}
    if bt == "fund":
        for iid in _util.held_instruments(conn, book_id):
            # 数量も帳簿価額も同じ as_of で切る(独立審査 新-13)。数量だけ全期間再生に
            # すると、将来日付の売りが先に記帳されている日の締めが「数量ゼロ ⇒ 残渣」と
            # 誤判定して実在の建玉を消す(実測: returns [-0.0196] ← 真値 [0.0])。
            qty, cost = _util.replay_position(conn, book_id, iid, as_of=date)
            # 全売却済みの銘柄も評価替えの対象にする(独立審査 新-10)。売りは取得原価
            # ぶんしか securities を取り崩さないため、評価替えで積んだ「時価 − 取得原価」が
            # 残渣として資産に残り、NAV が**恒久的に過大**になる(審査実測: 残高 200,000 /
            # 数量 0、returns [+0.0196, 0.0] ← 真値 [0, 0])。数量ゼロの時価は価格に依らず
            # ゼロなので終値は引かない(建玉の無い銘柄の終値を要求すると締めごと落ちる)。
            price = None if qty == 0 else _util.to_decimal(_price_of(price_source, iid))
            written_off = (
                _util.mtm_book_value(conn, book_id, iid, as_of=date)
                if price is None else Decimal(0)
            )
            entry_id = posting.post_mark_to_market(
                conn,
                book_id=book_id,
                instrument_id=iid,
                price=price,
                entry_date=date,
                run_id=run_id,
                posted_by=_util.MTM_POSTED_BY[0],
            )
            if entry_id is not None:
                marked.append(entry_id)
            # 原価恒等式(0034 の勘定分離が可能にした検査 — docs/design/11 §3.2):
            # 評価調整を別勘定へ出したので、原価勘定の残高は「建玉イベント(約定・現物
            # 拠出)が積んだ原価」だけになった。したがって再生した原価と一致すべきであり、
            # 破れは**評価替えの経路を一切参照せずに**検出できる。破る側に落ちるのは
            # 数量つき証憑を伴わない直接記帳(評価替えを騙る手仕訳・逆仕訳のオペミス・
            # 未対応の株式分割など)であり、まさに独立審査 新-14 / 新-15 が挙げた事象である。
            # 分離前は同じ式が書けなかった(原価と評価調整が同居し、差し引きに推定が要った)。
            cost_balance = _util.securities_cost_value(conn, book_id, iid, as_of=date)
            if cost_balance != cost:
                unexplained[str(iid)] = {
                    "book_value": str(cost_balance),
                    "replay_cost": str(cost),
                    "qty": str(qty),
                    "reason": (
                        "zero_qty_residue" if qty == 0 else "cost_identity_broken"
                    ),
                }
            if price is None:
                # 残渣が無ければ仕訳は立たない(entry_id None)= 記録することも無い。
                if entry_id is not None:
                    writeoffs[str(iid)] = _zero_qty_writeoff_row(written_off, entry_id)
                continue
            positions_detail[str(iid)] = {
                "qty": str(qty),
                "price": str(price),
                "market_value": str(qty * price),
            }
    if unexplained:
        _log.warning(
            "%s %s: 原価勘定の残高が建玉再生の取得原価と一致しない銘柄がある"
            "(説明不能な残渣 — 数量つき証憑を伴わない直接記帳、逆仕訳のオペミス、"
            "未対応のコーポレートアクションを疑う): %s",
            book_id, date.isoformat(), unexplained,
        )

    # 3. アクルーアル: 当面は手数料のみ(約定時に計上済み)。
    #    TODO: 金利(信用取引の支払利息 interest_expense / 貸株料など)の日次アクルーアル。

    # 4. NAV 算出(= 資産 − 負債)→ nav_snapshots に provisional で保存
    totals = statements.book_totals(conn, book_id, date)
    nav = totals["nav"]
    detail = {
        "assets": str(totals["assets"]),
        "liabilities": str(totals["liabilities"]),
        "net_income": str(totals["net_income"]),
        "positions": positions_detail,
        "priced_at": date.isoformat(),
    }
    # 全売却後の残渣を洗い替えた銘柄(数量ゼロなので positions とは語彙を分ける)。
    # 再締め経路(_reapply_mtm)も同じキー・同じ行スキーマで書く(独立審査 新-16)。
    if writeoffs:
        detail["zero_qty_writeoffs"] = writeoffs
    if unexplained:
        detail["unexplained_residue"] = unexplained
    _upsert_nav(conn, book_id, date, nav, "provisional", detail, run_id)

    # 5. ブローカー照合。全件 matched なら confirmed に更新。
    recon_result = None
    status = "provisional"
    if broker_snapshot is not None:
        recon_result = recon.reconcile(
            conn,
            book_id=book_id,
            date=date,
            broker_snapshot=broker_snapshot,
            run_id=run_id,
            broker=broker,
            on_break=on_break,
        )
        if recon_result.all_matched:
            _upsert_nav(conn, book_id, date, nav, "confirmed", detail, run_id)
            status = "confirmed"

    return {
        "nav": nav,
        "status": status,
        "marked": marked,
        "fills_recorded": fills_recorded,
        "recon": recon_result,
        "zero_qty_writeoffs": writeoffs,
        "unexplained_residue": unexplained,
    }


def _entries_watermark(conn: psycopg.Connection, book_id: str, as_of: _date) -> int:
    """NAV の入力になった仕訳の水位(``entry_date <= as_of`` の最大 entry_id)。

    リネージの input_refs(不変原則3)。同じ日付でも「どこまでの仕訳を見た値か」が
    残るため、締め後に立った仕訳による NAV の変化を後から説明できる。

    仕訳が 1 本も無い日は **0**(NULL ではない)を書く。NULL のままだと
    ``stored_watermark IS NULL`` で毎回 stale と判定され、値も通知も変わらないのに
    ``producer_history`` だけが締めのたびに伸び続ける(独立審査 新-4)。0 は
    「見た仕訳はゼロ件」を表す正しい水位であり、以後の比較にもそのまま使える。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(entry_id) FROM ledger.journal_entries "
            "WHERE book_id = %s AND entry_date <= %s",
            (book_id, as_of),
        )
        row = cur.fetchone()
    return 0 if row is None or row[0] is None else int(row[0])


def _producer(
    conn: psycopg.Connection, book_id: str, as_of: _date, run_id: int, job: str
) -> dict[str, Any]:
    """生成物のリネージ(不変原則3: producer_job / code_version / input_refs / as_of)。

    ``code_version`` は ``meta.runs`` が唯一の記録元なので run_id から引く(締め側で
    git を叩き直すと 2 つの真実ができる)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT code_version FROM meta.runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return {
        "job": job,
        "run_id": int(run_id),
        "code_version": row[0] if row else None,
        "as_of": as_of.isoformat(),
        "input_refs": {WATERMARK_KEY: _entries_watermark(conn, book_id, as_of)},
        "written_at": datetime.now(UTC).isoformat(),
    }


def _upsert_nav(
    conn: psycopg.Connection,
    book_id: str,
    snap_date: _date,
    nav: Decimal,
    status: str,
    detail: dict[str, Any],
    run_id: int,
    *,
    job: str = _JOB_DAILY_CLOSE,
    prior_producer: dict[str, Any] | None = None,
) -> None:
    """nav_snapshots を upsert する(同日再締めは上書き。provisional→confirmed の更新に対応)。

    ``detail.producer`` に書き手のリネージを載せる — 「いつの締めが作った値か」を
    後から辿れるようにする(不変原則3)。detail は jsonb なので列の追加は不要。

    ``prior_producer`` を渡すと ``detail.producer_history`` の末尾に積む。再締めが
    2 回以上走った日でも痕跡が連鎖する(独立審査 再-8: 上書きで最初の書き手が消える)。
    当日の締め(provisional→confirmed の 2 度書き)は同じ run による確定過程であり
    restatement ではないため履歴に積まない。
    """
    if prior_producer is not None:
        history = detail.get("producer_history")
        history = list(history) if isinstance(history, list) else []
        history.append(prior_producer)
        detail = {**detail, "producer_history": history}
    detail = {**detail, "producer": _producer(conn, book_id, snap_date, run_id, job)}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (book_id, snap_date)
            DO UPDATE SET nav = EXCLUDED.nav, status = EXCLUDED.status, detail = EXCLUDED.detail
            """,
            (book_id, snap_date, nav, status, json.dumps(detail)),
        )


#: 遅延仕訳のある日(stale 日)を 1 クエリで列挙する。各スナップショットが記録した水位
#: ``detail.producer.input_refs`` と、いま同じ条件(``entry_date <= snap_date``)で測った
#: 水位を比べ、後者が進んでいる日 = 締めの後に仕訳が立った日である。``entry_id`` は
#: IDENTITY で単調、``journal_entries`` は追記オンリー(``forbid_mutation``)なので
#: 「進んだ」以外の差は原理的に起きない。水位を持たない日(本機能より前に書かれた
#: スナップショット)は判定材料が無いため stale 扱いにして水位を埋める(fail-safe)。
#:
#: ``age_business_days`` は「その日より後に締めが走った回数」= 営業日の実績カウント。
#: 祝日カレンダーを持ち込まずに restatement の古さを測る(通知の材料性判定に使う)。
#:
#: ``position_changing_late`` は遅延仕訳が**建玉を動かすか**(``jl.instrument_id`` を持つ
#: 明細を含む仕訳が遅れて立ったか)。ポジション照合はその日の建玉に対して行われるので、
#: 建玉が変われば締め時点の照合結論は無効になる(独立審査 再-2 → ``recon_invalidated``)。
#:
#: 判定は**行レベル・建玉性**で行う。仕訳単位で「拠出資本勘定に触れない仕訳か」を見る
#: 述語は両方向に誤る(独立審査 新-1): 現物拠出(securities 借 / capital 貸)は 1 仕訳に
#: capital 行を含むため建玉が増えるのに判定から漏れ、逆に建玉を一切動かさない費用
#: アクルーアル(interest_expense / cash)は「拠出資本に触れない」だけで無効判定になる。
#: 実測でこの行レベル述語は 現物拠出=True / 費用=False / 出資=False / 約定=True と分離する。
#:
#: 水位が無い日は ``entry_id > NULL`` が NULL になり判定が false — 判定材料が
#: 無い日に照合無効を主張しない(restatement 判定と同じ姿勢)。
#:
#: **形(2026-08-04 の書き換え — reminders reclose-stale-pruning)**。意味は上のとおりで
#: 変えていない。変えたのは**同じ述語をどう評価させるか**である。旧形はスナップショット
#: 1 日ごとに ``max(entry_id) WHERE entry_date <= snap_date`` の相関サブクエリを回して
#: いたため、コストが スナップショット数 × 仕訳数 で伸びた(実測: 規模A 778.6 ms /
#: 規模B 8,371.1 ms = 行数 10 倍で 10.7 倍)。索引では直らないことは実測で確定している
#: (``migrations/0027_query_indexes.sql`` 索引2 の「効かない用途」節: プランナが
#: 「主キーの逆走査 + フィルタ + LIMIT 1」を選ぶため、どの列組み合わせの索引も使われない)。
#:
#: 新形は**日ごとに 1 回だけ集約してから走査する**。``entry_day`` が日付ごとの
#: ``max(entry_id)`` を 1 パスで作り、各スナップショットはその小さな集合(日数ぶんの行)に
#: 対して ``max`` を取る。``max(entry_id) WHERE entry_date <= d`` を「日ごとの max の
#: max」に分解しただけなので値は恒等的に等しい。
#:
#: **枝刈りは建玉性の判定にかける**。``position_changing_late`` は stale と判定された日に
#: しか要らないので、``pos_day`` は ``stale`` が確定してからその範囲だけを集約する
#: (``entry_id > min(stale.stored_watermark)`` / ``entry_date <= max(stale.snap_date)``)。
#: stale がゼロなら ``pos_day`` は空になり、建玉性の走査そのものが起きない。
#: ``EXISTS(∃e: date<=d ∧ id>W)`` を ``max{id : date<=d} > W`` に書き換えているが、
#: 最大値が閾値を超えることと超える要素が存在することは同値なので判定は変わらない。
#: 枝刈りの下限も同値性を壊さない — ある日 d が使う要素は ``id > W_d ≥ min(W)`` を
#: 満たすので、``id > min(W)`` で落とした行は d の判定に影響しない。
#:
#: 実測(0027 と同じ合成データ・中央値7回。遅延仕訳なし = 通常日 / 全日 stale = 最悪):
#: 規模A(仕訳 31,402 行・スナップショット 787 日)788.0 → 28.9 ms / 2,950.7 → 83.6 ms、
#: 規模B(仕訳 314,002 行・同 787 日)8,558.5 → 46.1 ms(**186x**)/ 53,817.9 → 150.3 ms。
#: 残るコストは ``entry_day`` の 1 パス集約(規模B で約 41 ms)であり、行数に線形。
#: スナップショット数 × 仕訳数 の掛け算は消えている。
#:
#: **採らなかった枝刈り(重要)**: reminders と 0027 のコメントが挙げていた案
#: 「全スナップショットの ``stored_watermark`` の**最大値**より後ろの ``entry_id`` を持つ
#: 最古の ``entry_date`` を求め、その日以降のスナップショットだけを候補にする」は
#: **検出漏れを起こすため採用していない**。反例: 9/1 の締め(水位 5)の後に 9/1 付けの
#: 仕訳(entry_id 50)が立ち、その状態で 9/2 の締めが走る(水位 100)。9/1 は
#: ``50 > 5`` で stale だが、最大水位 100 より後ろの仕訳は存在しないので候補が空になり、
#: **遅延仕訳を取り込んだ日が永久に再締めされない**。これは本機能が存在する理由そのもの
#: であり、速さのために落としてよい検出ではない。同じ形で健全にするには閾値を水位の
#: **最小値**にするしかなく、最小値は最古のスナップショットの水位なので枝刈りが効かない。
#: 上の分解(日ごと集約)は近似ではなく同値なので、この選択が要らない。
#: 反例は ``tests/ledger/test_stale_query_rewrite.py`` が対照テストで固定している。
_STALE_SNAPSHOTS_SQL = """
WITH snap AS (
    SELECT s.snap_date,
           (s.detail -> 'producer' -> 'input_refs' ->> %(wm_key)s)::bigint
               AS stored_watermark,
           row_number() OVER (ORDER BY s.snap_date DESC) - 1 AS age_business_days
    FROM ledger.nav_snapshots s
    WHERE s.book_id = %(book)s AND s.snap_date <= %(through)s
), entry_day AS MATERIALIZED (
    SELECT je.entry_date, max(je.entry_id) AS day_max
    FROM ledger.journal_entries je
    WHERE je.book_id = %(book)s AND je.entry_date <= %(through)s
    GROUP BY je.entry_date
), measured AS (
    SELECT snap.snap_date, snap.stored_watermark, snap.age_business_days,
           (SELECT max(d.day_max) FROM entry_day d
             WHERE d.entry_date <= snap.snap_date) AS current_watermark
    FROM snap
), stale AS MATERIALIZED (
    SELECT * FROM measured
    WHERE stored_watermark IS NULL
       OR coalesce(current_watermark, 0) > stored_watermark
), pos_day AS MATERIALIZED (
    SELECT je.entry_date, max(je.entry_id) AS day_max
    FROM ledger.journal_entries je
    WHERE je.book_id = %(book)s
      AND je.entry_date <= (SELECT max(snap_date) FROM stale)
      AND je.entry_id > (SELECT min(stored_watermark) FROM stale)
      AND EXISTS (
              SELECT 1 FROM ledger.journal_lines jl
              WHERE jl.entry_id = je.entry_id AND jl.instrument_id IS NOT NULL
          )
    GROUP BY je.entry_date
)
SELECT stale.snap_date, stale.stored_watermark, stale.current_watermark,
       stale.age_business_days,
       coalesce(
           (SELECT max(p.day_max) FROM pos_day p
             WHERE p.entry_date <= stale.snap_date) > stale.stored_watermark,
           false
       ) AS position_changing_late
FROM stale
ORDER BY stale.snap_date
"""

#: 再締めが打ち直さないもの(証憑としての snapshot に必ず書く注記 — 独立審査 再-6)。
_RESTATEMENT_NOTE = (
    "再締めは記帳済み仕訳の集計のみをやり直す(MTM は打ち直さない)。"
    "遅延約定を含む日の建玉は取得原価のままで時価ではない。"
)

#: MTM を集計内で再適用できた日の注記(独立審査 新-3 の是正)。
_RESTATEMENT_NOTE_MTM = (
    "再締めは記帳済み仕訳の集計をやり直し、建玉は as_of リプレイで復元した"
    "その日の数量をその日の終値で評価替えして NAV に反映した(仕訳は書かない)。"
)

#: 前回の再適用値を引き継いだ日の注記(独立審査 新-7)。
_RESTATEMENT_NOTE_MTM_CARRIED = (
    "再締めは記帳済み仕訳の集計をやり直したが、その日のバーが無いため評価替えは"
    "再計算できず、前回の再締めが求めた評価差額をそのまま引き継いだ"
    "(取得原価へは戻さない)。建玉はその後の遅延仕訳を反映していない可能性がある。"
)


def _reapply_mtm(
    conn: psycopg.Connection,
    book_id: str,
    snap_date: _date,
    price_source: HistoricalPriceSource,
) -> dict[str, Any] | None:
    """``snap_date`` 時点の建玉を復元し、その日の終値で評価替えした **NAV 調整額**を返す。

    **仕訳は書かない**(独立審査 新-3 の是正における制約): 過去日付への新規記帳は
    ``journal_entries`` の水位を進めて自分自身を stale にし、再締めが毎日仕訳を積む
    無限ループになる。評価替えは集計の中だけで行い、結果を ``detail`` に証憑として残す。

    数量は ``_util.replay_position(as_of=snap_date)``、帳簿価額は
    ``_util.securities_book_value(as_of=snap_date)`` — どちらも同じ日付境界で切るので、
    差分 ``時価 − 帳簿価額`` は ``post_mark_to_market`` がその日に打ったはずの delta と
    一致する(NAV = 資産 − 負債 なので、その delta をそのまま NAV に足せばよい)。

    **数量ゼロの銘柄も対象**(独立審査 新-10): その日までに全売却された銘柄は、売りが
    取得原価ぶんしか securities を取り崩さないため前日 MTM の残渣を持つ。時価は価格に
    依らずゼロなので**終値は引かず**(バーの有無も問わない — 建玉ゼロの銘柄の欠測で
    その日の再適用を諦めるのは過剰)、``delta = 0 − mtm_book_value`` を足して残渣を消す。
    消すのは**評価替えが作った残高だけ**であり securities の総額ではない(``replay_position``
    は broker_fill しか再生しないため、現物拠出など約定外の建玉が数量ゼロに見える)。
    ``run_daily_close`` の当日経路と同じ定義であり、片方だけ直すと当日と再締めで NAV が
    食い違う。

    戻り値 ``{"delta", "positions", "zero_qty_writeoffs", "priced_at"}``。``positions`` は
    **建玉のある銘柄だけ**、数量ゼロの洗い替えは当日経路と同じスキーマで
    ``zero_qty_writeoffs`` に出す(独立審査 新-16)。**建玉のある銘柄で 1 つでもその日の
    バーが無い日は None**(部分適用は「原価でも時価でもない NAV」を作るため、その日は
    再適用しない — 呼び出し側が前回値の引き継ぎか ``mtm_not_reapplied`` を選ぶ)。
    """
    positions: dict[str, Any] = {}
    writeoffs: dict[str, Any] = {}
    delta = Decimal(0)
    for iid in _util.held_instruments(conn, book_id):
        qty, _cost = _util.replay_position(conn, book_id, iid, as_of=snap_date)
        if qty == 0:
            # 消すのは評価替えが作った残高だけ(約定外の securities には触れない)。
            book_value = _util.mtm_book_value(conn, book_id, iid, as_of=snap_date)
            if book_value == 0:
                continue  # その日は未保有かつ残渣なし
            delta -= book_value
            writeoffs[str(iid)] = _zero_qty_writeoff_row(book_value, entry_id=None)
            continue
        raw = price_source(iid, snap_date)
        if raw is None:
            return None
        price = _util.to_decimal(raw)
        market_value = qty * price
        book_value = _util.securities_book_value(conn, book_id, iid, as_of=snap_date)
        delta += market_value - book_value
        positions[str(iid)] = {
            "qty": str(qty),
            "price": str(price),
            "market_value": str(market_value),
            "book_value": str(book_value),
        }
    return {
        "delta": delta,
        "positions": positions,
        "zero_qty_writeoffs": writeoffs,
        "priced_at": snap_date.isoformat(),
    }


def reclose_stale(
    conn: psycopg.Connection,
    *,
    book_id: str,
    through: _date,
    run_id: int,
    price_source: HistoricalPriceSource,
) -> list[dict[str, Any]]:
    """締めの後に仕訳が立った日(stale 日)を水位で検出し、その日だけ再計算する。

    **なぜ固定窓ではないか**(独立審査 再-1): 「直近 N 営業日」で対象を決めると、窓の
    外に落ちた遅延仕訳がスナップショットに永久に入らず、窓の境界に恒久的な偽日次
    リターンを立てる。しかも 1 度報告した後は差分が無くなるため以後は無言になる
    (実測: 対照 ``[0,0,0]`` に対し N=3 は ``[+0.5,0,0]``)。水位検出は「見た仕訳より
    後ろの仕訳があるか」を各日について直接問うため、窓の縁そのものが存在しない。

    **上書き条件は「NAV 変化 OR 水位変化」**(独立審査 再-4): 手数料ゼロの遅延約定の
    ように NAV が動かない仕訳でも水位は進む。NAV 等値でスキップすると水位が古いまま
    残り、同じ日を毎日 stale と誤検出し続ける(かつ本当の遅延を見分けられない)。

    **MTM の再適用**(独立審査 新-3 の是正): 集計だけをやり直すと、遅延**約定**が入った
    日の建玉が取得原価のまま残り、翌日の締めが時価に打ち直した瞬間に「約定日の値洗い差 ×
    数量 / NAV」の**恒久的な偽リターン**が立つ(審査実測: 市場 1200 の日に 1000 で 1000 株を
    遅延記帳 → 真値 ``[0.0]`` に対し観測 ``[+0.020]``、翌日以降も訂正されない)。そこで
    ``price_source`` が渡された日は、``_util.replay_position(as_of=snap_date)`` でその日
    時点の建玉を復元し、その日の終値で評価替えした差分を **集計内で** NAV に足す。

    **仕訳は書かない**: ``post_mark_to_market`` を過去日付で呼ぶ経路は作らない。新-13 で
    数量も ``as_of`` で切るようにしたので「その日に存在しなかった建玉を過去日付で記帳する」
    危険は消えたが、再-6 のもう一方の理由は生きている — 過去日への新規記帳は水位を進めて
    自分自身を stale にし、再締めが毎日仕訳を積む無限ループになる。再適用の根拠は
    ``detail.mtm_reapplied`` に建玉・終値・帳簿価額として残す。

    **対象は ``recon_invalidated`` と同じ集合**(遅延仕訳に建玉行を含む日、および過去の
    再締めで一度でもそのフラグが立った日)。それ以外の日の MTM は締め時点の建玉に対して
    正しく打たれており、打ち直す理由が無い。

    **その日のバーが 1 銘柄でも欠ける日**は再適用しない(部分適用で「原価でも時価でもない
    NAV」を作らない)。このとき:

    - 前回の再締めが求めた評価差額があれば**それを引き継ぐ**(``mtm_carried_forward``)。
      再適用は仕訳を残さないので、引き継がずに集計だけをやり直すと NAV が取得原価へ
      revert して偽リターンが復活する(独立審査 新-7 — 実測で確認された欠陥)
    - 引き継ぐ値も無ければ ``detail.mtm_not_reapplied`` を立てて取得原価のままにする

    ``price_source`` は**必須引数**である(独立審査 新-8): 既定値を持たせると渡し忘れた
    呼び出しが是正済みの日を黙って取得原価へ戻す。評価替えを打ちようがない文脈では
    ``no_price`` を明示的に渡すこと。

    **status / restated / recon_invalidated の定義**(独立審査 再-2 に対する裁定):

    - ``status``(provisional/confirmed)= **締め時点の照合の結論**。執行照合と
      ポジション照合が一致したかどうかの記録であり、後から書き換えない(列の語彙を
      動かすと risk・ダッシュボードの読み手すべてに波及するため)
    - ``detail.restated`` = **その後に判明した会計訂正**。確定 NAV が動いた事実
    - ``detail.recon_invalidated`` = **訂正により照合結論が無効化された**。遅延仕訳に
      拠出資本以外(遅延約定など)が含まれる日は、その日の建玉自体が締め時点と違う
      ため ``status='confirmed'`` を「照合済み」として読んではならない。リスク日次は
      この日を breaks 相当で urgent 通知する

    古い日の restatement は ``urgent_restatements`` が urgent 通知の対象として拾う。

    戻り値(日付昇順): ``[{date, nav_before, nav_after, status, restated, late_entries,
    recon_invalidated, mtm_reapplied, mtm_carried_forward, mtm_pending,
    age_business_days, watermark_before, watermark_after}, ...]``。
    ``restated`` または ``recon_invalidated`` が True の日は呼び出し側が必ず通知すること。
    ``mtm_pending`` は「評価替えが要るのに当日バーが無く取得原価のまま」の日であり、
    ``recon_invalidated`` と違い**当該 run 限りでなく累積の状態**を表す(通知の分岐に使う)。
    """
    with conn.cursor() as cur:
        cur.execute(
            _STALE_SNAPSHOTS_SQL,
            {"book": book_id, "through": through, "wm_key": WATERMARK_KEY},
        )
        stale = cur.fetchall()

    # 評価替えの対象はファンド帳簿だけ(運営帳簿に建玉は無い — run_daily_close と同じ条件)。
    can_reapply = _util.book_type(conn, book_id) == "fund"

    results: list[dict[str, Any]] = []
    for snap_date, stored_wm, current_wm, age, position_changing_late in stale:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nav, status, detail FROM ledger.nav_snapshots "
                "WHERE book_id = %s AND snap_date = %s",
                (book_id, snap_date),
            )
            prev_nav, status, prev_detail = cur.fetchone()
        prev_nav = _util.to_decimal(prev_nav)
        prev_detail = prev_detail if isinstance(prev_detail, dict) else {}
        totals = statements.book_totals(conn, book_id, snap_date)

        # 水位を持たない日(本機能より前のスナップショット)は「遅延仕訳があった」と
        # 断定できない。NAV も変わらないならリネージの後追い記録だけを行い、
        # restatement としては扱わない — 起きていない訂正を主張しないため。
        late_entries = stored_wm is not None and (current_wm or 0) > stored_wm
        recon_invalidated = bool(late_entries and position_changing_late)

        # 建玉が後から変わった日だけ、その日の建玉と終値で評価替えを打ち直す(新-3)。
        # 判定には**過去の再締めで立ったフラグも含める**: 再適用は仕訳を書かないので、
        # 2 回目の再締め(例: 同じ日に拠出資本だけが遅れて立つ)が「今回は建玉が動いて
        # いない」として集計だけをやり直すと、前回の評価替えが NAV から消えて偽リターンが
        # 復活する。``recon_invalidated`` は一度立ったら下ろさないので判定材料に使える。
        invalidated_ever = bool(recon_invalidated or prev_detail.get("recon_invalidated"))
        mtm = (
            _reapply_mtm(conn, book_id, snap_date, price_source)
            if (can_reapply and invalidated_ever)
            else None
        )
        # 今回再評価できなかった日でも、前回の再適用値があれば引き継ぐ(新-7)。
        carried = mtm is None and invalidated_ever
        if carried:
            mtm = _carry_forward_mtm(prev_detail, run_id)
            carried = mtm is not None
        nav_from_journals = totals["nav"]
        if mtm is not None:
            totals = _with_mtm(totals, _util.to_decimal(mtm["delta"]))
        nav = totals["nav"]
        restated = nav != prev_nav
        detail = (
            _restated_detail(
                prev_detail, totals, run_id, prev_nav, nav,
                recon_invalidated=recon_invalidated, mtm=mtm, carried=carried,
                nav_from_journals=nav_from_journals,
            )
            if (late_entries or restated)
            else dict(prev_detail)
        )

        _upsert_nav(
            conn, book_id, snap_date, nav, status, detail, run_id,
            job=_JOB_RECLOSE, prior_producer=prev_detail.get("producer"),
        )
        results.append(
            {
                "date": snap_date,
                "nav_before": prev_nav,
                "nav_after": nav,
                "status": status,
                "restated": restated,
                "late_entries": late_entries,
                "recon_invalidated": recon_invalidated,
                "mtm_reapplied": mtm is not None and not carried,
                "mtm_carried_forward": carried,
                "mtm_pending": bool(invalidated_ever and mtm is None),
                "age_business_days": int(age),
                "watermark_before": stored_wm,
                "watermark_after": current_wm,
            }
        )
    return results


def _carry_forward_mtm(prev_detail: dict[str, Any], run_id: int) -> dict[str, Any] | None:
    """前回の再締めが求めた評価差額を引き継ぐ(独立審査 新-7)。無ければ None。

    再適用は**仕訳を残さない**ため、評価差額はスナップショットの ``detail`` にしか無い。
    当日バーが欠測した回の再締めが引き継がずに集計だけをやり直すと、NAV は取得原価へ
    revert し、一度消したはずの偽リターンが復活する(実測: 10,200,000 → 9,999,999)。

    引き継いだ建玉・終値は**前回のもの**であり、その後の遅延仕訳を反映していない可能性が
    ある。値を黙って使い回すのではなく ``carried_forward`` / ``carried_forward_by_run`` で
    証憑に明記し、通知側が区別できるようにする。
    """
    prev = prev_detail.get("mtm_reapplied")
    if not isinstance(prev, dict) or prev.get("delta") is None:
        return None
    return {
        **prev,
        "carried_forward": True,
        "carried_forward_by_run": int(run_id),
    }


def _with_mtm(totals: dict[str, Decimal], delta: Decimal) -> dict[str, Decimal]:
    """評価替えの差分を集計値に載せる(``post_mark_to_market`` の仕訳と同じ効果)。

    Dr securities / Cr unrealized_pnl は資産と収益を同額動かす。NAV = 資産 − 負債 なので
    NAV も同額動く。負債・資本は動かない。
    """
    if delta == 0:
        return totals
    return {
        **totals,
        "assets": totals["assets"] + delta,
        "income": totals["income"] + delta,
        "net_income": totals["net_income"] + delta,
        "nav": totals["nav"] + delta,
    }


def _restated_detail(
    prev_detail: dict[str, Any],
    totals: dict[str, Decimal],
    run_id: int,
    nav_before: Decimal,
    nav_after: Decimal,
    *,
    recon_invalidated: bool = False,
    mtm: dict[str, Any] | None = None,
    carried: bool = False,
    nav_from_journals: Decimal | None = None,
) -> dict[str, Any]:
    """再締め後の ``detail``。古い建玉明細を**残さず**訂正の事実を書く(独立審査 再-2/再-3)。

    再計算するのは集計値(``book_totals``)だけなので、``positions``(締め時点の建玉と
    評価額)は再締め後の NAV と整合しない。嘘のデータを残すより落として
    ``positions_stale`` を立てるほうが証憑として正しい。``reclose`` は 2 回目以降の
    訂正で最初の ``nav_before`` が消えないよう配列で追記する(独立審査 再-8)。

    ``recon_invalidated`` は**一度立ったら下ろさない**(勘定を戻す仕訳が来ても、その日の
    照合が締め時点の建玉に対して行われた事実は変わらない)。ただし不可逆なフラグをその
    まま毎日の urgent 条件にすると「一度でも遅延約定が起きたら永久に赤」になるため
    (中-5 で是正済みの欠陥の再発 — 独立審査 新-2)、**いつの再締めで立った/更新された
    か**を ``recon_invalidated_by_run`` に残し、通知側が鮮度で絞れるようにする。

    ``mtm`` を渡した日は評価替えを反映した日であり、``mtm_not_reapplied`` は False に
    なって根拠(建玉・終値・帳簿価額)が ``mtm_reapplied`` に載る。``positions_stale`` は
    **True のまま**にする — ``positions`` キーは締め時点の建玉スナップショットの語彙であり、
    再締めが復元した as_of 建玉を同じキーに書くと読み手が両者を区別できなくなる
    (再適用の建玉は ``mtm_reapplied.positions`` にある)。``carried`` は前回値の引き継ぎ
    (今回は再計算していない — 独立審査 新-7)。

    ``nav_from_journals`` は**評価替えを載せる前**の仕訳集計そのままの NAV(独立審査 新-9)。
    再適用は仕訳を書かないので、これが無いとスナップショットの ``nav`` を試算表合計と
    突合できず、複式簿記で最も強い監査不変式が検証不能になる(不変原則3)。
    ``nav = nav_from_journals + mtm_reapplied.delta`` が常に成り立つ。
    """
    history = prev_detail.get("reclose")
    history = list(history) if isinstance(history, list) else []
    history.append(
        {
            "nav_before": str(nav_before),
            "nav_after": str(nav_after),
            "at": datetime.now(UTC).isoformat(),
            "run_id": int(run_id),
            "reason": "締め後に立った仕訳の取り込み(独立審査 重要-2)",
        }
    )
    note = (
        _RESTATEMENT_NOTE if mtm is None
        else _RESTATEMENT_NOTE_MTM_CARRIED if carried
        else _RESTATEMENT_NOTE_MTM
    )
    detail = {k: v for k, v in prev_detail.items() if k != "positions"}
    detail.update(
        assets=str(totals["assets"]),
        liabilities=str(totals["liabilities"]),
        net_income=str(totals["net_income"]),
        positions_stale=True,
        mtm_not_reapplied=mtm is None,
        restatement_note=note,
        restated=True,
        restated_at=datetime.now(UTC).isoformat(),
        restated_by_run=int(run_id),
        reclose=history,
    )
    # 仕訳集計そのままの NAV(評価替えを載せる前)。試算表合計との突合点(新-9)。
    if nav_from_journals is not None:
        detail["nav_from_journals"] = str(nav_from_journals)
    if mtm is not None:
        detail["mtm_reapplied"] = {
            "delta": str(mtm["delta"]),
            "positions": mtm["positions"],
            # 数量ゼロの洗い替えは当日経路と同じキー・同じ行スキーマ(独立審査 新-16)。
            # 引き継ぎ(carried)のときは前回の再締めが書いた内容がそのまま乗る。
            "zero_qty_writeoffs": mtm.get("zero_qty_writeoffs", {}),
            "priced_at": mtm["priced_at"],
            # 引き継ぎ時は**実際に計算した run** を残す(値の出どころを指すため)。
            "run_id": int(mtm["run_id"]) if carried else int(run_id),
            **({"carried_forward": True,
                "carried_forward_by_run": int(run_id)} if carried else {}),
        }
    else:
        detail.pop("mtm_reapplied", None)
    # 既に立っている日を再締めが触った(= 水位が動いた)ときも「更新」として run を打ち直す。
    if recon_invalidated or prev_detail.get("recon_invalidated"):
        detail["recon_invalidated"] = True
        detail["recon_invalidated_by_run"] = int(run_id)
    return detail


def urgent_restatements(reclosed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """urgent で上げるべき restatement(``RESTATEMENT_URGENT_BUSINESS_DAYS`` より古い日)。

    しきい値は通知の材料性基準であり IPS の判定値ではない(``risk.navflow`` の
    ``PENDING_MATERIALITY_NAV`` と同じ位置づけ)。当日〜数営業日の訂正は締めの正常な
    運用だが、それより古い日の確定 NAV が動くのは既報値の書き換えである。
    """
    return [
        r for r in reclosed
        if r["restated"] and r["age_business_days"] > RESTATEMENT_URGENT_BUSINESS_DAYS
    ]


__all__ = [
    "RESTATEMENT_URGENT_BUSINESS_DAYS",
    "WATERMARK_KEY",
    "HistoricalPriceSource",
    "no_price",
    "reclose_stale",
    "run_daily_close",
    "urgent_restatements",
]
