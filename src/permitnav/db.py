"""Database connection and plain-SQL migrations.

Migrations are numbered `.sql` files in `migrations/`, applied in order, each in its own
transaction, and recorded in `schema_migration`. There is no ORM and no down-migrations.
"""

from __future__ import annotations

import os
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
    return newly_applied
