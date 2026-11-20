"""Storing parsed statutes as dated versions (D3, D4; docs/DESIGN.md "Phase 1 internals").

`ingest_statute(conn, raw, statute)` applies one download in one transaction. Per unit, the
parsed wordings are matched one-to-one to the stored wordings that are still observed
(same text), and then:

1. **Still there** (matched): take the dates the source now gives. An end date the source no
   longer gives is cleared (the change was withdrawn or postponed), unless the end is now
   "on a day the government decides". A real start date clears an estimate.
2. **Gone** (stored, not in the text): `observed_to` is set, and the wording ends:
   - when a new wording of the unit without a start date replaces it, on the day the change
     was seen (an estimate, `dates_estimated`), even if it had an end date of its own; the new
     wording starts that day too;
   - otherwise when the earliest new wording that starts after it takes effect;
   - otherwise when its repealing act took effect;
   - otherwise on the day the change was seen (estimate).
   A wording whose end would fall on or before its own start never took effect: it is marked
   `withdrawn` and never applies. A wording that was waiting for a government decision just
   stops being observed.
3. **New** (in the text, not stored): inserted.

Transitional provisions are then linked to the versions their act introduced and to the
versions those replaced. Re-ingesting a download already recorded changes nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

import psycopg

from permitnav.parsing.statute import Statute
from permitnav.parsing.types import ParsedUnit
from permitnav.sources.riksdagen import RawFetch


@dataclass
class IngestReport:
    download_id: int
    already_recorded: bool = False  # this exact download was ingested before
    units: int = 0
    new_versions: int = 0
    unchanged_versions: int = 0
    updated_versions: int = 0
    closed_versions: int = 0
    withdrawn_versions: int = 0
    links: int = 0
    estimated: list[str] = field(default_factory=list)  # units whose change date was guessed
    conflicts: list[str] = field(default_factory=list)  # from the parser


@dataclass
class _Stored:
    id: int
    unit_id: str
    text_hash: str
    valid_from: date | None
    valid_to: date | None
    pending: str | None
    dates_estimated: bool


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ingest_statute(conn: psycopg.Connection, raw: RawFetch, statute: Statute) -> IngestReport:
    with conn.transaction():
        # Versions are rewritten in any order; the overlap rule is checked once at the end.
        conn.execute("SET CONSTRAINTS one_wording_per_day DEFERRED")
        download_id, already = _record_download(conn, raw)
        report = IngestReport(
            download_id=download_id, already_recorded=already, conflicts=list(statute.conflicts)
        )
        _upsert_units(conn, statute.units)

        parsed_by_unit: dict[str, list[ParsedUnit]] = {}
        for unit in statute.units:
            parsed_by_unit.setdefault(unit.unit_id, []).append(unit)
        report.units = len(parsed_by_unit)
        stored_by_unit: dict[str, list[_Stored]] = {}
        for stored in _open_versions(conn, raw.url):
            stored_by_unit.setdefault(stored.unit_id, []).append(stored)

        seen_on = raw.fetched_at.date()
        for unit_id in parsed_by_unit.keys() | stored_by_unit.keys():
            _apply_unit(
                conn,
                parsed_by_unit.get(unit_id, []),
                stored_by_unit.get(unit_id, []),
                raw,
                download_id,
                seen_on,
                statute,
                report,
            )

        report.links = _link_transitional(conn, statute)
        conn.execute("SET CONSTRAINTS one_wording_per_day IMMEDIATE")  # check now, not at commit
    return report


def versions_on(conn: psycopg.Connection, unit_id: str, as_of: date) -> list[dict]:
    """The wordings of a unit that apply on a date (D8, D5).

    Wordings waiting for a government decision, and withdrawn wordings, never apply.
    """
    rows = conn.execute(
        """
        SELECT id, text, valid_from, valid_to, amended_by, pending, dates_estimated
        FROM unit_version
        WHERE unit_id = %(unit)s
          AND pending IS DISTINCT FROM 'starts'
          AND NOT withdrawn
          AND (
            (time_basis = 'valid'
              AND (valid_from IS NULL OR valid_from <= %(d)s)
              AND (valid_to IS NULL OR valid_to > %(d)s))
            OR (time_basis = 'observed'
              AND (observed_from AT TIME ZONE 'Europe/Stockholm')::date <= %(d)s
              AND (observed_to IS NULL
                   OR (observed_to AT TIME ZONE 'Europe/Stockholm')::date > %(d)s))
          )
        ORDER BY valid_from NULLS FIRST, id
        """,
        {"unit": unit_id, "d": as_of},
    ).fetchall()
    keys = ("id", "text", "valid_from", "valid_to", "amended_by", "pending", "dates_estimated")
    return [dict(zip(keys, row, strict=True)) for row in rows]


# --- one unit ---------------------------------------------------------------------------------


def _apply_unit(
    conn: psycopg.Connection,
    parsed: list[ParsedUnit],
    stored: list[_Stored],
    raw: RawFetch,
    download_id: int,
    seen_on: date,
    statute: Statute,
    report: IngestReport,
) -> None:
    # Match one-to-one by text, in order; identical texts with different dates stay distinct.
    unmatched_stored = list(stored)
    new: list[ParsedUnit] = []
    for wording in parsed:
        h = text_hash(wording.text)
        hit = next((s for s in unmatched_stored if s.text_hash == h), None)
        if hit is None:
            new.append(wording)
            continue
        unmatched_stored.remove(hit)
        if _update_dates(conn, hit, wording):
            report.updated_versions += 1
        else:
            report.unchanged_versions += 1

    # A new wording with no start date (and not waiting for a decision) replaces a gone
    # current wording now: both ends of the change are estimated as the day it was seen.
    undated_replacement = any(
        w.valid_from is None
        and w.pending != "starts"
        and (w.valid_to is None or w.valid_to > seen_on)
        for w in new
    )
    estimated_change = False
    for gone in unmatched_stored:
        estimated_change |= _close(
            conn, gone, new, undated_replacement, raw, seen_on, statute, report
        )
    if estimated_change:
        report.estimated.append(stored[0].unit_id)

    for wording in new:
        estimate = (
            estimated_change
            and wording.valid_from is None
            and wording.pending != "starts"
            and (wording.valid_to is None or wording.valid_to > seen_on)
        )
        _insert_version(conn, wording, raw, download_id, seen_on if estimate else None)
        report.new_versions += 1


def _update_dates(conn: psycopg.Connection, stored: _Stored, parsed: ParsedUnit) -> bool:
    """Bring a still-present wording's dates in line with what the source says now."""
    if parsed.valid_from is not None:
        valid_from, estimated = parsed.valid_from, False
    elif parsed.pending == "starts":  # the start date was replaced by a government decision
        valid_from, estimated = None, False
    else:  # keep a start we knew (from the amending act, or our own estimate)
        valid_from, estimated = stored.valid_from, stored.dates_estimated
    valid_to = parsed.valid_to  # a withdrawn end date is cleared
    pending = parsed.pending
    if (valid_from, valid_to, pending, estimated) == (
        stored.valid_from,
        stored.valid_to,
        stored.pending,
        stored.dates_estimated,
    ):
        return False
    if stored.dates_estimated and not estimated and stored.valid_from is not None:
        # The real start replaces our estimate: the wording before it ended then too.
        conn.execute(
            "UPDATE unit_version SET valid_to = %s, dates_estimated = false"
            " WHERE unit_id = %s AND valid_to = %s AND dates_estimated AND id <> %s",
            (valid_from, stored.unit_id, stored.valid_from, stored.id),
        )
    conn.execute(
        "UPDATE unit_version SET valid_from = %s, valid_to = %s, pending = %s,"
        " dates_estimated = %s WHERE id = %s",
        (valid_from, valid_to, pending, estimated, stored.id),
    )
    return True


