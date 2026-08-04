"""J-Quants V2 取込テスト（HTTP 全モック）。

正常系（API キー→日足→bars 書込・SCD2・証憑・リネージ）・重複（冪等）・
認証（API キー未設定）・ページネーション、statements の daily 配線・失敗分離・
日付範囲バックフィル（T-030）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ryza.ingest import jquants
from ryza.ingest.base import FetchResult


def test_api_key_env(monkeypatch):
    monkeypatch.setenv("RYZA_JQUANTS_API_KEY", "KEY123")
    assert jquants.api_key() == "KEY123"


def test_api_key_env_takes_priority_over_secret(monkeypatch, fake_secret_manager):
    """env があれば Secret Manager へアクセスしない(Issue #30)。"""
    calls = fake_secret_manager({"jquants-api-key": "SMKEY"})
    monkeypatch.setenv("RYZA_JQUANTS_API_KEY", "ENVKEY")
    monkeypatch.setenv("GCP_PROJECT", "proj")
    assert jquants.api_key() == "ENVKEY"
    assert calls == []


def test_api_key_secret_manager_fallback(monkeypatch, fake_secret_manager):
    """env 未設定でも VM(GCP_PROJECT あり)なら Secret 'jquants-api-key' から取得。"""
    fake_secret_manager({"jquants-api-key": "SMKEY"})
    monkeypatch.delenv("RYZA_JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "proj")
    assert jquants.api_key() == "SMKEY"


def test_api_key_missing_raises(monkeypatch, fake_secret_manager):
    """env も Secret も無ければ JQuantsAuthError(daily では skipped 扱い)。"""
    fake_secret_manager({})  # Secret 未登録(404)
    monkeypatch.delenv("RYZA_JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    with pytest.raises(jquants.JQuantsAuthError):
        jquants.api_key()


def test_api_key_secret_failure_reason_in_message(monkeypatch, fake_secret_manager):
    """Secret 取得失敗の理由がエラーメッセージ(→ daily skip 理由)に載る(Issue #38)。"""
    fake_secret_manager({})  # 未登録/バージョン未追加 → 404
    monkeypatch.delenv("RYZA_JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "proj")
    with pytest.raises(jquants.JQuantsAuthError, match="jquants-api-key.*404"):
        jquants.api_key()


def test_auth_headers_uses_x_api_key():
    assert jquants._auth_headers("KEY123") == {"x-api-key": "KEY123"}


# ── 実効取得日(Free プランの 12 週遅延、Issue #38)──────────────────────────
def test_effective_quote_date_clamps_recent_date():
    """当日(遅延窓内)の要求は「今日 − lag」へ丸める(400 回避)。"""
    eff = jquants.effective_quote_date(
        date(2026, 8, 3), today=date(2026, 8, 3), lag_days=91
    )
    assert eff == date(2026, 5, 4)  # 月曜: 平日繰り下げなし


def test_effective_quote_date_rolls_weekend_to_friday():
    """丸め結果が土日なら直前の金曜へ繰り下げる。"""
    eff = jquants.effective_quote_date(
        date(2026, 8, 1), today=date(2026, 8, 1), lag_days=91
    )
    assert eff == date(2026, 5, 1)  # 2026-05-02(土) → 05-01(金)


def test_effective_quote_date_keeps_old_date():
    """遅延窓より古い要求日付はそのまま。"""
    eff = jquants.effective_quote_date(
        date(2026, 4, 30), today=date(2026, 8, 3), lag_days=91
    )
    assert eff == date(2026, 4, 30)


def test_effective_quote_date_lag_zero_keeps_today():
    """有償プラン(lag_days=0)では平日の当日取得に戻る。"""
    eff = jquants.effective_quote_date(
        date(2026, 8, 3), today=date(2026, 8, 3), lag_days=0
    )
    assert eff == date(2026, 8, 3)


def test_fetch_all_error_includes_status_and_body(fetcher):
    """非 2xx はステータスとレスポンスボディ付きで失敗する(切り分け用、Issue #38)。"""
    fetcher.add("equities/bars/daily", FetchResult(
        status=400, body=b'{"message": "out of subscription range"}',
    ))
    with pytest.raises(RuntimeError, match="status=400.*out of subscription range"):
        jquants.fetch_daily_quotes(fetcher, "KEY", "2026-08-03")


def test_fetch_daily_quotes_sends_api_key_header(fetcher):
    fetcher.add("equities/bars/daily", FetchResult(
        status=200, body=b'{"data": [{"Code": "72030"}]}',
    ))
    quotes = jquants.fetch_daily_quotes(fetcher, "KEY123", "2026-08-03")
    assert quotes == [{"Code": "72030"}]


def test_fetch_paginates_across_pages(fetcher):
    # 1 ページ目は pagination_key を返し、2 ページ目で終端。部分一致で同一 URL に
    # 2 回目以降は key 無しレスポンスを当てるため、先に登録したルートが優先される
    # のを避けて 2 番目のルートを登録順で後にする。
    class Paging:
        def __init__(self):
            self.n = 0

        def fetch(self, url, *, params=None, headers=None, method="GET", data=None):
            self.n += 1
            if self.n == 1:
                return FetchResult(
                    status=200,
                    body=b'{"data": [{"Code": "72030"}], "pagination_key": "K2"}',
                )
            return FetchResult(status=200, body=b'{"data": [{"Code": "67580"}]}')

    rows = jquants._fetch_all(Paging(), "/v2/equities/master", key="KEY")
    assert [r["Code"] for r in rows] == ["72030", "67580"]


def _quotes():
    return [
        {"Code": "72030", "O": 100, "H": 110, "L": 95, "C": 105, "Vo": 1000},
        {"Code": "67580", "O": 50, "H": 55, "L": 48, "C": 52, "Vo": 500},
    ]


def test_ingest_daily_quotes_writes_bars_with_lineage(conn, run, store):
    as_of = datetime.now(UTC)
    raw = b'{"data": []}'
    res = jquants.ingest_daily_quotes(
        conn, run, store, _quotes(),
        quote_date="2026-08-03", raw_response=raw, as_of=as_of,
    )
    assert res == {"written": 2, "total": 2}

    # SCD2 で 7203.T / 6758.T が自動登録されている。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.instruments "
            "WHERE symbol IN ('7203.T','6758.T') AND valid_to IS NULL"
        )
        assert cur.fetchone()[0] == 2

    # bars に run_id / as_of 付きで 2 本。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.bars "
            "WHERE source='jquants' AND run_id=%s AND as_of=%s",
            (run.run_id, as_of),
        )
        assert cur.fetchone()[0] == 2

    # 各バーに証憑リネージ辺(共有 DB の既存辺と区別するため自 run に絞る)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges "
            "WHERE from_kind='bars' AND to_kind='evidence' AND run_id=%s",
            (run.run_id,),
        )
        assert cur.fetchone()[0] == 2


def test_ingest_daily_quotes_maps_v2_ohlcv(conn, run, store):
    """V2 の O/H/L/C/Vo カラムが bars の OHLCV に正しく対応する。"""
    as_of = datetime.now(UTC)
    jquants.ingest_daily_quotes(
        conn, run, store, _quotes()[:1],
        quote_date="2026-08-03", raw_response=b"{}", as_of=as_of,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open, high, low, close, volume FROM market.bars b "
            "JOIN market.instruments i USING (instrument_id) "
            "WHERE i.symbol='7203.T' AND b.source='jquants' AND b.run_id=%s",
            (run.run_id,),
        )
        assert cur.fetchone() == (100, 110, 95, 105, 1000)


def test_ingest_daily_quotes_idempotent(conn, run, store):
    as_of = datetime.now(UTC)
    kw = dict(quote_date="2026-08-03", raw_response=b"{}", as_of=as_of)
    r1 = jquants.ingest_daily_quotes(conn, run, store, _quotes(), **kw)
    r2 = jquants.ingest_daily_quotes(conn, run, store, _quotes(), **kw)
    assert r1["written"] == 2
    assert r2["written"] == 0  # 同一 PK は増えない
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.bars WHERE source='jquants' AND run_id=%s",
            (run.run_id,),
        )
        assert cur.fetchone()[0] == 2


