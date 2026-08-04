"""ledger.evidence の不変性ガード(migrations/0036 — T-022 / Issue #128)。

裁定(T-022 指示書):
1. **UPDATE は封鎖**: sha256 / payload_ref の書き換えは改竄検知(A-1)の前提を壊すため、
   参照の有無に関わらず拒否される。
2. **TRUNCATE も封鎖**: 行トリガは TRUNCATE で発火しないため文トリガで塞ぐ(0035 と
   同じ論法)。CASCADE 経路も含めて単体で確認する。
3. **参照済み行の DELETE は FK が拒否**: 本ガードの裁定はこの既存挙動に依存するため、
   その前提が壊れていないことをリグレッションとして固定する。
4. **未参照行の DELETE は許容**: tests/conftest.py の ``_CLEAR_EVIDENCE_SQL``(残留データ
   隔離)が依存する経路であり、本 migration ではあえて塞がない(Issue #23 とセットで
   再評価 — ops/reminders.yaml `ledger-evidence-full-append-only`)。

テストは既存の ledger テストの流儀に従う: 関数スコープの ``conn`` フィクスチャで接続を得て、
commit せず rollback で隔離する(tests/ledger/conftest.py L18-24)。
"""

from __future__ import annotations

import psycopg
import pytest

# 「参照済み evidence」を作るための最小仕訳(既存の tests/test_migrations.py の
# _new_evidence / _new_entry と同型 — こちらは独立のテストファイルなのでインライン化)。
_INSERT_EVIDENCE_SQL = """
    INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
    VALUES (%s, 'inline://test', sha256('x'::bytea), 'test', now())
    RETURNING evidence_id
"""

_INSERT_ENTRY_SQL = """
    INSERT INTO ledger.journal_entries
        (book_id, entry_date, description, evidence_id, posted_by, run_id)
    VALUES ('DEMO_FUND', DATE '2026-08-04', 'guard test', %s, 'test', 0)
    RETURNING entry_id
"""


def _new_evidence(cur, kind: str = "broker_fill") -> int:
    cur.execute(_INSERT_EVIDENCE_SQL, (kind,))
    return cur.fetchone()[0]


def _new_referenced_evidence(cur) -> int:
    """journal_entries から参照されている evidence 行の evidence_id を返す。"""
    evidence_id = _new_evidence(cur)
    cur.execute(_INSERT_ENTRY_SQL, (evidence_id,))
    return evidence_id


# ── (1) UPDATE 封鎖 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("column", "value", "why"),
    [
        ("payload_ref", "inline://tampered", "参照先の書き換え(証憑差し替え)"),
        ("sha256", b"\x00" * 32, "ハッシュの書き換え(改竄検知の無効化)"),
        ("kind", "invoice", "種別の書き換え"),
        ("source", "attacker", "取得元の書き換え"),
    ],
)
def test_evidence_update_rejected_when_unreferenced(conn, column, value, why):
    """未参照の evidence 行への UPDATE も拒否される(参照の有無に関わらず不変)。"""
    with conn.cursor() as cur:
        evidence_id = _new_evidence(cur)
        with pytest.raises(psycopg.errors.RaiseException, match="証憑は不変"):
            cur.execute(
                f"UPDATE ledger.evidence SET {column} = %s WHERE evidence_id = %s",  # noqa: S608 - column は固定リスト
                (value, evidence_id),
            )
    conn.rollback()


def test_evidence_update_rejected_when_referenced(conn):
    """参照済み evidence 行への UPDATE(証憑差し替え攻撃)も拒否される — 本ガードの本丸。"""
    with conn.cursor() as cur:
        evidence_id = _new_referenced_evidence(cur)
        with pytest.raises(psycopg.errors.RaiseException, match="証憑は不変"):
            cur.execute(
                "UPDATE ledger.evidence SET payload_ref = %s WHERE evidence_id = %s",
                ("inline://tampered", evidence_id),
            )
    conn.rollback()