def _close(
    conn: psycopg.Connection,
    gone: _Stored,
    new: list[ParsedUnit],
    undated_replacement: bool,
    raw: RawFetch,
    seen_on: date,
    statute: Statute,
    report: IngestReport,
) -> bool:
    """Close a wording that is no longer in the text. Returns True if the date is estimated."""
    report.closed_versions += 1
    if gone.pending == "starts":  # never took effect; just no longer announced
        conn.execute(
            "UPDATE unit_version SET observed_to = %s, withdrawn = true WHERE id = %s",
            (raw.fetched_at, gone.id),
        )
        report.withdrawn_versions += 1
        return False

    estimated = False
    valid_to = gone.valid_to
    if undated_replacement and (valid_to is None or seen_on < valid_to):
        # The replacement takes over from the day the change was seen, even if this wording
        # had an end date of its own (the replacement now carries that end date).
        valid_to, estimated = seen_on, True
    elif valid_to is None:
        later_starts = [
            w.valid_from
            for w in new
            if w.valid_from is not None
            and w.pending != "starts"
            and (gone.valid_from is None or w.valid_from > gone.valid_from)
        ]
        if later_starts:
            valid_to = min(later_starts)
        elif (act := statute.repealed.get(gone.unit_id)) and act in statute.in_force:
            valid_to = statute.in_force[act]
        else:
            valid_to, estimated = seen_on, True

    if gone.valid_from is not None and valid_to <= gone.valid_from:
        # It would end before it started: an announced wording that never took effect.
        conn.execute(
            "UPDATE unit_version SET observed_to = %s, withdrawn = true, pending = NULL"
            " WHERE id = %s",
            (raw.fetched_at, gone.id),
        )
        report.withdrawn_versions += 1
        return False

    conn.execute(
        "UPDATE unit_version SET observed_to = %s, valid_to = %s, pending = NULL,"
        " dates_estimated = dates_estimated OR %s WHERE id = %s",
        (raw.fetched_at, valid_to, estimated, gone.id),
    )
    return estimated


