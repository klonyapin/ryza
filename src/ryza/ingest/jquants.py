"""ingest.jquants — J-Quants API **V2**（日足・財務・銘柄マスタ）。

Free プランで開始する（必要時に予算承認の上 Standard 化）。J-Quants は 2026-06-01 に
V1 を廃止し V2 へ移行済み（V1 の ``/v1/token/auth_refresh`` は HTTP 410 Gone）。V2 は
**API キー認証**（``x-api-key`` ヘッダ）で、リフレッシュトークン→idToken の 2 段認証は
廃止された。API キーは Secret Manager の ``jquants-api-key``、無ければ環境変数
``RYZA_JQUANTS_API_KEY`` フォールバック。

取込対象（V2 エンドポイント）:
- 銘柄マスタ（``/v2/equities/master``）→ ``market.instruments``（SCD2 自動登録）
- 日足（``/v2/equities/bars/daily``）→ ``market.bars``（timeframe='1d', source='jquants'）
- 財務（``/v2/fins/summary``）→ ``docs.documents``（source_type='filing'）

V2 ではレスポンスのカラム名が短縮された（例 ``Open``→``O``、``Volume``→``Vo``、
``DisclosedDate``→``DiscDate``）。トップレベルは ``{"data": [...]}`` に統一され、
``pagination_key`` でページ送りする（全ページを連結して返す）。

**Free プランの取得可能日付（Issue #38）**: Free プランは直近 12 週の日足が提供されず、
窓内の日付を ``date`` に指定すると HTTP 400 が返る（2026-08-03 の VM daily 実走で確認。
域内の過去日付では同一リクエスト形式で 200）。``effective_quote_date`` が要求日付を
「今日 − ``_PLAN_LAG_DAYS``」以前の平日へ丸めてから取得する。有償プラン移行時は
``--lag-days 0`` を指定する。

HTTP は ``Fetcher`` 越し（テストはモック）。日足の各バーは証憑（API 生レスポンス）への
リネージ辺を張る。

実行: ``python -m ryza.ingest.jquants [--date YYYY-MM-DD] [--lag-days N]``
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.base import Fetcher
from ryza.provenance import EvidenceStore, Run, record
from ryza.provenance import run as run_ctx
from ryza.secrets import probe_secret

_API_BASE = "https://api.jquants.com"
SOURCE = "jquants"
SOURCE_NAME = "J-Quants"

# Free プランは直近 12 週（84 日）の日足が取得できず、窓内の日付指定は HTTP 400 になる
# （モジュール docstring 参照）。データ反映タイミングの揺れ・境界日ズレを吸収するため
# +7 日のマージンを置く（12 週遅延データの分析用途で 1 週の追加遅延は実害なし）。
_PLAN_LAG_DAYS = 91


class JQuantsAuthError(RuntimeError):
    """API キー未設定。"""


def api_key() -> str:
    """API キーを取得する（env 優先 → Secret Manager ``jquants-api-key``）。

    env ``RYZA_JQUANTS_API_KEY`` / ``JQUANTS_API_KEY`` を優先し、無ければ VM（GCE）上で
    Secret Manager ``jquants-api-key`` を取得する（``ryza.secrets.load_secret``、
    Issue #30）。Secret ``jquants-refresh-token`` も登録されているが V2 は API キー認証
    のみのため使用しない（V1 の遺物）。
    """
    res = probe_secret(
        env=("RYZA_JQUANTS_API_KEY", "JQUANTS_API_KEY"), secret="jquants-api-key"
    )
    if not res.value:
        # 理由（env 未設定/GCP_PROJECT 不明/Secret 取得失敗）を daily の skip 理由へ
        # 可視化する（Issue #38）。
        raise JQuantsAuthError(f"J-Quants API キー未設定: {res.reason}")
    return res.value


def _auth_headers(key: str) -> dict[str, str]:
    return {"x-api-key": key}


def _fetch_all(
    fetcher: Fetcher,
    path: str,
    *,
    key: str,
    params: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """V2 エンドポイントを ``pagination_key`` で全ページ取得し ``data`` を連結して返す。

    V2 は全エンドポイントで ``{"data": [...], "pagination_key": ...}`` 形。次ページが
    無くなる（``pagination_key`` が返らない）まで繰り返す。
    """
    url = f"{_API_BASE}{path}"
    query = dict(params or {})
    out: list[dict[str, Any]] = []
    while True:
        resp = fetcher.fetch(url, params=query, headers=_auth_headers(key))
        if not resp.ok:
            # エラーボディ（J-Quants は message を返す）を含め、プラン範囲外・
            # パラメータ不正等の切り分けをログだけで可能にする（Issue #38）。
            body = resp.body[:200].decode("utf-8", errors="replace")
            raise RuntimeError(f"{path} 失敗: status={resp.status} body={body}")
        payload = resp.json()
        out.extend(payload.get("data", []))
        next_key = payload.get("pagination_key")
        if not next_key:
            break
        query["pagination_key"] = next_key
    return out


def effective_quote_date(
    requested: date, *, today: date | None = None, lag_days: int = _PLAN_LAG_DAYS
) -> date:
    """プラン遅延を考慮した実効取得日を返す（Issue #38）。

    ``requested`` と「今日 − ``lag_days``」の古い方を取り、土日なら直前の金曜へ繰り
    下げる（土日指定は常に空データのため）。祝日は繰り下げない（API は営業日以外を
    空データで返すだけでエラーにはならず、取引カレンダー依存を持ち込まない）。
    ``lag_days=0`` で遅延なしプラン（Standard 等）の当日取得に戻る。
    """
    today = today if today is not None else date.today()
    eff = min(requested, today - timedelta(days=lag_days))
    while eff.weekday() >= 5:  # 5=土, 6=日
        eff -= timedelta(days=1)
    return eff


def _normalize_symbol(code: str) -> str:
    """J-Quants の Code（5 桁, 例 '72030'）を TSE ティッカー（'7203.T'）に正規化。"""
    code = str(code).strip()
    if len(code) == 5 and code.endswith("0"):
        code = code[:4]
    return f"{code}.T"


# ────────────────────────────────────────────────────────────────────────────
# 銘柄マスタ（/v2/equities/master）
# ────────────────────────────────────────────────────────────────────────────
def fetch_listed_info(fetcher: Fetcher, key: str) -> list[dict[str, Any]]:
    """上場銘柄一覧を取得する。"""
    return _fetch_all(fetcher, "/v2/equities/master", key=key)


def ingest_instruments(
    conn: psycopg.Connection,
    records: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """銘柄マスタを ``market.instruments`` に反映（SCD2 自動登録）。

    既存銘柄はそのまま、未登録銘柄のみ新規行を作る。``{'resolved': n, 'created': m}``。
    """
    as_of = as_of or datetime.now(UTC)
    created = 0
    for rec in records:
        symbol = _normalize_symbol(rec.get("Code", ""))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM market.instruments "
                "WHERE symbol = %s AND valid_to IS NULL LIMIT 1",
                (symbol,),
            )
            existed = cur.fetchone() is not None
        base.resolve_instrument(
            conn, symbol, asset_class="equity", venue="TSE",
            currency="JPY", as_of=as_of,
        )
        if not existed:
            created += 1
    return {"resolved": len(records), "created": created}


# ────────────────────────────────────────────────────────────────────────────
# 日足（/v2/equities/bars/daily）
# ────────────────────────────────────────────────────────────────────────────
def fetch_daily_quotes(
    fetcher: Fetcher, key: str, quote_date: str
) -> list[dict[str, Any]]:
    """指定日の全銘柄日足を取得する（Free プランは 12 週遅延だが構造は同じ）。"""
    return _fetch_all(
        fetcher, "/v2/equities/bars/daily", key=key, params={"date": quote_date}
    )


def _num(rec: dict[str, Any], key: str) -> float | None:
    v = rec.get(key)
    return float(v) if v is not None else None


def ingest_daily_quotes(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    quotes: list[dict[str, Any]],
    *,
    quote_date: str,
    raw_response: dict[str, Any] | list[Any] | bytes | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """日足を ``market.bars`` に書き込む（冪等・証憑・リネージ込み）。

    V2 のカラム名（``O/H/L/C/Vo``）を読む。バーの ``ts`` は当日 00:00 UTC。各バー行は
    生 API レスポンス（証憑）へのリネージ辺を張る。``{'written': 新規本数, 'total': 入力件数}``。
    """
    as_of = as_of or datetime.now(UTC)
    ts = datetime.fromisoformat(quote_date).replace(tzinfo=UTC)

    evidence_id: int | None = None
    if raw_response is not None:
        evidence_id, _ = base.save_raw(
            conn, store, kind="jquants_daily_bars",
            payload=raw_response, source=SOURCE_NAME,
        )

    written = 0
    for q in quotes:
        symbol = _normalize_symbol(q.get("Code", ""))
        instrument_id = base.resolve_instrument(
            conn, symbol, asset_class="equity", venue="TSE",
            currency="JPY", as_of=as_of,
        )
        created = base.write_bar(
            conn, run,
            instrument_id=instrument_id, ts=ts, timeframe="1d",
            open=_num(q, "O"), high=_num(q, "H"),
            low=_num(q, "L"), close=_num(q, "C"),
            volume=_num(q, "Vo"), source=SOURCE, as_of=as_of,
        )
        if created:
            written += 1
            if evidence_id is not None:
                record(
                    conn, run,
                    [("bars", base.bar_ref(instrument_id, "1d", ts))],
                    [("evidence", evidence_id)],
                )
    return {"written": written, "total": len(quotes)}


# ────────────────────────────────────────────────────────────────────────────
# 財務（/v2/fins/summary）
# ────────────────────────────────────────────────────────────────────────────
def fetch_statements(
    fetcher: Fetcher, key: str, *, code: str | None = None, stmt_date: str | None = None
) -> list[dict[str, Any]]:
    """財務諸表サマリを取得する（code か date のいずれかで）。"""
    params: dict[str, str] = {}
    if code:
        params["code"] = code
    if stmt_date:
        params["date"] = stmt_date
    return _fetch_all(fetcher, "/v2/fins/summary", key=key, params=params)


def ingest_statements(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    statements: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """財務を ``docs.documents``（source_type='filing'）に取り込む（冪等・証憑・リネージ）。

    V2 のカラム名（``Code/DiscDate/DiscNo/DocType``）を読む。1 開示 = 1 文書。冪等キーは
    ``DiscDate + DiscNo``。``{'written', 'total'}``。
    """
    as_of = as_of or datetime.now(UTC)
    written = 0
    for st in statements:
        symbol = _normalize_symbol(st.get("Code", ""))
        disclosure_no = st.get("DiscNo", "")
        disclosed = st.get("DiscDate", "")
        title = f"{symbol} 財務諸表 {st.get('DocType', '')} ({disclosed})"
        published_at = None
        if disclosed:
            try:
                published_at = datetime.fromisoformat(disclosed).replace(tzinfo=UTC)
            except ValueError:
                published_at = None
        res = base.upsert_document(
            conn, run, store,
            source_type="filing", source_name=SOURCE_NAME,
            title=title, body=None, lang="ja",
            published_at=published_at, as_of=as_of,
            meta={"symbol": symbol, "kind": "financial_statement"},
            raw_payload=st, evidence_kind="jquants_statement",
            hash_source=f"{SOURCE_NAME}:{disclosed}:{disclosure_no}:{symbol}",
        )
        if res.created:
            written += 1
    return {"written": written, "total": len(statements)}


# ────────────────────────────────────────────────────────────────────────────
# オーケストレーション + エントリポイント
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class DailyResult:
    instruments: dict[str, int]
    bars: dict[str, int]
    # statements は日足の後段で取得する（Free プランでは日足と同じ 12 週遅延が適用され
    # るため実効日は共有する）。取込 0 件が「本当に開示ゼロだった」か「取込段が例外で
    # 空回りした」かをサマリで見分けるため、成功時は ``{'written', 'total'}``、失敗時は
    # ``{'error': ...}`` を載せる（run_daily 内で完結：src/ryza/jobs/daily.py には触れない）。
    statements: dict[str, Any] = field(default_factory=dict)


def run_daily(
    conn: psycopg.Connection,
    run: Run,
    store: EvidenceStore,
    fetcher: Fetcher,
    *,
    quote_date: str,
    with_instruments: bool = True,
    with_statements: bool = True,
    key: str | None = None,
) -> DailyResult:
    """日次取込（銘柄マスタ更新 + 当日日足 + 財務諸表）を実行する。

    statements は日足の**後**に取得する。同一 API・同一プラン遅延・同一認証のため実効日
    （``quote_date``）を共有するが、HTTP エラー等で失敗しても日足取込を巻き添えにしない
    よう例外をここで捕捉し、``DailyResult.statements`` に ``{'error': ...}`` として記録する
    （T-030 §1 の「静かに空回りさせない」）。``with_statements=False`` で無効化できる。
    """
    key = key if key is not None else api_key()
    inst_result = {"resolved": 0, "created": 0}
    if with_instruments:
        inst_result = ingest_instruments(conn, fetch_listed_info(fetcher, key))
    quotes = fetch_daily_quotes(fetcher, key, quote_date)
    bars_result = ingest_daily_quotes(
        conn, run, store, quotes,
        quote_date=quote_date, raw_response=quotes,
    )
    stmt_result: dict[str, Any] = {}
    if with_statements:
        try:
            statements = fetch_statements(fetcher, key, stmt_date=quote_date)
            stmt_result = ingest_statements(conn, run, store, statements)
        except Exception as exc:  # noqa: BLE001 - 日足取込を巻き添えにしない（T-030 §1）
            stmt_result = {"error": f"{type(exc).__name__}: {exc}"}
    return DailyResult(
        instruments=inst_result, bars=bars_result, statements=stmt_result
    )


# バックフィル時の API 呼び出し間 sleep（秒）。J-Quants の公式レートリミット表は Free
# プランでは明記されていない（2026-08-04 時点、jquants.com/pricing/）。境界越えで 429 を
# 出すよりは保守的に **1 秒**の間を置き、他ジョブと同居して走らせても API 側に負荷を
# 集中させない。数百日ぶんの呼び出しでも合計数分の増加に収まる（日単位のバックフィル
# は運用上「たまに」流すもので、律速要因ではない）。
_BACKFILL_SLEEP_SEC = 1.0

# バックフィルの進捗ログを何日ごとに出すか。数百日を無言で回さない（T-030 §2）。
_BACKFILL_PROGRESS_EVERY = 20


def _weekday_range(start: date, end: date) -> list[date]:
    """``start``〜``end``（両端含む）の平日のみを返す（土日はスキップ）。

    J-Quants の ``date`` 指定は開示が無い日でも空データを返すためエラーにはならないが、
    土日を叩く意味は無いため落とす（``effective_quote_date`` と同じ扱い）。祝日は
    落とさない（取引カレンダー依存を持ち込まない）。
    """
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # 5=土, 6=日
            out.append(d)
        d += timedelta(days=1)
    return out


def run_backfill_statements(
    conn: psycopg.Connection,
    fetcher: Fetcher,
    store: EvidenceStore,
    *,
    date_from: date,
    date_to: date,
    key: str | None = None,
    sleep_sec: float = _BACKFILL_SLEEP_SEC,
    progress_every: int = _BACKFILL_PROGRESS_EVERY,
) -> dict[str, int]:
    """日付範囲の財務諸表をバックフィルする（平日のみ・冪等・レート配慮）。

    ``date_from`` から ``date_to`` の**平日**を日次で ``/v2/fins/summary`` に叩き、
    ``ingest_statements`` へ流す。開示が無い日は空リストが返る（エラーにならない）。
    冪等キー（``DiscDate+DiscNo``）は ``ingest_statements`` 側で担保されているので再実行
    安全（既存分は written=0）。返り値は ``{days, written, total}``。
    """
    key = key if key is not None else api_key()
    days = _weekday_range(date_from, date_to)
    total_written = 0
    total_seen = 0
    for i, d in enumerate(days, start=1):
        iso = d.isoformat()
        with run_ctx(
            "ingest.jquants.backfill_statements",
            {"date": iso}, conn=conn,
        ) as r:
            statements = fetch_statements(fetcher, key, stmt_date=iso)
            res = ingest_statements(conn, r, store, statements)
        total_written += res["written"]
        total_seen += res["total"]
        if i % progress_every == 0 or i == len(days):
            print(
                f"jquants backfill_statements 進捗 {i}/{len(days)} "
                f"最終日={iso} written+={res['written']} total+={res['total']}"
            )
        if i < len(days) and sleep_sec > 0:
            time.sleep(sleep_sec)
    return {"days": len(days), "written": total_written, "total": total_seen}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J-Quants 日次取込")
    parser.add_argument(
        "--date", default=date.today().isoformat(), help="対象日 YYYY-MM-DD"
    )
    parser.add_argument(
        "--no-instruments", action="store_true", help="銘柄マスタ更新を省略"
    )
    parser.add_argument(
        "--no-statements", action="store_true",
        help="財務諸表取込を省略（日足と切り離したいとき）",
    )
    parser.add_argument(
        "--lag-days", type=int, default=_PLAN_LAG_DAYS,
        help=f"プラン遅延日数（既定 {_PLAN_LAG_DAYS}=Free。有償プランは 0）",
    )
    parser.add_argument(
        "--backfill-statements-from", default=None,
        help="財務諸表バックフィル開始日 YYYY-MM-DD（--to と併用でバックフィルモード）",
    )
    parser.add_argument(
        "--backfill-statements-to", default=None,
        help="財務諸表バックフィル終了日 YYYY-MM-DD（両端含む・平日のみ処理）",
    )
    args = parser.parse_args(argv)

    store = base.default_store()
    fetcher = base.default_fetcher()
    # autocommit 共有接続: 冪等な追記書込は逐次確定でよい（再実行が続きを埋める）。
    conn = connect(autocommit=True)
    try:
        # ── バックフィルモード（両方指定されたときのみ） ──────────────────────
        if args.backfill_statements_from and args.backfill_statements_to:
            date_from = date.fromisoformat(args.backfill_statements_from)
            date_to = date.fromisoformat(args.backfill_statements_to)
            if date_from > date_to:
                parser.error("--backfill-statements-from は --backfill-statements-to 以前")
            summary = run_backfill_statements(
                conn, fetcher, store, date_from=date_from, date_to=date_to,
            )
            print(
                f"jquants backfill_statements {date_from}〜{date_to}: {summary}"
            )
            return 0

        # ── 通常の日次モード ─────────────────────────────────────────────────
        # Free プランは直近 12 週の日付指定が HTTP 400 になるため、取得可能な日付へ丸める
        # （モジュール docstring / Issue #38）。財務データも同じプラン遅延の対象なので
        # 実効日を共有する（T-030 §1）。
        quote_date = effective_quote_date(
            date.fromisoformat(args.date), lag_days=args.lag_days
        ).isoformat()

        params = {"date": args.date, "effective_date": quote_date}
        with run_ctx("ingest.jquants.daily", params, conn=conn) as r:
            result = run_daily(
                conn, r, store, fetcher,
                quote_date=quote_date,
                with_instruments=not args.no_instruments,
                with_statements=not args.no_statements,
            )
        print(f"jquants daily {quote_date} (要求 {args.date}): {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
