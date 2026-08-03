"""T-001 受け入れ基準の自動検証。

テスト専用 DB(tests/conftest.py の ``migrated_db`` が用意)に対して実行する。
DB に接続できない場合は全テストを skip する（Docker 未導入環境向け）。

前提: `docker compose up -d` 済み、または RYZA_DATABASE_URL が有効な DB を指す。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.db import migrate
from ryza.db.conn import connect

# 5 スキーマ・全テーブルの期待集合（設計書 §2〜§6）。
EXPECTED_TABLES: dict[str, set[str]] = {
    "meta": {"schema_migrations", "runs", "lineage_edges", "audit_findings"},
    "market": {"instruments", "bars", "indicators"},
    "docs": {"documents", "embeddings", "market_view", "research_reports"},
    "trade": {"signals", "order_intents", "orders", "fills"},
    "ledger": {
        "books", "accounts", "evidence", "journal_entries", "journal_lines",
        "nav_snapshots", "reconciliations", "budgets",
    },
}


@pytest.fixture
def conn(migrated_db):
    """関数スコープの接続。テストは commit せず rollback して隔離する。"""
    c = connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _new_evidence(cur, kind: str = "broker_fill") -> int:
    cur.execute(
        """
        INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
        VALUES (%s, 'inline://test', sha256('x'::bytea), 'test', now())
        RETURNING evidence_id
        """,
        (kind,),
    )
    return cur.fetchone()[0]


def _new_entry(cur, book_id: str = "DEMO_FUND", evidence_id: int | None = None) -> int:
    if evidence_id is None:
        evidence_id = _new_evidence(cur)
    cur.execute(
        """
        INSERT INTO ledger.journal_entries
            (book_id, entry_date, description, evidence_id, posted_by, run_id)
        VALUES (%s, DATE '2026-08-02', 'test', %s, 'test', 0)
        RETURNING entry_id
        """,
        (book_id, evidence_id),
    )
    return cur.fetchone()[0]


# ── 受け入れ基準 1: 適用が冪等 ──────────────────────────────────────────────
def test_migrations_are_idempotent(migrated_db):
    # migrated_db フィクスチャで一度適用済み。再実行すると未適用が無い。
    assert migrate.run() == []


def test_all_six_migrations_recorded(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM meta.schema_migrations ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
    # T-001 の 6 マイグレーションが先頭に順序どおり記録されている。後続タスク
    # （0007 報道部・0008 リサーチ取込 …）が積み増すため、先頭 6 件のみを検証する。
    assert versions[:6] == ["0001", "0002", "0003", "0004", "0005", "0006"]


# ── 受け入れ基準 2: 5 スキーマ・全テーブルが存在 ───────────────────────────
def test_all_schemas_exist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT schema_name FROM information_schema.schemata")
        schemas = {r[0] for r in cur.fetchall()}
    assert set(EXPECTED_TABLES).issubset(schemas)


def test_all_tables_exist(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema = ANY(%s)
            """,
            (list(EXPECTED_TABLES),),
        )
        got: dict[str, set[str]] = {s: set() for s in EXPECTED_TABLES}
        for schema, table in cur.fetchall():
            got[schema].add(table)
    for schema, expected in EXPECTED_TABLES.items():
        assert expected.issubset(got[schema]), f"{schema}: 欠落 {expected - got[schema]}"


def test_ledger_integrity_triggers_exist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        triggers = {r[0] for r in cur.fetchall()}
    for name in (
        "journal_lines_book_match",
        "journal_lines_balanced",
        "journal_entries_no_mutation",
        "journal_lines_no_mutation",
        "journal_lines_mtm_guard",  # 0034: 評価調整勘定の書き込みガード
    ):
        assert name in triggers, f"トリガ {name} が存在しない"


def test_dev_chat_guard_triggers_exist_and_are_enabled(conn):
    """0024 のガードが存在し、**有効**であること(独立役員審査 軽-8)。

    トリガの存在だけを見ても足りない。所有ロールは
    ``ALTER TABLE ops.dev_chat DISABLE TRIGGER USER`` の 1 行でガードを無音化でき
    (その手口は本テストスイートのフィクスチャ自身が残留行の掃除に使っている)、
    ``tgenabled`` が 'D' に落ちたまま元に戻し忘れれば追記オンリーは空手形になる。
    'O'(= origin/local のセッション複製設定で発火)であることまで確認する。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tgname, tgenabled FROM pg_trigger
            WHERE tgrelid = 'ops.dev_chat'::regclass AND NOT tgisinternal
            ORDER BY tgname
            """
        )
        triggers = dict(cur.fetchall())
    assert set(triggers) == {"dev_chat_append_only", "dev_chat_no_truncate"}
    assert all(state == "O" for state in triggers.values()), triggers


