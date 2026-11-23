"""Golden-set drift (phase 4, slice 5; D17).

A golden item is written against the sources as they read on its `as_of` date. Evals keep
running it with that date, so it never turns wrong by itself, but it can stop describing the
rules people ask about now. Per expected unit of each item written "as of today" (items that
test a past or future date on purpose, `version-dependent`, are left out):

- `newer wording`: a wording in force today started after the item's date (a new act);
- `page changed`: Migrationsverket's page reads differently today than on the item's date;
- `corrected`: the wording the item was written against was later replaced by a corrected
  reading of the same source (a parser fix, a published correction);
- `upcoming`: a dated wording that takes effect after today (still valid until then);
- `gone`: no wording applies on the item's date any more (the eval stops on this too).

All but `upcoming` mean the item needs review: D17 keeps the old item as a historical test
and adds a fresh copy dated today, written against today's sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import psycopg

from permitnav.evals.golden import GoldenItem

NEEDS_REVIEW = ("newer wording", "page changed", "corrected", "gone")


@dataclass(frozen=True)
class Drift:
    item: str
    unit: str
    kind: str
    detail: str


SUCCESSOR = re.compile(r"^(?P<original>.+)-(?P<day>\d{8})$")


def golden_drift(conn: psycopg.Connection, items: list[GoldenItem], today: date) -> list[Drift]:
    """Drift of the items that describe today's rules: not version-dependent ones, and not
    ones superseded by a fresh copy (`<id>-YYYYMMDD` in the same set): those now test their own
    date only (D17), and only the newest copy is followed."""
    superseded = {m["original"] for i in items if (m := SUCCESSOR.match(i.id))}
    for i in items:  # a copy of a copy supersedes the earlier copy too
        if (m := SUCCESSOR.match(i.id)) and any(
            o.id.startswith(f"{m['original']}-") and o.id > i.id for o in items
        ):
            superseded.add(i.id)
    found: list[Drift] = []
    for item in items:
        if item.type == "version-dependent" or item.review != "checked":
            continue
        if item.id in superseded:
            continue
        units = dict.fromkeys(unit for group in item.expected for unit in group)
        for unit in units:
            found += _unit_drift(conn, item, unit, today)
    return found


def _unit_drift(conn: psycopg.Connection, item: GoldenItem, unit: str, today: date) -> list[Drift]:
    rows = conn.execute(
        """
        SELECT v.id, v.time_basis, v.valid_from, v.valid_to, v.pending, v.withdrawn,
               (v.observed_from AT TIME ZONE 'Europe/Stockholm')::date,
               (v.observed_to AT TIME ZONE 'Europe/Stockholm')::date, v.text_hash, v.amended_by
        FROM unit_version v WHERE v.unit_id = %s ORDER BY v.id
        """,
        (unit,),
    ).fetchall()
    versions = [_Version(*row) for row in rows]
    then = [v for v in versions if not v.withdrawn and v.applies(item.as_of)]
    drift: list[Drift] = []
    if not then:
        return [Drift(item.id, unit, "gone", f"nothing applies on {item.as_of}")]
    written = then[-1]
    if written.time_basis == "observed":
        now = [v for v in versions if not v.withdrawn and v.applies(today)]
        if not now:
            drift.append(Drift(item.id, unit, "gone", f"the page is gone ({today})"))
        elif now[-1].text_hash != written.text_hash:
            drift.append(
                Drift(item.id, unit, "page changed", f"reads differently since {now[-1].seen}")
            )
        return drift
    current = [v for v in versions if not v.withdrawn and v.applies(today)]
    later = [
        v for v in versions
        if not v.withdrawn and v.pending != "starts" and v.valid_from is not None
        and v.valid_from > today
    ]  # fmt: skip
    if not current and not later:  # repealed or dropped, nothing in its place
        drift.append(Drift(item.id, unit, "gone", f"no wording applies on {today}"))
    elif written.valid_to is not None and written.valid_to > today and not later:
        drift.append(Drift(item.id, unit, "upcoming", f"ends on {written.valid_to}, nothing after"))
    for v in versions:
        if v.withdrawn or v.pending == "starts" or v.valid_from is None:
            continue
        act = f" ({v.amended_by})" if v.amended_by else ""
        if item.as_of < v.valid_from <= today:
            drift.append(Drift(item.id, unit, "newer wording", f"from {v.valid_from}{act}"))
        elif v.valid_from > today:
            drift.append(Drift(item.id, unit, "upcoming", f"from {v.valid_from}{act}"))
    for v in versions:  # the wording of the item's date, read again since the item was written
        replaced = v.withdrawn and v.ended is not None and v.ended > item.as_of
        if replaced and v.applies(item.as_of, ignore_withdrawn=True):
            drift.append(Drift(item.id, unit, "corrected", f"reading replaced on {v.ended}"))
            break
    return drift


@dataclass(frozen=True)
class _Version:
    id: int
    time_basis: str
    valid_from: date | None
    valid_to: date | None
    pending: str | None
    withdrawn: bool
    seen: date
    ended: date | None  # observed_to: when it stopped being seen or was withdrawn
    text_hash: str
    amended_by: str | None

    def applies(self, day: date, ignore_withdrawn: bool = False) -> bool:
        if self.withdrawn and not ignore_withdrawn:
            return False
        if self.time_basis == "observed":
            return self.seen <= day and (self.ended is None or self.ended > day)
        if self.pending == "starts":
            return False
        return (self.valid_from is None or self.valid_from <= day) and (
            self.valid_to is None or self.valid_to > day
        )


def fresh_copies(items: list[GoldenItem], raw: dict[str, dict], drift: list[Drift], today: date):
    """D17: a draft copy of each item that needs review, dated today, for a person to check
    against today's sources (the old item stays as a historical test). Yields raw JSON rows."""
    reasons: dict[str, list[str]] = {}
    for d in drift:
        if d.kind in NEEDS_REVIEW:
            reasons.setdefault(d.item, []).append(f"{d.unit}: {d.kind}, {d.detail}")
    suffix = today.strftime("%Y%m%d")
    drafted = {item.id for item in items if item.id in reasons}
    for item in items:
        if item.id not in reasons:
            continue
        row = dict(raw[item.id])
        row["id"] = f"{item.id}-{suffix}"
        original = row.get("translation_of")
        if original and original in drafted:  # else it keeps pointing at the existing original
            row["translation_of"] = f"{original}-{suffix}"
        row["as_of"] = today.isoformat()
        row["review"] = "draft"
        row["notes"] = (
            f"Fresh copy (D17) of {item.id}, whose sources changed since {item.as_of}: "
            + "; ".join(reasons[item.id])
            + ". Re-check the expected units and the reference answer against today's sources."
        )
        yield row
