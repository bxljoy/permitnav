"""Rebuild the older wordings of statute sections from the amending acts (phase 4, slice 3).

The consolidated text holds only today's wording of each section. Every amending act prints
the new wording of the sections it changes, and a footnote naming the act whose wording it
replaced ("Senaste lydelse 2018:319"). So, walking back from a section's current wording:

    current wording, from act A (in force d_A)
      A's footnote for the section -> B: the wording before was B's text, from d_B until d_A
      B's footnote -> C: before that, C's text, from d_C until d_B   … and so on,

until a wording that started on or before the cut-off (2023-01-01), or the statute's own
text (2006), or a step that cannot be taken. Stops, each counted with its reason:
  - the act changed the section without printing it (a word swap, "ordet … ska bytas ut");
  - the section is new in that act, or re-added after a repeal: nothing applied before.
    A section without a footnote is not proof that it was never amended: an act that
    replaces a whole chapter ("upph. 4 kap.; nya 4 kap.") prints its sections without
    footnotes. So the register decides: the latest earlier act whose scope names the
    section (or its chapter) gave the wording before; if it repealed it, there was none;
    if no act names it, the wording before is the statute's as enacted;
  - the earlier act's in-force date is not known (partly pending, several dates);
  - the period is already covered by a stored wording (the overlap rule decides).

A renumbered section ("Senaste lydelse av tidigare 9 § 2013:606") had another number before:
its earlier wordings are stored under that number, which is what the section was called then.

Rebuilt wordings are ordinary dated versions (time basis 'valid'), with the act's PDF as
their download, so the consolidated-text ingest never treats them as its own.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

import psycopg

from permitnav.corpus.store import text_hash
from permitnav.parsing.sfs_act import ActSection, ParsedAct
from permitnav.parsing.sfsr import Register, ScopeItem, parse_scope, scope_names

CUTOFF = date(2023, 1, 1)


@dataclass
class HistoryReport:
    wordings: int = 0  # older wordings stored
    unchanged: int = 0  # older wordings already stored by an earlier run, identical
    replaced: int = 0  # rebuilt by an earlier run, read differently now (a parser fix)
    units_created: int = 0  # sections under a former number that had no unit yet
    sections: int = 0  # current wordings whose history was walked
    stops: Counter[str] = field(default_factory=Counter)  # why a walk ended before the cut-off
    reached_cutoff: int = 0  # walks that reached a wording in force on the cut-off date
    missing_acts: set[str] = field(default_factory=set)  # act PDFs a walk needed, not at hand
    covered: list[str] = field(default_factory=list)  # periods another wording already holds


@dataclass(frozen=True)
class Statute:
    sfs: str  # "2005:716"
    in_force: date  # its own in-force date, for the wording as enacted
    kind: str  # "Lag" or "Förordning": how its act markers read


def rebuild_history(
    conn: psycopg.Connection,
    statute: Statute,
    acts: Mapping[str, ParsedAct],  # every act PDF parsed so far, the statute itself included
    in_force: Mapping[str, date | None],  # each amending act's single in-force date
    kinds: Mapping[str, str],  # each act's marker word ("Lag"/"Förordning"), from its title
    downloads: Mapping[str, int],  # each act's PDF download id
    seen: datetime,
    scopes: Mapping[str, list[ScopeItem]],  # the register's scope of each amending act
    cutoff: date = CUTOFF,
) -> HistoryReport:
    report = HistoryReport()
    timeline = _Timeline(in_force, scopes)
    document = f"sfs:{statute.sfs}"
    rows = conn.execute(
        """
        SELECT v.unit_id, v.amended_by, v.valid_from FROM unit_version v
        JOIN unit u ON u.id = v.unit_id
        JOIN download d ON d.id = v.download_id AND d.source <> 'sfs-act'  -- walk from the text
        WHERE u.document = %s AND u.source_type = 'statute' AND v.time_basis = 'valid'
          AND v.observed_to IS NULL AND NOT v.withdrawn AND v.pending IS DISTINCT FROM 'starts'
          AND v.amended_by IS NOT NULL AND v.valid_from > %s
        ORDER BY v.unit_id
        """,
        (document, cutoff),
    ).fetchall()
    conn.execute("SET CONSTRAINTS one_wording_per_day IMMEDIATE")
    for unit_id, act_sfs, start in rows:
        report.sections += 1
        chapter, number = _numbers(unit_id)
        _walk(conn, report, statute, acts, in_force, kinds, downloads, seen, cutoff, timeline,
              unit_id, chapter, number, act_sfs, start)  # fmt: skip
    conn.execute("SET CONSTRAINTS one_wording_per_day DEFERRED")
    return report


def _walk(
    conn: psycopg.Connection,
    report: HistoryReport,
    statute: Statute,
    acts: Mapping[str, ParsedAct],
    in_force: Mapping[str, date | None],
    kinds: Mapping[str, str],
    downloads: Mapping[str, int],
    seen: datetime,
    cutoff: date,
    timeline: _Timeline,
    unit_id: str,
    chapter: str,
    number: str,
    act_sfs: str,
    start: date,
) -> None:
    template = unit_id
    while start > cutoff:
        act = acts.get(act_sfs)
        if act is None:
            report.missing_acts.add(act_sfs)
            return _stop(report, "act PDF not available")
        section = _find(act, chapter, number)
        if section is None:
            return _stop(report, "section not printed in its act (word swap or renumbering)")
        if section.footnote_missing:
            return _stop(report, "footnote text not found")
        if section.previous is None:
            if "upphävd" in section.footnote:
                return _stop(report, "re-added after a repeal: nothing before")
            if not _asks_register(section):
                return _stop(report, "footnote not understood")
            found = timeline.last_change_before(act_sfs, chapter, number)
            if found == "undated":
                return _stop(report, "an undated act in between may have changed it")
            if found is None:
                if _find(acts[statute.sfs], chapter, number) is None:
                    return _stop(report, "new section: nothing before")
                previous, previous_start = statute.sfs, statute.in_force
            elif found[1] == "upph":
                return _stop(report, "repealed before this act: nothing before")
            else:
                previous = found[0]
                previous_start = in_force.get(previous)
                if previous_start is None:
                    return _stop(report, "earlier act's in-force date not known")
        else:
            previous = section.previous
            previous_start = in_force.get(previous)
            if previous_start is None:
                return _stop(report, "earlier act's in-force date not known")
        if section.old_number:  # it had another number until this act
            chapter, number = _former(section, chapter)
        earlier = acts.get(previous)
        if earlier is None:
            report.missing_acts.add(previous)
            return _stop(report, "act PDF not available")
        wording = _find(earlier, chapter, number)
        if wording is None:
            return _stop(report, "section not printed in the earlier act")
        if wording.footnote_missing:  # its page was not read cleanly: the text may be cut
            return _stop(report, "earlier act's page not read cleanly")
        if section.old_number:
            unit_id = _ensure_unit(conn, report, template, statute, chapter, number)
        if previous_start >= start:
            return _stop(report, "dates out of order")
        marker = (
            "" if previous == statute.sfs else f" {kinds.get(previous, statute.kind)} ({previous})."
        )
        amended_by = None if previous == statute.sfs else previous
        text = wording.text + marker
        stored = _store(conn, unit_id, text, (previous_start, start), amended_by,
                        downloads[previous], seen)  # fmt: skip
        if stored == "text":  # e.g. walking back from a wording due in 2027 reaches today's
            return _stop(report, "reached a wording the consolidated text holds")
        if stored == "covered":
            report.covered.append(f"{unit_id} {previous_start}..{start}")
            return _stop(report, "period already covered by another rebuilt wording")
        if stored == "same":
            report.unchanged += 1
        elif stored == "replaced":
            report.replaced += 1
        else:
            report.wordings += 1
        if previous == statute.sfs:
            return _stop(report, "reached the statute as enacted")
        act_sfs, start = previous, previous_start
    report.reached_cutoff += 1


class _Timeline:
    """The statute's amending acts in the order they took effect, with what each touched."""

    def __init__(
        self, in_force: Mapping[str, date | None], scopes: Mapping[str, list[ScopeItem]]
    ) -> None:
        self._in_force = in_force
        self._scopes = scopes

    def last_change_before(
        self, act_sfs: str, chapter: str, number: str
    ) -> tuple[str, str] | str | None:
        """The latest act in force before `act_sfs` whose scope names the section, with its
        operation ("upph", "ändr", "ny"); None if none; "undated" if an act without a usable
        date names it (it might fall in between)."""
        until = self._in_force.get(act_sfs)
        if until is None:
            return "undated"
        best: tuple[date, str, str] | None = None
        undated: list[str] = []
        for sfs, items in self._scopes.items():
            if sfs == act_sfs:
                continue
            ops = [i.op for i in items if scope_names(i, chapter, number)]
            if not ops:
                continue
            when = self._in_force.get(sfs)
            if when is None:
                undated.append(sfs)
            elif when < until and (best is None or when > best[0]):
                # one act can repeal and re-add a section: the addition is what remained
                op = "ny" if "ny" in ops else ("ändr" if "ändr" in ops else ops[0])
                best = (when, sfs, op)
        # an undated act numbered between the one found and this one might come in between
        floor = _number(best[1]) if best else (0, 0)
        if any(floor < _number(sfs) < _number(act_sfs) for sfs in undated):
            return "undated"
        return (best[1], best[2]) if best else None


