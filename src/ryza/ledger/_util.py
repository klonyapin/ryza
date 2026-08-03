"""会計エンジン内部共通ヘルパー。

証憑(evidence)の作成、meta.runs の作成、Decimal 変換、勘定科目メタの取得、
移動平均法によるポジション再生(fills の再生)を提供する。

- 金額はすべて Decimal で扱う(numeric 列と対応)。
- 証憑作成(``create_evidence``)は T-003 の証憑ストア(``ryza.provenance.evidence``)経由に
  統合済み(T-005)。環境変数 ``RYZA_EVIDENCE_DIR`` があれば ``EvidenceStore(LocalStorage(そのパス))``
  で不変保存 + sha256 改竄検知 + 重複排除を行う。未設定時は設計書 §5 補足に従い、小さな内部記録は
  payload_ref に JSON をインライン格納する(kind='decision' 等の内部記録はインライン許容)。
- ``replay_position`` はどちらの経路の証憑でも payload を復元して読む。
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from datetime import UTC, datetime
from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

from ryza.provenance.evidence import EvidenceStore, LocalStorage

CODE_VERSION = "T-002"

#: 取得原価を積む勘定(約定・現物拠出)。**評価調整はここに入らない**(0034 の分離)。
COST_ACCOUNT = "securities"

#: 評価替え(MTM)の累計を積む独立勘定。残渣の同定はこの**残高そのもの**であり、
#: 仕訳の自由記入列に対する述語での推定ではない(独立審査 新-14 の構造的根治。
#: 判断の全文は docs/design/11-mtm-account-separation.md)。
MTM_ACCOUNT = "securities_mtm"

#: 建玉の数量を再生できる証憑の kind(``replay_position`` の対象)。
#: ``in_kind_contribution`` は約定を経ない建玉(現物拠出)に数量を持たせるための
#: 証憑であり、これを再生対象に含めないと当該建玉は数量ゼロに見えて**一度も
#: 評価替えされない**(独立審査 新-17)。
POSITION_EVIDENCE_KINDS: tuple[str, ...] = ("broker_fill", "in_kind_contribution")

# 証憑ストア経由で保存された payload_ref の URI スキーム(インライン格納との判別に使う)。
_STORE_URI_SCHEMES = ("file://", "gs://")


@functools.lru_cache(maxsize=8)
def _evidence_store_for(evidence_dir: str) -> EvidenceStore:
    """RYZA_EVIDENCE_DIR に対応する EvidenceStore を返す(パス単位でキャッシュ)。"""
    return EvidenceStore(LocalStorage(evidence_dir))


def _evidence_store() -> EvidenceStore | None:
    """環境変数 ``RYZA_EVIDENCE_DIR`` があれば証憑ストアを、無ければ None を返す。

    None のときは create_evidence が従来どおり payload_ref に JSON をインライン格納する。
    """
    evidence_dir = os.environ.get("RYZA_EVIDENCE_DIR")
    if not evidence_dir:
        return None
    return _evidence_store_for(evidence_dir)


def to_decimal(x: Any) -> Decimal:
    """int/float/str/Decimal を Decimal に変換する。float は文字列経由で精度劣化を避ける。"""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(str(x))
    return Decimal(x)


def _now() -> datetime:
    return datetime.now(UTC)


def create_run(
    conn: psycopg.Connection,
    job_name: str,
    *,
    code_version: str = CODE_VERSION,
    params: dict | None = None,
    status: str = "success",
) -> int:
    """meta.runs に実行記録を作り run_id を返す。全書き込みのリネージの鍵。"""
    with conn.cursor() as cur:
        now = _now()
        cur.execute(
            """
            INSERT INTO meta.runs (job_name, code_version, started_at, finished_at,
                                   status, params)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING run_id
            """,
            (job_name, code_version, now, now, status, json.dumps(params or {})),
        )
        return cur.fetchone()[0]


def _canonical_json(payload: Any) -> str:
    """dict/list は決定論的 JSON 文字列に、str はそのまま返す。"""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _store_payload(payload: Any) -> bytes | dict[str, Any] | list[Any]:
    """証憑ストア(``EvidenceStore.store``)が受ける型に正規化する。

    dict/list/bytes はそのまま(dict/list は store 側が決定論的 JSON 化)。str は utf-8 バイト列に。
    それ以外は決定論的 JSON バイト列にする。
    """
    if isinstance(payload, (bytes, dict, list)):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return _canonical_json(payload).encode("utf-8")


def create_evidence(
    conn: psycopg.Connection,
    *,
    kind: str,
    payload: Any,
    source: str,
    retrieved_at: datetime | None = None,
) -> int:
    """証憑行を作成し evidence_id を返す。

    ``RYZA_EVIDENCE_DIR`` が設定されていれば T-003 の証憑ストア経由で保存する
    (不変保存 + sha256 改竄検知 + 重複排除。retrieved_at はストアが設定するため無視)。
    未設定時は小さな内部記録として JSON を payload_ref にインライン格納し、
    sha256 は JSON バイト列に対して計算する(設計書 §5 補足)。
    """
    store = _evidence_store()
    if store is not None:
        return store.store(conn, kind, _store_payload(payload), source)

    text = _canonical_json(payload)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING evidence_id
            """,
            (kind, text, digest, source, retrieved_at or _now()),
        )
        return cur.fetchone()[0]


