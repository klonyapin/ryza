"""T-001 受け入れ基準の自動検証。

ライブ PostgreSQL（compose.yaml の DB）に対して実行する。DB に接続できない場合は
全テストを skip する（Docker 未導入環境向け）。

前提: `docker compose up -d` 済み、または RYZA_DATABASE_URL が有効な DB を指す。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.db import migrate
from ryza.db.conn import connect, database_url

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


@pytest.fixture(scope="session")
def migrated_db():
    """DB に接続できれば全マイグレーションを適用して yield。不可なら skip。"""
    try:
        with psycopg.connect(database_url(), connect_timeout=3):
            pass
    except Exception as exc:  # noqa: BLE001 - 接続不能は skip 理由として提示
        pytest.skip(f"PostgreSQL に接続できないため skip: {exc}")
    migrate.run()
    yield


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
    ):
        assert name in triggers, f"トリガ {name} が存在しない"


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
        assert cur.fetchone()[0] == 18  # ファンド帳簿の科目数
        cur.execute("SELECT count(*) FROM ledger.accounts WHERE book_id = 'OPS'")
        assert cur.fetchone()[0] == 13  # 運営帳簿の科目数
