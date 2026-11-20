"""The time model's database rules (docs/DESIGN.md, "What the time-model prototype showed")."""

from __future__ import annotations

import psycopg
import pytest

from permitnav import db

pytestmark = pytest.mark.db

UNIT = "sfs:2005:716/6kap/4§"


def _setup(conn: psycopg.Connection) -> int:
    download_id = conn.execute(
        "INSERT INTO download (source, url, fetched_at, content_hash, storage_path, http_status)"
        " VALUES ('riksdagen', 'https://data.riksdagen.se/dokument/sfs-2005-716.text', now(),"
        " 'h', 'data/raw/sfs-2005-716.txt', 200) RETURNING id"
    ).fetchone()[0]  # type: ignore[index]
    conn.execute(
        "INSERT INTO unit (id, source_type, authority, language, document, heading, url)"
        " VALUES (%s, 'statute', 'law', 'sv', 'sfs:2005:716', '6 kap. 4 §', 'https://x')",
        (UNIT,),
    )
    return download_id


def _add(conn, download_id, valid_from=None, valid_to=None, basis="valid", unit=UNIT) -> None:
    conn.execute(
        "INSERT INTO unit_version (unit_id, text, text_hash, time_basis, valid_from, valid_to,"
        " observed_from, download_id) VALUES (%s, 't', 'h', %s, %s, %s, now(), %s)",
        (unit, basis, valid_from, valid_to, download_id),
    )


def test_migrations_are_idempotent(conn: psycopg.Connection) -> None:
    assert db.migrate(conn) == []


def test_current_and_upcoming_wordings_can_coexist(conn: psycopg.Connection) -> None:
    d = _setup(conn)
    _add(conn, d, valid_from=None, valid_to="2028-01-01")
    _add(conn, d, valid_from="2028-01-01")


def test_overlapping_wording_is_rejected(conn: psycopg.Connection) -> None:
    d = _setup(conn)
    _add(conn, d, valid_from="2026-06-01")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        _add(conn, d, valid_from="2026-09-01")


def test_overlapping_wording_with_unknown_start_is_rejected(conn: psycopg.Connection) -> None:
    d = _setup(conn)
    _add(conn, d, valid_from=None, valid_to="2028-01-01")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        _add(conn, d, valid_from=None, valid_to=None)


def test_web_page_versions_are_dated_by_observation_only(conn: psycopg.Connection) -> None:
    d = _setup(conn)
    _add(conn, d, basis="observed")
    _add(conn, d, basis="observed")
    with pytest.raises(psycopg.errors.CheckViolation):
        _add(conn, d, valid_from="2026-01-01", basis="observed")