# --- rows -------------------------------------------------------------------------------------


def _record_download(conn: psycopg.Connection, raw: RawFetch) -> tuple[int, bool]:
    """Insert the download as evidence, or reuse it if this exact download is already there."""
    existing = conn.execute(
        "SELECT id FROM download WHERE url = %s AND fetched_at = %s AND content_hash = %s",
        (raw.url, raw.fetched_at, raw.content_hash),
    ).fetchone()
    if existing:
        return existing[0], True
    row = conn.execute(
        "INSERT INTO download (source, url, fetched_at, content_hash, storage_path, http_status)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            raw.source,
            raw.url,
            raw.fetched_at,
            raw.content_hash,
            str(raw.storage_path or ""),
            raw.http_status,
        ),
    ).fetchone()
    assert row is not None
    return row[0], False


def _upsert_units(conn: psycopg.Connection, units: list[ParsedUnit]) -> None:
    latest: dict[str, ParsedUnit] = {}
    for unit in units:
        latest[unit.unit_id] = unit  # the last wording's heading wins
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO unit (id, source_type, authority, language, document, heading, url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              source_type = EXCLUDED.source_type, authority = EXCLUDED.authority,
              document = EXCLUDED.document, heading = EXCLUDED.heading, url = EXCLUDED.url
            """,
            [
                (u.unit_id, u.source_type, u.authority, u.language, u.document, u.heading, u.url)
                for u in latest.values()
            ],
        )


def _open_versions(conn: psycopg.Connection, url: str) -> list[_Stored]:
    rows = conn.execute(
        """
        SELECT v.id, v.unit_id, v.text_hash, v.valid_from, v.valid_to, v.pending,
               v.dates_estimated
        FROM unit_version v JOIN download d ON d.id = v.download_id
        WHERE d.url = %s AND v.observed_to IS NULL
        ORDER BY v.id
        """,
        (url,),
    ).fetchall()
    return [_Stored(*row) for row in rows]


def _insert_version(
    conn: psycopg.Connection,
    parsed: ParsedUnit,
    raw: RawFetch,
    download_id: int,
    estimated_start: date | None,
) -> None:
    conn.execute(
        """
        INSERT INTO unit_version (unit_id, text, text_hash, time_basis, valid_from, valid_to,
          amended_by, pending, dates_estimated, observed_from, download_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            parsed.unit_id,
            parsed.text,
            text_hash(parsed.text),
            parsed.time_basis,
            estimated_start or parsed.valid_from,
            parsed.valid_to,
            parsed.amended_by,
            parsed.pending,
            estimated_start is not None,
            raw.fetched_at,
            download_id,
        ),
    )


def _link_transitional(conn: psycopg.Connection, statute: Statute) -> int:
    """Link each transitional point to the versions its act introduced and replaced (D4).

    "Replaced" means: a version of the same unit that ended exactly when one of the act's own
    versions started, by a date from the source (not an estimate).
    """
    count = 0
    for unit in statute.units:
        if unit.source_type != "transitional":
            continue
        conn.execute("DELETE FROM unit_link WHERE from_unit_id = %s", (unit.unit_id,))
        act = unit.document.removeprefix("sfs:")
        for target in dict.fromkeys(t for _, t in unit.links):
            rows = conn.execute(
                """
                SELECT 'transitional_introduced', v.id FROM unit_version v
                WHERE v.unit_id = %(u)s AND v.amended_by = %(act)s AND NOT v.withdrawn
                UNION
                SELECT 'transitional_ended', old.id
                FROM unit_version old JOIN unit_version v
                  ON v.unit_id = old.unit_id AND v.amended_by = %(act)s
                 AND v.valid_from = old.valid_to AND NOT v.withdrawn
                WHERE old.unit_id = %(u)s AND old.id <> v.id
                  AND NOT old.dates_estimated AND NOT old.withdrawn
                """,
                {"u": target, "act": act},
            ).fetchall()
            for kind, version_id in rows:
                inserted = conn.execute(
                    "INSERT INTO unit_link (from_unit_id, to_version_id, kind)"
                    " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (unit.unit_id, version_id, kind),
                )
                count += inserted.rowcount
    return count