def test_classification_history_guards_exist_and_are_enabled(conn):
    """0026 の追記オンリーガードが存在し、**有効**であること(0024 と同基準)。

    分類履歴は E6(point-in-time ユニバース)の証跡そのもので、無音化されると
    「当時の分類」を後から書き換えられる。``tgenabled`` が 'D'(= 掃除のために
    フィクスチャが一時的に落とす状態)のまま残っていないことまで見る。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tgname, tgenabled FROM pg_trigger
            WHERE tgrelid = 'market.instrument_classification_history'::regclass
              AND NOT tgisinternal
            ORDER BY tgname
            """
        )
        triggers = dict(cur.fetchall())
    assert set(triggers) == {
        "instrument_classification_history_no_mutation",
        "instrument_classification_history_no_truncate",
        # 記録時刻の固定(E6 カバレッジの偽装防止 — 審査 C-19)。テストは PIT 検証の
        # ためにこの 1 本だけを一時的に外すので、無効のまま残らないことを見る。
        "instrument_classification_history_stamp_recorded_at",
    }
    assert all(state == "O" for state in triggers.values()), triggers


def test_bars_is_partitioned(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relkind
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'market' AND c.relname = 'bars'
            """
        )
        relkind = cur.fetchone()[0]
    assert relkind == "p"  # partitioned table


# ── 受け入れ基準 3: 貸借不一致の仕訳が INSERT できない ─────────────────────
def test_unbalanced_entry_rejected(conn):
    with conn.cursor() as cur:
        entry_id = _new_entry(cur)
        cur.execute(
            """
            INSERT INTO ledger.journal_lines
                (entry_id, line_no, book_id, account_id, debit, credit, currency)
            VALUES (%s, 1, 'DEMO_FUND', 'cash', 1000, 0, 'JPY')
            """,
            (entry_id,),
        )
        # 貸方が無い＝不一致。DEFERRED 制約を即時評価させて発火を確認。
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    conn.rollback()


def test_balanced_entry_accepted(conn):
    with conn.cursor() as cur:
        entry_id = _new_entry(cur)
        cur.execute(
            """
            INSERT INTO ledger.journal_lines
                (entry_id, line_no, book_id, account_id, debit, credit, currency)
            VALUES (%s, 1, 'DEMO_FUND', 'cash', 1000, 0, 'JPY'),
                   (%s, 2, 'DEMO_FUND', 'capital', 0, 1000, 'JPY')
            """,
            (entry_id, entry_id),
        )
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # 発火せず通る
    conn.rollback()


# ── 受け入れ基準 4: evidence_id なしの仕訳が INSERT できない ───────────────
def test_entry_without_evidence_rejected(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.NotNullViolation):
            cur.execute(
                """
                INSERT INTO ledger.journal_entries
                    (book_id, entry_date, description, evidence_id, posted_by, run_id)
                VALUES ('DEMO_FUND', DATE '2026-08-02', 'no evidence', NULL, 'test', 0)
                """
            )
    conn.rollback()


# ── 受け入れ基準 5: 親 entry と異なる book_id の line が INSERT できない ────
def test_line_book_mismatch_rejected(conn):
    with conn.cursor() as cur:
        entry_id = _new_entry(cur, book_id="DEMO_FUND")
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                """
                INSERT INTO ledger.journal_lines
                    (entry_id, line_no, book_id, account_id, debit, credit, currency)
                VALUES (%s, 1, 'OPS', 'cash_bank', 1000, 0, 'JPY')
                """,
                (entry_id,),
            )
    conn.rollback()


# ── 受け入れ基準 6: journal_entries への UPDATE が拒否される ───────────────
def test_journal_entries_update_rejected(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT entry_id FROM ledger.journal_entries LIMIT 1")
        row = cur.fetchone()
        assert row is not None, "シード仕訳が存在しない"
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE ledger.journal_entries SET description = 'x' WHERE entry_id = %s",
                (row[0],),
            )
    conn.rollback()


def test_journal_lines_delete_rejected(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute("DELETE FROM ledger.journal_lines")
    conn.rollback()


# ── 受け入れ基準 7: シード後の DEMO_FUND 試算表残高 ────────────────────────
def test_seed_trial_balance(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, sum(debit) AS d, sum(credit) AS c
            FROM ledger.journal_lines
            WHERE book_id = 'DEMO_FUND'
            GROUP BY account_id
            ORDER BY account_id
            """
        )
        balances = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    # 0006 初期出資 ¥1,000,000 + 0011 追加出資 ¥9,000,000(2026-08-03 増額決定)
    assert balances["cash"] == (10000000, 0)
    assert balances["capital"] == (0, 10000000)