def test_ingest_statements_as_documents(conn, run, store):
    statements = [
        {"Code": "72030", "DiscDate": "2026-08-03",
         "DiscNo": "20260803001", "DocType": "FYFinancialStatements"},
    ]
    r1 = jquants.ingest_statements(conn, run, store, statements)
    r2 = jquants.ingest_statements(conn, run, store, statements)
    assert r1["written"] == 1
    assert r2["written"] == 0  # 冪等
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type FROM docs.documents WHERE source_name='J-Quants'"
        )
        assert cur.fetchone()[0] == "filing"


def test_run_daily_full_flow(conn, run, store, fetcher):
    fetcher.add("equities/master", FetchResult(
        status=200,
        body=b'{"data": [{"Code": "72030"}]}',
    ))
    fetcher.add("equities/bars/daily", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","O":1,"H":2,"L":1,"C":2,"Vo":10}]}',
    ))
    # T-030: statements も日足と同じ実効日で取得する。全ルート登録の完全系。
    fetcher.add("fins/summary", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","DiscDate":"2026-08-03",'
             b'"DiscNo":"20260803001","DocType":"FYFinancialStatements"}]}',
    ))
    result = jquants.run_daily(
        conn, run, store, fetcher, quote_date="2026-08-03", key="KEY123"
    )
    assert result.bars["written"] == 1
    assert result.instruments["resolved"] == 1
    assert result.statements == {"written": 1, "total": 1}


