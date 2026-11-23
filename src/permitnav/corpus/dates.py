"""In-force dates from the amendment register (phase 4, slice 2).

The consolidated text marks each amended section with its act ("Lag (2026:385)") but gives no
date for changes already in force, so those wordings were stored without a start ("applies on
any date"). Here they get their start from the register: the act's in-force date, or for a
section never amended (no act marker), the statute's own in-force date.

Conservative by design: only wordings with no start yet are touched (a date from the text or
an earlier run is never overwritten); acts that are partly "on a day the government decides",
undated in the register or with several dates are skipped; a date that would overlap another
wording of the same section is skipped and reported. Every date set is a "dates" row in the
change log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import psycopg

from permitnav.parsing.sfsr import FeedItem, Register


@dataclass
class DatesReport:
    from_acts: int = 0  # wordings dated by their amending act
    original: int = 0  # never-amended wordings dated by the statute's own in-force date
    transitional: int = 0  # transitional provisions dated by their act
    skipped: dict[str, int] = field(default_factory=dict)  # why a wording stayed undated
    conflicts: list[str] = field(default_factory=list)  # units where a date would overlap


def record_acts(
    conn: psycopg.Connection, registers: list[Register], feed: list[FeedItem], seen: datetime
) -> list[FeedItem]:
    """Store what the register and the feed say about each amending act. Returns the feed
    items not seen before and not yet in the register: early notice of a new act."""
    known = {row[0] for row in conn.execute("SELECT sfs FROM amending_act").fetchall()}
    with conn.transaction():
        for register in registers:
            for act in register.acts:
                conn.execute(
                    """
                    INSERT INTO amending_act (sfs, statute, title, scope, in_force,
                      partly_pending, transitional, in_register, first_seen, last_seen)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s, %s)
                    ON CONFLICT (sfs) DO UPDATE SET title = EXCLUDED.title,
                      scope = EXCLUDED.scope, in_force = EXCLUDED.in_force,
                      partly_pending = EXCLUDED.partly_pending,
                      transitional = EXCLUDED.transitional, in_register = true,
                      statute = EXCLUDED.statute,
                      last_seen = EXCLUDED.last_seen
                    """,
                    (
                        act.sfs,
                        register.statute,
                        act.title,
                        act.scope,
                        act.in_force,
                        act.partly_pending,
                        act.transitional,
                        seen,
                        seen,
                    ),
                )
        for item in feed:
            conn.execute(
                """
                INSERT INTO amending_act (sfs, statute, title, published, first_seen, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (sfs) DO UPDATE SET published = EXCLUDED.published,
                  last_seen = EXCLUDED.last_seen
                """,
                (item.sfs, item.statute, item.title, item.published, seen, seen),
            )
    in_register = {a.sfs for r in registers for a in r.acts}
    return [i for i in feed if i.sfs not in known and i.sfs not in in_register]


def apply_in_force_dates(
    conn: psycopg.Connection, register: Register, download_id: int, seen: datetime
) -> DatesReport:
    """Give undated wordings of one statute (and its acts' transitional provisions) their
    start from the register. Each update is its own savepoint: an overlap skips one wording."""
    report = DatesReport()
    # The overlap rule is deferred to commit by default; checked per update here, so an
    # overlap fails inside its savepoint and skips only that wording.
    conn.execute("SET CONSTRAINTS one_wording_per_day IMMEDIATE")
    acts = {a.sfs: a for a in register.acts}
    document = f"sfs:{register.statute}"
    rows = conn.execute(
        """
        SELECT v.id, v.unit_id, v.amended_by, v.valid_to, u.source_type, u.document
        FROM unit_version v JOIN unit u ON u.id = v.unit_id
        WHERE (u.document = %(doc)s AND u.source_type = 'statute'
               OR u.source_type = 'transitional' AND u.document = ANY(%(acts)s))
          AND v.time_basis = 'valid' AND v.valid_from IS NULL
          AND v.pending IS DISTINCT FROM 'starts'
          AND NOT v.withdrawn AND v.observed_to IS NULL
        ORDER BY v.unit_id, v.id
        """,
        {"doc": document, "acts": [f"sfs:{sfs}" for sfs in acts]},
    ).fetchall()
    for version_id, unit_id, amended_by, valid_to, source_type, unit_document in rows:
        if source_type == "transitional":
            act = acts.get(unit_document.removeprefix("sfs:"))
            source = f"act {act.sfs}" if act else ""
            if act is None:  # never the statute's own date for an act's provisions
                _skip(report, "act not in the register")
                continue
        elif amended_by is None:
            act, source = None, f"{register.statute} itself"
        else:
            act = acts.get(amended_by)
            source = f"act {amended_by}"
            if act is None:
                _skip(report, "act not in the register")
                continue
        if act is not None and (act.in_force is None or act.partly_pending):
            why = "partly pending" if act.partly_pending else "no single date in the register"
            _skip(report, why)
            continue
        start: date | None = act.in_force if act is not None else register.in_force
        if start is None:
            _skip(report, "no single date in the register")
            continue
        if valid_to is not None and start >= valid_to:
            report.conflicts.append(f"{unit_id}: start {start} not before its end {valid_to}")
            continue
        try:
            with conn.transaction():  # a savepoint: an overlap undoes only this update
                conn.execute(
                    "UPDATE unit_version SET valid_from = %s WHERE id = %s", (start, version_id)
                )
                conn.execute(
                    "INSERT INTO change_log (observed_at, download_id, unit_id, kind,"
                    " old_version_id, new_version_id, diff)"
                    " VALUES (%s, %s, %s, 'dates', %s, %s, %s)",
                    (
                        seen,
                        download_id,
                        unit_id,
                        version_id,
                        version_id,
                        f"valid_from: unknown -> {start} (register: {source})",
                    ),
                )
        except psycopg.errors.ExclusionViolation:
            report.conflicts.append(f"{unit_id}: {start} would overlap another wording")
            continue
        if source_type == "transitional":
            report.transitional += 1
        elif amended_by is None:
            report.original += 1
        else:
            report.from_acts += 1
    conn.execute("SET CONSTRAINTS one_wording_per_day DEFERRED")  # back to the default
    return report


def _skip(report: DatesReport, reason: str) -> None:
    report.skipped[reason] = report.skipped.get(reason, 0) + 1