def test_seed_books_and_accounts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT book_id FROM ledger.books ORDER BY book_id")
        books = [r[0] for r in cur.fetchall()]
        assert books == ["DEMO_FUND", "OPS"]
        assert "LIVE_FUND" not in books  # まだ作らない

        cur.execute(
            "SELECT count(*) FROM ledger.accounts WHERE book_id = 'DEMO_FUND'"
        )
        # 18(0006 の初期セット)+ 1(0034 の評価調整勘定 securities_mtm)。
        assert cur.fetchone()[0] == 19  # ファンド帳簿の科目数
        cur.execute("SELECT count(*) FROM ledger.accounts WHERE book_id = 'OPS'")
        assert cur.fetchone()[0] == 13  # 運営帳簿の科目数

        # 0034: 評価調整勘定はファンド帳簿にだけ置く(運営帳簿に建玉は無い)。
        cur.execute(
            "SELECT book_id, category FROM ledger.accounts "
            "WHERE account_id = 'securities_mtm' ORDER BY book_id"
        )
        assert cur.fetchall() == [("DEMO_FUND", "asset")]


# ── 0027: 実クエリ向けの索引 ──────────────────────────────────────────────────
#
# **検査するのは索引の存在と定義（列と列順・部分索引の述語）だけである。**
# EXPLAIN のプラン検証はテストにしない — プラン選択は行数・統計・共有バッファの状態・
# PostgreSQL のバージョンで変わり、CI の空 DB では索引を使わないのが正しい判断になる。
# 「速くなったか」をテストで主張するとフレークするだけで、根拠にもならない。実測は
# migrations/0027_query_indexes.sql のコメントに EXPLAIN ANALYZE の前後比較として残す。
#
# 列順まで固定するのは、それ自体が審査の対象だったからである。ただし
# **「2 列版 (book_id, account_id) は選ばれない」は規模条件付きの命題**であることに注意
# （独立役員審査 中-1）: 明細 5 万行規模（規模A）では 2 列版も選ばれ
# securities_book_value を 4.00 → 2.19 ms（1.8x）改善する。選ばれなくなるのは明細 60 万行
# 規模（規模B）からで、そこでは 2 列版は逐次走査のままになる（39.7 / 37.1 ms = 改善ゼロ）。
# つまり **instrument_id を落とす変更は、小さい DB では何も壊れていないように見えたまま、
# 本番規模で無言に索引不使用へ退化する**。CI の DB は空に近く EXPLAIN では捕まらないので、
# 定義そのものをここで固定して列落ちが黙って通らないようにする。計測の詳細は
# migrations/0027_query_indexes.sql のコメントを参照。
EXPECTED_INDEX_DEFS: dict[tuple[str, str], str] = {
    ("ledger", "journal_lines_book_account_instrument_idx"):
        "CREATE INDEX journal_lines_book_account_instrument_idx "
        "ON ledger.journal_lines USING btree (book_id, account_id, instrument_id)",
    ("ledger", "journal_entries_book_date_idx"):
        "CREATE INDEX journal_entries_book_date_idx "
        "ON ledger.journal_entries USING btree (book_id, entry_date)",
    ("meta", "runs_started_at_idx"):
        "CREATE INDEX runs_started_at_idx ON meta.runs USING btree (started_at)",
    ("meta", "runs_running_idx"):
        "CREATE INDEX runs_running_idx ON meta.runs USING btree (run_id DESC) "
        "WHERE (status = 'running'::text)",
}


def test_query_indexes_exist_with_expected_definition(conn):
    """0027 の索引が存在し、列・列順・部分索引の述語まで定義どおりであること。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT schemaname, indexname, indexdef
            FROM pg_indexes
            WHERE (schemaname, indexname) IN (
                ('ledger', 'journal_lines_book_account_instrument_idx'),
                ('ledger', 'journal_entries_book_date_idx'),
                ('meta',   'runs_started_at_idx'),
                ('meta',   'runs_running_idx')
            )
            """
        )
        got = {(s, n): d for s, n, d in cur.fetchall()}
    missing = set(EXPECTED_INDEX_DEFS) - set(got)
    assert not missing, f"0027 の索引が存在しない: {missing}"
    for key, expected in EXPECTED_INDEX_DEFS.items():
        assert got[key] == expected, f"{key[1]} の定義が想定と違う: {got[key]}"


def test_query_indexes_migration_sql_is_idempotent(conn):
    """0027 の SQL 本体を再実行しても失敗しないこと（CREATE INDEX IF NOT EXISTS）。

    ``test_migrations_are_idempotent`` はランナーが適用済み version を飛ばすことしか
    見ておらず、SQL 自体の冪等性は検査していない。台帳が失われた状態からの再適用や、
    別 DB への手当てで同じファイルを流すことは実際に起こるため、ここで直接叩く。
    """
    version, path = next(
        (v, p) for v, p in migrate.discover_migrations() if v == "0027"
    )
    assert path.name == "0027_query_indexes.sql"
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)  # 既に適用済みの DB に対して 2 度目の実行
        cur.execute(sql)  # 3 度目でも同じ
    conn.rollback()