# ── T-030: statements の daily 配線・失敗分離・バックフィル ─────────────────
def test_run_daily_writes_statements_alongside_bars(conn, run, store, fetcher):
    """run_daily が statements も取得・取込し DailyResult.statements に載せる。"""
    fetcher.add("equities/bars/daily", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","O":1,"H":2,"L":1,"C":2,"Vo":10}]}',
    ))
    fetcher.add("fins/summary", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","DiscDate":"2026-08-03",'
             b'"DiscNo":"D1","DocType":"FYFinancialStatements"},'
             b'{"Code":"67580","DiscDate":"2026-08-03",'
             b'"DiscNo":"D2","DocType":"FYFinancialStatements"}]}',
    ))
    result = jquants.run_daily(
        conn, run, store, fetcher,
        quote_date="2026-08-03", with_instruments=False, key="KEY123",
    )
    assert result.bars["written"] == 1
    assert result.statements == {"written": 2, "total": 2}
    # DB にも docs.documents（source_name='J-Quants'）が 2 件書かれている。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM docs.documents "
            "WHERE source_name='J-Quants' AND meta->>'kind'='financial_statement'"
        )
        assert cur.fetchone()[0] == 2


def test_run_daily_statements_error_does_not_break_bars(conn, run, store, fetcher):
    """statements の HTTP エラーが日足取込の成功を巻き添えにしない（T-030 §1）。"""
    fetcher.add("equities/bars/daily", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","O":1,"H":2,"L":1,"C":2,"Vo":10}]}',
    ))
    fetcher.add("fins/summary", FetchResult(
        status=500, body=b'{"message": "boom"}',
    ))
    result = jquants.run_daily(
        conn, run, store, fetcher,
        quote_date="2026-08-03", with_instruments=False, key="KEY123",
    )
    # 日足は書けている。
    assert result.bars["written"] == 1
    # statements はエラーとして記録される（例外は上には伝播しない）。
    assert "error" in result.statements
    assert "500" in result.statements["error"]
    # 日足の書き込みは実在する（statements 失敗で巻き戻っていない）。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.bars WHERE source='jquants' AND run_id=%s",
            (run.run_id,),
        )
        assert cur.fetchone()[0] == 1


def test_run_daily_with_statements_false_skips_statements(conn, run, store, fetcher):
    """--no-statements 相当（with_statements=False）で財務段を丸ごとスキップする。"""
    fetcher.add("equities/bars/daily", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","O":1,"H":2,"L":1,"C":2,"Vo":10}]}',
    ))
    # fins/summary へは叩かない（登録しない = FakeFetcher の 404 に落ちない）ことを確認。
    result = jquants.run_daily(
        conn, run, store, fetcher,
        quote_date="2026-08-03", with_instruments=False, with_statements=False,
        key="KEY123",
    )
    assert result.statements == {}
    # fetcher.calls に fins/summary への呼び出しが無いこと。
    assert not any("fins/summary" in c for c in fetcher.calls)