def load_evidence_payload(
    conn: psycopg.Connection, evidence_id: int, payload_ref: str
) -> Any | None:
    """証憑の payload を JSON として復元する(ストア経由・インラインの両対応)。

    payload_ref が証憑ストアの URI(file://|gs://)なら実体を取得して json.loads し、
    そうでなければインライン JSON としてそのまま json.loads する。復元不能時は None。
    """
    if payload_ref.startswith(_STORE_URI_SCHEMES):
        store = _evidence_store()
        if store is not None:
            try:
                return json.loads(store.get(conn, evidence_id).decode("utf-8"))
            except (ValueError, TypeError, KeyError, OSError):
                return None
        return None
    try:
        return json.loads(payload_ref)
    except (ValueError, TypeError):
        return None


def resolve_evidence(
    conn: psycopg.Connection,
    evidence: int | dict | None,
    *,
    default_kind: str = "decision",
    default_source: str = "ledger",
) -> int:
    """evidence を evidence_id に解決する。

    - int: 既存 evidence_id としてそのまま返す
    - dict: {kind, payload, source} から新規作成
    - None: ValueError(証憑必須)
    """
    if evidence is None:
        raise ValueError("証憑(evidence)が必要です: 仕訳は evidence_id 必須")
    if isinstance(evidence, int):
        return evidence
    if isinstance(evidence, dict):
        return create_evidence(
            conn,
            kind=evidence.get("kind", default_kind),
            payload=evidence.get("payload", evidence),
            source=evidence.get("source", default_source),
        )
    raise TypeError(f"evidence は int/dict/None のいずれか: {type(evidence)!r}")


def book_type(conn: psycopg.Connection, book_id: str) -> str:
    """帳簿の book_type('fund'|'ops')を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT book_type FROM ledger.books WHERE book_id = %s", (book_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"未知の帳簿: {book_id}")
    return row[0]


def account_meta(conn: psycopg.Connection, book_id: str) -> dict[str, dict[str, str]]:
    """勘定科目 account_id -> {name, category} のマップを返す。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, name, category FROM ledger.accounts WHERE book_id = %s",
            (book_id,),
        )
        return {r[0]: {"name": r[1], "category": r[2]} for r in cur.fetchall()}


def cash_account(bt: str) -> str:
    """帳簿種別に対応する現金勘定 ID。"""
    return "cash" if bt == "fund" else "cash_bank"


