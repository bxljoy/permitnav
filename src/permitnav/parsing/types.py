"""Types shared by the parsers (docs/DESIGN.md, "Phase 1 internals")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class ParsedUnit:
    """One citable unit as read from one download (D2).

    A statute section that appears twice in the consolidated text (an expiring and an
    upcoming wording) becomes two ParsedUnits with the same unit_id and different dates.
    """

    unit_id: str  # e.g. "sfs:2005:716/6kap/2§"
    source_type: str  # statute | transitional | web_page | position_paper | ruling
    authority: str  # law | ordinance | regulation | ruling | position_paper | web_page
    language: str  # sv | en
    document: str  # e.g. "sfs:2005:716"
    heading: str  # chapter, subheading and section, or page heading path
    url: str
    text: str
    time_basis: str = "valid"  # valid | observed (D5)
    valid_from: date | None = None
    valid_to: date | None = None
    amended_by: str | None = None  # e.g. "2026:295"
    # Set when a change takes effect "on the day the government decides" (no date yet):
    # "starts" = this wording is not in force yet; "ends" = this wording will end then.
    pending: str | None = None
    links: tuple[tuple[str, str], ...] = field(default=())  # (kind, target unit_id)