def test_backfill_statements_processes_weekdays_only(conn, store, fetcher):
    """バックフィル: 平日のみを日次処理し、合計サマリを返す。"""
    # 2026-05-01(金) 〜 2026-05-08(金): 8 日中平日は 6 日（5/2 土・5/3 日を除く）。
    # 全日に対して同一 DiscNo の 1 件を返すモック（冪等キー確認も兼ねる）。
    fetcher.add("fins/summary", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","DiscDate":"2026-05-01",'
             b'"DiscNo":"BF-01","DocType":"FYFinancialStatements"}]}',
    ))
    summary = jquants.run_backfill_statements(
        conn, fetcher, store,
        date_from=date(2026, 5, 1), date_to=date(2026, 5, 8),
        key="KEY123", sleep_sec=0, progress_every=100,
    )
    assert summary["days"] == 6
    # 冪等キー（DiscDate+DiscNo）が同一なので written は初回の 1 件のみ、以降は 0。
    assert summary["total"] == 6
    assert summary["written"] == 1
    # 土日には API を叩いていない（呼び出し回数=6）。
    call_count = sum(1 for c in fetcher.calls if "fins/summary" in c)
    assert call_count == 6
    # 呼び出しに 5/2(土)・5/3(日) の date パラメータが混入していない。
    assert not any("date=2026-05-02" in c or "date=2026-05-03" in c
                   for c in fetcher.calls)


def test_backfill_statements_idempotent_on_rerun(conn, store, fetcher):
    """バックフィル: 再実行で written=0（DiscDate+DiscNo が冪等キー）。"""
    fetcher.add("fins/summary", FetchResult(
        status=200,
        body=b'{"data": [{"Code":"72030","DiscDate":"2026-05-04",'
             b'"DiscNo":"BF-IDEM","DocType":"FYFinancialStatements"}]}',
    ))
    kw = dict(
        date_from=date(2026, 5, 4), date_to=date(2026, 5, 4),
        key="KEY123", sleep_sec=0, progress_every=100,
    )
    r1 = jquants.run_backfill_statements(conn, fetcher, store, **kw)
    r2 = jquants.run_backfill_statements(conn, fetcher, store, **kw)
    assert r1 == {"days": 1, "written": 1, "total": 1}
    assert r2 == {"days": 1, "written": 0, "total": 1}


def test_backfill_statements_rejects_reversed_range():
    days = jquants._weekday_range(date(2026, 5, 5), date(2026, 5, 1))
    assert days == []  # 開始 > 終了は空（呼び出しゼロ・レンジ検査は CLI 側）


def test_main_effective_date_applies_to_statements(conn, store, fetcher):
    """run_daily 呼び出し時、statements も quote_date（=実効日）で叩かれる。

    daily の実効日丸め（effective_quote_date, Issue #38）が statements にも一貫して適用
    されることを、URL の date パラメータで直接確認する（T-030 §4）。
    """
    fetcher.add("equities/bars/daily", FetchResult(
        status=200, body=b'{"data": []}',
    ))
    fetcher.add("fins/summary", FetchResult(
        status=200, body=b'{"data": []}',
    ))
    from ryza.provenance import start_run

    r = start_run("test.jquants.eff", conn=conn)
    jquants.run_daily(
        conn, r, store, fetcher,
        quote_date="2026-05-04", with_instruments=False, key="KEY",
    )
    # statements の呼び出しに date=2026-05-04 が渡っている。
    stmt_calls = [c for c in fetcher.calls if "fins/summary" in c]
    assert stmt_calls, "fins/summary が呼ばれていない"
    assert all("date=2026-05-04" in c for c in stmt_calls)


def test_normalize_symbol():
    assert jquants._normalize_symbol("72030") == "7203.T"
    assert jquants._normalize_symbol("7203") == "7203.T"
