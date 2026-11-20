"""Shared fixtures. Database tests run against a fresh, throwaway database per test session."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from permitnav import db


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    base = os.environ.get("DATABASE_URL")
    if not base:
        pytest.skip("DATABASE_URL not set; start Postgres with `docker compose up -d`")
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
    """A migrated database; each test's changes are rolled back afterwards."""
    with psycopg.connect(database_url) as connection:
        db.migrate(connection)
        connection.commit()
        try:
            yield connection
        finally:
            connection.rollback()