def replay_position(
    conn: psycopg.Connection,
    book_id: str,
    instrument_id: int,
    *,
    as_of: _date | None = None,
) -> tuple[Decimal, Decimal]:
    """記帳済みの建玉イベントを再生し、移動平均法の (保有数量, 取得原価合計) を返す。

    対象は ``POSITION_EVIDENCE_KINDS`` の証憑 — 約定(``broker_fill``)と現物拠出
    (``in_kind_contribution``)である。

    - buy / 現物拠出: qty と cost(=qty*price、手数料は別途費用計上のため原価に含めない)を加算
    - sell: 数量を減らし、取得原価を平均原価分だけ取り崩す
    逆仕訳(reversal_of)された建玉イベントと、逆仕訳エントリ自体は除外する。
    MTM(price_snapshot)は原価に影響しないため、ここでは対象外。

    ``as_of`` を渡すと ``entry_date <= as_of`` の約定だけを再生する(既定 None = 全期間 —
    従来挙動)。**なぜ必要か**(独立審査 新-3): 全期間再生の数量を使って過去日付の評価替えを
    打つと「その日に存在しなかった建玉」を過去日付で記帳してしまうため、再締めは MTM を
    打ち直せず、遅延約定のあった日の建玉が取得原価のまま残って恒久的な偽リターンを立てる。
    ``as_of`` はその日時点の建玉を point-in-time(不変原則4)で復元する手段である。

    逆仕訳の除外も ``as_of`` で切る(``r.entry_date <= as_of``)。``securities_book_value``
    の as_of は逆仕訳の**明細**を日付で落とすので、数量側だけ日付を無視して取り消すと
    「時価 − 帳簿価額」の差分が両者の非対称から生じる — 評価替えの差分計算が壊れる。

    **現物拠出の扱い**(独立審査 新-17 の是正): 以前は ``broker_fill`` しか再生しなかった
    ため、約定を経ずに建った securities(現物拠出 Dr securities / Cr capital)は**数量ゼロに
    見え、一度も評価替えされなかった**(審査実測: 終値 1500/2000/500 を渡しても残高は拠出額
    のまま、``detail.positions`` にも出ない)。是正は「建玉の真実をどこに持たせるか」の選択で
    あり、**拠出時に数量つきの証憑を要求する**方を採った(``posting.post_in_kind_contribution``
    が ``in_kind_contribution`` 証憑を作る)。建玉イベント表を新設する案は、``trade.fills`` と
    証憑という既存の 2 つの真実に 3 つ目を足すことになるため採らない。

    **限界: 再生できるのは証憑を持つ経路だけである。** 数量つき証憑を伴わない手仕訳
    (``Dr securities / Cr capital`` を kind='decision' で立てる等)はここに現れない。それは
    黙って評価から漏れるのではなく、締めの原価恒等式(``securities 残高 = ここが返す原価``)を
    破って ``unexplained_residue`` に名指しで出る(``ledger.closing``)。株式分割・併合・
    現物払戻は未対応であり、同じく恒等式を破る側に落ちる。
    """
    sql = """
        SELECT e.kind, je.evidence_id, e.payload_ref
        FROM ledger.journal_entries je
        JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
        WHERE je.book_id = %s
          AND e.kind = ANY(%s)
          AND je.reversal_of IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ledger.journal_entries r
              WHERE r.reversal_of = je.entry_id
                AND (%s::date IS NULL OR r.entry_date <= %s)
          )
          AND (%s::date IS NULL OR je.entry_date <= %s)
        ORDER BY je.entry_id
    """
    with conn.cursor() as cur:
        cur.execute(
            sql, (book_id, list(POSITION_EVIDENCE_KINDS), as_of, as_of, as_of, as_of)
        )
        rows = cur.fetchall()

    qty = Decimal(0)
    cost = Decimal(0)
    for kind, evidence_id, payload_ref in rows:
        fill = load_evidence_payload(conn, evidence_id, payload_ref)
        if not isinstance(fill, dict):
            continue
        if int(fill.get("instrument_id", -1)) != int(instrument_id):
            continue
        f_qty = to_decimal(fill["qty"])
        f_price = to_decimal(fill["price"])
        # 現物拠出は取得(買い)と同じ向きで建玉を積む。売り方向の現物払戻は未対応
        # (証憑 kind を分けて side を持たせる拡張になる)。
        side = "buy" if kind == "in_kind_contribution" else fill["side"]
        if side == "buy":
            qty += f_qty
            cost += f_qty * f_price
        elif side == "sell":
            if qty <= 0:
                continue
            released = cost * f_qty / qty
            cost -= released
            qty -= f_qty
    return qty, cost


def held_instruments(conn: psycopg.Connection, book_id: str) -> list[int]:
    """建玉勘定(原価 ``securities`` / 評価調整 ``securities_mtm``)に明細を持つ全銘柄 ID。

    評価調整勘定も見るのは、原価がゼロでも評価調整の残渣だけが残る銘柄を締めの視界から
    落とさないため(全売却済み銘柄の洗い替え対象 — 独立審査 新-10)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT instrument_id
            FROM ledger.journal_lines
            WHERE book_id = %s AND account_id = ANY(%s) AND instrument_id IS NOT NULL
            ORDER BY instrument_id
            """,
            (book_id, [COST_ACCOUNT, MTM_ACCOUNT]),
        )
        return [r[0] for r in cur.fetchall()]


def _account_instrument_balance(
    conn: psycopg.Connection,
    book_id: str,
    instrument_id: int,
    accounts: list[str],
    as_of: _date | None,
) -> Decimal:
    """指定勘定・指定銘柄の残高(debit − credit)。逆仕訳は貸借の相殺で自然に落ちる。"""
    sql = """
        SELECT COALESCE(sum(jl.debit - jl.credit), 0)
        FROM ledger.journal_lines jl
        JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
        WHERE jl.book_id = %s AND jl.account_id = ANY(%s)
          AND jl.instrument_id = %s
    """
    params: list[Any] = [book_id, accounts, instrument_id]
    if as_of is not None:
        sql += " AND je.entry_date <= %s"
        params.append(as_of)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return to_decimal(cur.fetchone()[0])