def _asks_register(section: ActSection) -> bool:
    """A section whose footnote names no earlier wording ("Senaste lydelse …"), so the
    register says which act gave it: no footnote, or one like "Ändringen innebär …"."""
    return (
        section.previous is None
        and not section.footnote_missing
        and "upphävd" not in section.footnote
        and "Senaste lydelse" not in section.footnote
    )


def _number(sfs: str) -> tuple[int, int]:
    year, number = sfs.split(":")
    return int(year), int(number)


def needed_acts(
    statute: str,
    acts: Mapping[str, ParsedAct],
    in_force: Mapping[str, date | None],
    scopes: Mapping[str, list[ScopeItem]],
    cutoff: date = CUTOFF,
) -> set[str]:
    """The act PDFs a rebuild back to the cut-off reads: every act in force after it (or not
    dated), the statute itself, and each act one of their sections names as the wording
    before. Returns those not in `acts` yet; fetch them and call again until it is empty."""
    wanted = {statute} | {
        sfs for sfs in scopes if (when := in_force.get(sfs)) is None or when > cutoff
    }
    timeline = _Timeline(in_force, scopes)
    for sfs in sorted(wanted & acts.keys()):
        for section in acts[sfs].sections:
            if section.previous:
                wanted.add(section.previous)
            elif sfs != statute and _asks_register(section):
                found = timeline.last_change_before(sfs, section.chapter, section.section)
                if isinstance(found, tuple) and found[1] != "upph":
                    wanted.add(found[0])
    return wanted - acts.keys()


