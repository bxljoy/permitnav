"""Shared fixtures. Database tests run against a fresh, throwaway database per test session."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql
from psycopg.conninfo import make_conninfo

from permitnav import db, tracing

# Tests trace into memory, installed before anything else can configure tracing: a test run
# never exports to Langfuse or a file, whatever keys are set in the developer's shell.
SPANS = InMemorySpanExporter()
_test_provider = TracerProvider()
_test_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_test_provider)
tracing._provider = _test_provider


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    SPANS.clear()
    tracing.set_content_mode("metadata")
    yield SPANS
    tracing.set_content_mode("metadata")


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    base = os.environ.get("DATABASE_URL")
    if not base:
        pytest.skip("DATABASE_URL not set; start Postgres with `docker compose up -d db`")
    name = f"permitnav_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    test_url = make_conninfo(base, dbname=name)
    try:
        yield test_url
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def conn(database_url: str) -> Iterator[psycopg.Connection]:
    """A migrated database; each test runs in a transaction that is always rolled back.

    Code under test that opens its own transaction gets a savepoint inside it. Deferred
    constraints are made immediate so violations show up in the test, not at a commit
    that never happens.
    """
    with psycopg.connect(database_url) as connection:
        db.migrate(connection)
        connection.commit()
        with connection.transaction(force_rollback=True):
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
            yield connection


@pytest.fixture
def fresh_database_url() -> Iterator[str]:
    """A migrated database of its own, for tests that check what is really committed."""
    base = os.environ.get("DATABASE_URL")
    if not base:
        pytest.skip("DATABASE_URL not set; start Postgres with `docker compose up -d db`")
    name = f"permitnav_fresh_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(base, dbname=name)
    try:
        with psycopg.connect(url) as connection:
            db.migrate(connection)
        yield url
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
