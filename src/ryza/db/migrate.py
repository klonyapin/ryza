"""薄いマイグレーションランナー。

`migrations/NNNN_name.sql` を連番順に適用し、適用済みを `meta.schema_migrations`
に記録する。既に記録済みのファイルはスキップするため、再実行は冪等。

使い方:
    uv run python -m ryza.db.migrate
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg

from ryza.db.conn import connect

# migrations/ はリポジトリルート直下。src/ryza/db/migrate.py から 3 つ上がルート。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")

# meta スキーマがまだ無い初回適用でも記録できるよう、台帳だけは先に用意する。
_BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.schema_migrations (
    version     text PRIMARY KEY,
    filename    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    """(version, path) の一覧を version 昇順で返す。"""
    found: list[tuple[str, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        m = _FILENAME_RE.match(path.name)
        if not m:
            continue
        found.append((m.group(1), path))
    found.sort(key=lambda t: t[0])
    return found


def applied_versions(conn: psycopg.Connection) -> set[str]:
    """適用済み version の集合。"""
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM meta.schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _bootstrap(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_BOOTSTRAP_SQL)
    conn.commit()


def apply_migration(conn: psycopg.Connection, version: str, path: Path) -> None:
    """1 ファイルをトランザクション内で適用し、台帳に記録する。"""
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO meta.schema_migrations (version, filename) VALUES (%s, %s)",
            (version, path.name),
        )
    conn.commit()


def run(directory: Path = MIGRATIONS_DIR) -> list[str]:
    """未適用のマイグレーションを順に適用する。適用した version を返す。"""
    migrations = discover_migrations(directory)
    newly_applied: list[str] = []
    with connect() as conn:
        _bootstrap(conn)
        done = applied_versions(conn)
        for version, path in migrations:
            if version in done:
                continue
            print(f"applying {path.name} ...", file=sys.stderr)
            try:
                apply_migration(conn, version, path)
            except Exception:
                conn.rollback()
                raise
            newly_applied.append(version)
    if newly_applied:
        print(f"applied {len(newly_applied)} migration(s): {', '.join(newly_applied)}",
              file=sys.stderr)
    else:
        print("no pending migrations", file=sys.stderr)
    return newly_applied


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