def _stop(report: HistoryReport, reason: str) -> None:
    report.stops[reason] += 1


def _numbers(unit_id: str) -> tuple[str, str]:
    """'sfs:2005:716/6akap/18a§' -> ('6 a', '18 a')."""
    found = re.search(r"/(\d+)([a-z]?)kap/(\d+)([a-z]?)§$", unit_id)
    if found is None:
        raise ValueError(f"not a chapter/section unit: {unit_id}")
    chapter = f"{found.group(1)} {found.group(2)}".strip()
    return chapter, f"{found.group(3)} {found.group(4)}".strip()


def _find(act: ParsedAct, chapter: str, number: str) -> ActSection | None:
    return next((s for s in act.sections if s.chapter == chapter and s.section == number), None)


def _former(section: ActSection, chapter: str) -> tuple[str, str]:
    """'9' (same chapter) or '8 a kap. 4' -> (chapter, number)."""
    assert section.old_number is not None
    if " kap. " in section.old_number:
        old_chapter, old_number = section.old_number.split(" kap. ")
        return old_chapter, old_number
    return chapter, section.old_number


def _ensure_unit(
    conn: psycopg.Connection,
    report: HistoryReport,
    template: str,
    statute: Statute,
    chapter: str,
    number: str,
) -> str:
    unit_id = f"sfs:{statute.sfs}/{chapter.replace(' ', '')}kap/{number.replace(' ', '')}§"
    if conn.execute("SELECT 1 FROM unit WHERE id = %s", (unit_id,)).fetchone():
        return unit_id
    row = conn.execute(
        "SELECT source_type, authority, language, document, url FROM unit WHERE id = %s",
        (template,),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO unit (id, source_type, authority, language, document, heading, url)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (unit_id, row[0], row[1], row[2], row[3], f"{chapter} kap. {number} §", row[4]),
    )
    report.units_created += 1
    return unit_id