# ── (2) TRUNCATE 封鎖 ───────────────────────────────────────────────────────
def test_evidence_truncate_without_cascade_rejected_by_fk(conn):
    """CASCADE 無しの `TRUNCATE ledger.evidence` は PostgreSQL の FK 参照禁止で拒否される。

    journal_entries.evidence_id / reconciliations.evidence_id からの FK があるため、
    PostgreSQL は CASCADE 無しの TRUNCATE を FeatureNotSupported で先に拒否する
    (0036 の文トリガに到達する前に SQL 解釈段階で落ちる)。この既存挙動が本ガードの
    有効性(evidence 単体を消せない)の前提の一部なので、リグレッションとして固定する。
    """
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            cur.execute("TRUNCATE ledger.evidence")
    conn.rollback()


def test_evidence_truncate_cascade_rejected(conn):
    """CASCADE 経由の `TRUNCATE ledger.evidence CASCADE` は文トリガで拒否される。

    0036 で追加した evidence_no_truncate 文トリガが発火する経路。CASCADE を付ければ
    PostgreSQL の FK チェック(FeatureNotSupported)は迂回できるが、evidence 側の
    0036 文トリガ、あるいは CASCADE 先の journal 2 表に張られた 0035 文トリガの
    いずれかが必ず発火して RAISE EXCEPTION に落とす — どちらか一方でも塞がっていれば
    ガードが空いている状態にはならない。
    """
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="TRUNCATE は禁止"):
            cur.execute("TRUNCATE ledger.evidence CASCADE")
    conn.rollback()


# ── (3) 参照済み行の DELETE は FK が拒否 ────────────────────────────────────
def test_referenced_evidence_delete_rejected_by_fk(conn):
    """本ガードの裁定は「参照済み行の DELETE は FK が既に拒否する」という前提に依存する。

    この前提が壊れると『UPDATE を塞げば十分』という論法が崩れて DELETE 封鎖の要否判断が
    変わる(裁定の再評価が要る)ため、リグレッション検知としてここで固定する。
    """
    with conn.cursor() as cur:
        evidence_id = _new_referenced_evidence(cur)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "DELETE FROM ledger.evidence WHERE evidence_id = %s",
                (evidence_id,),
            )
    conn.rollback()


# ── (4) 未参照行の DELETE は許容(conftest 経路の保全)────────────────────
def test_unreferenced_evidence_delete_allowed(conn):
    """どこからも参照されない evidence 行は DELETE できる。

    conftest.py の ``clear_residual`` / ``_CLEAR_EVIDENCE_SQL`` が依存する経路であり、
    本 migration ではあえて塞がない(裁定 3 — Issue #23 とセットで再評価)。conftest が
    無変更で通ることが本設計の受け入れ根拠なので、その経路を直接叩いて確認する。
    """
    with conn.cursor() as cur:
        evidence_id = _new_evidence(cur)
        cur.execute(
            "DELETE FROM ledger.evidence WHERE evidence_id = %s",
            (evidence_id,),
        )
        assert cur.rowcount == 1, "未参照行の DELETE が通らないと clear_residual が壊れる"
    conn.rollback()


# ── トリガの存在・有効性(0035 test_ledger_truncate_guard_triggers... と同基準)─
def test_evidence_guard_triggers_exist_and_are_enabled(conn):
    """0036 の 2 トリガが ledger.evidence に存在し、有効(tgenabled='O')であること。

    所有者ロールは ``ALTER TABLE ... DISABLE TRIGGER USER`` 1 行でガードを無音化でき、
    'D' のまま戻し忘れれば不変性は空手形になる(0024 dev_chat・0026 分類履歴・
    0035 journal と同じ基準)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tgname, tgenabled
            FROM pg_trigger
            WHERE tgrelid = 'ledger.evidence'::regclass
              AND tgname IN ('evidence_no_update', 'evidence_no_truncate')
              AND NOT tgisinternal
            ORDER BY tgname
            """
        )
        rows = cur.fetchall()
    assert rows == [
        ("evidence_no_truncate", "O"),
        ("evidence_no_update", "O"),
    ], rows