def securities_cost_value(
    conn: psycopg.Connection,
    book_id: str,
    instrument_id: int,
    *,
    as_of: _date | None = None,
) -> Decimal:
    """``securities`` 勘定(**取得原価のみ**)の当該銘柄残高。

    0034 の勘定分離により、この値は「建玉イベント(約定・現物拠出)が積んだ原価」だけを
    含む。したがって ``replay_position`` が返す原価と**一致すべき恒等式**が成立し、締めが
    毎日検査できる(``ledger.closing`` の ``unexplained_residue``)。分離前はこの式が
    書けなかった — 評価調整が同じ勘定に混ざっており、突合には推定で差し引く必要があった。
    """
    return _account_instrument_balance(
        conn, book_id, instrument_id, [COST_ACCOUNT], as_of
    )


def securities_book_value(
    conn: psycopg.Connection,
    book_id: str,
    instrument_id: int,
    *,
    as_of: _date | None = None,
) -> Decimal:
    """当該銘柄の帳簿価額 = **原価勘定 + 評価調整勘定**(borrow=debit-credit)。MTM 反映後は時価。

    0034 の分離後も**この関数の意味は変えていない**(呼び出し側 — 評価替えの差分計算・
    recon の評価額突合・再締めの再適用 — が見たいのは常に「いまの帳簿価額」であるため)。
    変わったのは内訳が 2 勘定に分かれたことだけである。
    """
    return _account_instrument_balance(
        conn, book_id, instrument_id, [COST_ACCOUNT, MTM_ACCOUNT], as_of
    )


#: 評価替え仕訳の ``posted_by``。``post_mark_to_market`` と DB トリガ
#: (``ledger.check_mtm_line`` — migrations/0034)がこの値以外での評価調整勘定への記帳を
#: 拒否する。0034 の勘定分離以降、これは**読み取り時の判定子ではなく書き込み時のガード**
#: である(残渣の同定は ``mtm_book_value`` = 勘定残高が行う)。
#:
#: **分離しても外してはならない**(docs/design/11-mtm-account-separation.md §5.2-1): 分離は
#: 新-14 の攻撃を塞がず、宛先を ``securities`` から ``securities_mtm`` へ移すだけである。
#: ``Dr securities_mtm / Cr capital`` を締めジョブ名で立てれば、次の締めが同額を洗い替えて
#: 偽の未実現損を立てる — 審査実測(a)(NAV 13,000,000→10,000,000)と同じ結果が同じ手順で
#: 再現する。``posted_by`` は ``post_entry`` の呼び出し側が決める列なので、**これは防御で
#: あって境界ではない**。構造的に断つには DB ロール分離(締め専用ロール + ``current_user``
#: を見るトリガ)が要るが、単一ロール前提のインフラ全体に波及するため採っていない。
MTM_POSTED_BY: tuple[str, ...] = ("ledger.closing",)


def mtm_book_value(
    conn: psycopg.Connection,
    book_id: str,
    instrument_id: int,
    *,
    as_of: _date | None = None,
) -> Decimal:
    """評価調整勘定 ``securities_mtm`` の当該銘柄残高 = **評価替えが作った残高**。

    全売却後の「残渣」を消す量はこれであって、``securities_book_value`` の総額ではない
    (独立審査 新-10 の是正を実装する過程で判明した危険): 総額を消しに行くと、約定を経ずに
    建った建玉(現物拠出)の原価まで帳簿から消える(実測: 現物拠出 1,000,000 の日の NAV が
    丸ごと戻り、``restated`` が False になる = 訂正が消える)。

    **0034 以降これは推定ではない。** 分離前は同じ量を「``securities`` 勘定のうち
    ``evidence.kind='price_snapshot'`` かつ ``posted_by ∈ MTM_POSTED_BY`` の行の合計」という
    仕訳の自由記入列に対する述語で**推定**しており、判定子を騙る手仕訳が実在資産を消したり
    (審査実測 NAV 13,000,000→10,000,000)、無から NAV を増やしたり(同 10,500,000)できた
    — 独立審査 新-14。いまは勘定残高そのものなので判定子が無い。

    逆仕訳は貸方に同額を立てるため残高で自然に相殺する(分離前に必要だった逆仕訳の
    除外ロジックは不要になった)。ここに現れない数量ゼロの残高、および原価恒等式
    (``securities`` 残高 = ``replay_position`` の原価)の破れは ``closing.run_daily_close``
    が ``unexplained_residue`` として毎締めに検出・記録する(新-15)— 黙って消すのでも
    黙って残すのでもなく、名指しして残す。
    """
    return _account_instrument_balance(
        conn, book_id, instrument_id, [MTM_ACCOUNT], as_of
    )
