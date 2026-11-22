"""The eval corpus: a snapshot of what the retrieval eval searches, for CI (phase 3).

Contents: every stored wording, their units, downloads (metadata only, never page
content), citation and transitional links, page translations, and `structure` chunks with
their bge-m3 embeddings. All of it, so the eval in CI sees what a local run sees (a golden
item may expect a section a page no longer shows, or a date before the current wording).
Every stored wording is, today, text as a source published it; when phase 4 rebuilds statute
history from amending acts, those rebuilt versions must be left out here. It is published as a
public GitHub release asset, so it holds public source text only: statutes (no copyright),
Migrationsverket's text (free reuse with attribution) and rulings (CC0). Never the rebuilt
statute history or the change log (phase 4): those are served by the app, not published.

Format: gzip text. A header line, a JSON manifest line, then per table a `TABLE name cols`
line, PostgreSQL COPY text rows, and a `\\.` line.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql

HEADER = "PERMITNAV-EVAL-CORPUS 1"
END = "\\."
STRATEGY = "structure"
SEQUENCES = {"download": "id", "unit_version": "id", "chunk": "id"}

# Each table with the rows to keep; `kept_versions` is a temporary table of version ids.
TABLES: list[tuple[str, str]] = [
    (
        "download",
        "id IN (SELECT download_id FROM unit_version WHERE id IN (SELECT id FROM kept_versions))",
    ),
    ("unit", "id IN (SELECT unit_id FROM unit_version WHERE id IN (SELECT id FROM kept_versions))"),
    ("unit_version", "id IN (SELECT id FROM kept_versions)"),
    (
        "unit_link",
        "to_version_id IN (SELECT id FROM kept_versions) AND from_unit_id IN"
        " (SELECT unit_id FROM unit_version WHERE id IN (SELECT id FROM kept_versions))",
    ),
    ("page_translation", "true"),
    ("chunk", f"strategy = '{STRATEGY}' AND version_id IN (SELECT id FROM kept_versions)"),
    (
        "chunk_embedding_bge_m3",
        f"chunk_id IN (SELECT id FROM chunk WHERE strategy = '{STRATEGY}'"
        " AND version_id IN (SELECT id FROM kept_versions))",
    ),
]


def latest_migration(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT max(name) FROM schema_migration").fetchone()
    return row[0] if row and row[0] else ""


def columns(conn: psycopg.Connection, table: str) -> list[str]:
    """The table's columns, without generated ones (COPY cannot write those)."""
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND is_generated = 'NEVER'
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def export_snapshot(conn: psycopg.Connection, path: Path, commit: str) -> dict:
    """Write the eval corpus to `path`. Returns the manifest."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("export_snapshot needs a connection outside a transaction")
    with conn.transaction():
        # One snapshot of the data for the counts and the rows (an index run may commit).
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute(
            "CREATE TEMP TABLE kept_versions ON COMMIT DROP AS SELECT id FROM unit_version"
        )
        counts = {}
        for table, where in TABLES:
            row = conn.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE " + where).format(sql.Identifier(table))  # pyright: ignore[reportArgumentType]
            ).fetchone()
            counts[table] = row[0] if row else 0
        manifest = {
            "schema": latest_migration(conn),
            "commit": commit,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "strategy": STRATEGY,
            "counts": counts,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb") as out:
            out.write(f"{HEADER}\n{json.dumps(manifest)}\n".encode())
            for table, where in TABLES:
                cols = columns(conn, table)
                out.write(f"TABLE {table} {','.join(cols)}\n".encode())
                query = sql.SQL("COPY (SELECT {} FROM {} WHERE " + where + ") TO STDOUT").format(  # pyright: ignore[reportArgumentType]
                    sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier(table)
                )
                with conn.cursor().copy(query) as copy:
                    for data in copy:
                        out.write(bytes(data))
                out.write(f"{END}\n".encode())
    return manifest


def read_manifest(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        if f.readline().rstrip("\n") != HEADER:
            raise ValueError(f"{path}: not an eval corpus snapshot")
        return json.loads(f.readline())


def import_snapshot(conn: psycopg.Connection, path: Path) -> dict:
    """Load a snapshot into a migrated, empty database. Returns the manifest."""
    manifest = read_manifest(path)
    schema = latest_migration(conn)
    if manifest["schema"] != schema:
        raise ValueError(
            f"snapshot made at schema {manifest['schema']!r}, database is at {schema!r}:"
            " export a new snapshot"
        )
    for table, _ in TABLES:
        row = conn.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier(table))
        ).fetchone()
        if row and row[0]:
            raise ValueError(f"the database already holds {table} rows; import into an empty one")
    with conn.transaction(), gzip.open(path, "rt", encoding="utf-8") as f:
        for _ in range(2):  # the header and the manifest, checked above
            f.readline()
        for table, cols, rows in _tables(f):
            query = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols))
            )
            with conn.cursor().copy(query) as copy:
                for line in rows:
                    copy.write(line)
        for table, column in SEQUENCES.items():
            conn.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s, %s), coalesce(max({}), 1)) FROM {}"
                ).format(sql.Identifier(column), sql.Identifier(table)),
                (table, column),
            )
    return manifest


def _tables(lines: Iterator[str]) -> Iterator[tuple[str, list[str], Iterator[str]]]:
    for line in lines:
        kind, table, cols = line.rstrip("\n").split(" ", 2)
        if kind != "TABLE":
            raise ValueError(f"expected a TABLE line, got {line[:40]!r}")
        yield table, cols.split(","), _rows(lines)


def _rows(lines: Iterator[str]) -> Iterator[str]:
    for line in lines:
        if line.rstrip("\n") == END:
            return
        yield line
    raise ValueError("snapshot ends in the middle of a table")
