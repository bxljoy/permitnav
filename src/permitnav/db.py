"""Database connection and plain-SQL migrations.

Migrations are numbered `.sql` files in `migrations/`, applied in order, each in its own
transaction, and recorded in `schema_migration`. There is no ORM and no down-migrations.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import LiteralString, cast

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env and load it.")
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())


def migrate(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration not applied yet. Returns the names applied, in order."""
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # Read inside this block: a bare SELECT would open a transaction around everything
        # below, and a failing migration would then undo the ones applied before it.
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migration")}
    newly_applied = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in applied:
            continue
        with conn.transaction():
            # Migration files are trusted, version-controlled SQL.
            conn.execute(cast(LiteralString, path.read_text(encoding="utf-8")))
            conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
        newly_applied.append(path.name)
    if newly_applied:
        analyze(conn)
    return newly_applied


def analyze(conn: psycopg.Connection) -> None:
    """Refresh the planner's statistics, after a migration or a bulk load. A column added
    with a default changes no rows, so autovacuum never analyses it and the planner guesses
    (0.5% of rows match): that guess for chunk.kind made every search query join row by row,
    95 ms instead of 27 ms for the law's vector ranking. Only an optimisation: a failure is
    reported and never fails what came before it."""
    try:
        with conn.transaction():
            conn.execute("ANALYZE")
    except psycopg.Error as error:
        print(f"warning: ANALYZE failed ({type(error).__name__}: {error})", file=sys.stderr)