def _store(
    conn: psycopg.Connection,
    unit_id: str,
    text: str,
    period: tuple[date, date],
    amended_by: str | None,
    download_id: int,
    seen: datetime,
) -> str:
    """One rebuilt wording: "stored"; "same" if an earlier run stored it already;
    "replaced" if an earlier run rebuilt (part of) that period otherwise (the old rows are
    withdrawn, kept as evidence); "text" if the consolidated text's own wording holds (part
    of) that period, or "covered" if another rebuilt one of this run does: then nothing is
    changed."""
    same = conn.execute(
        """
        SELECT 1 FROM unit_version v JOIN download d ON d.id = v.download_id
        WHERE v.unit_id = %s AND v.valid_from = %s AND v.valid_to = %s AND NOT v.withdrawn
          AND d.source = 'sfs-act' AND v.text_hash = %s
        """,
        (unit_id, *period, text_hash(text)),
    ).fetchone()
    if same:
        return "same"
    # An earlier run's rebuild of (part of) this period, read differently then (a parser fix,
    # a corrected in-force date): this run's reading replaces it. Rows of this run stay.
    earlier = conn.execute(
        """
        SELECT v.id FROM unit_version v JOIN download d ON d.id = v.download_id
        WHERE v.unit_id = %s AND NOT v.withdrawn AND d.source = 'sfs-act'
          AND v.observed_from < %s AND daterange(v.valid_from, v.valid_to) && daterange(%s, %s)
        """,
        (unit_id, seen, *period),
    ).fetchall()
    try:
        with conn.transaction():  # a savepoint: an overlap undoes only this wording
            for (old_id,) in earlier:
                conn.execute(
                    "UPDATE unit_version SET withdrawn = true, observed_to = %s WHERE id = %s",
                    (seen, old_id),
                )
            conn.execute(
                """
                INSERT INTO unit_version (unit_id, text, text_hash, time_basis, valid_from,
                  valid_to, amended_by, observed_from, download_id)
                VALUES (%s, %s, %s, 'valid', %s, %s, %s, %s, %s)
                """,
                (
                    unit_id,
                    text,
                    text_hash(text),
                    *period,
                    amended_by,
                    seen,
                    download_id,
                ),
            )
    except psycopg.errors.ExclusionViolation:
        from_text = conn.execute(
            """
            SELECT 1 FROM unit_version v JOIN download d ON d.id = v.download_id
            WHERE v.unit_id = %s AND NOT v.withdrawn AND d.source <> 'sfs-act'
              AND v.time_basis = 'valid' AND v.pending IS DISTINCT FROM 'starts'
              AND daterange(v.valid_from, v.valid_to) && daterange(%s, %s)
            """,
            (unit_id, *period),
        ).fetchone()
        return "text" if from_text else "covered"
    return "replaced" if earlier else "stored"


@dataclass
class Inputs:
    """What a rebuild of one statute reads: its register and the act PDFs on disk."""

    statute: Statute
    in_force: Mapping[str, date | None]
    kinds: Mapping[str, str]
    scopes: Mapping[str, list[ScopeItem]]


def inputs_from_register(register: Register, kind: str) -> Inputs:
    """From the parsed register: each act's single in-force date (none if partly pending),
    its marker word ("Lag (2026:385)" -> "Lag") and its scope."""
    if register.in_force is None:
        raise ValueError(f"register for {register.statute}: no in-force date of its own")
    return Inputs(
        statute=Statute(register.statute, register.in_force, kind),
        in_force={a.sfs: None if a.partly_pending else a.in_force for a in register.acts},
        kinds={a.sfs: a.title.split(" ", 1)[0] or kind for a in register.acts},
        scopes={a.sfs: parse_scope(a.scope) for a in register.acts},
    )
