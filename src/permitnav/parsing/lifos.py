"""Migrationsverket's legal position papers in Lifos (phase 4, slice 5).

Lifos's search pages are closed to crawlers (robots.txt: `Disallow: /sokning*`, added by
Migrationsverket 2026-04-15), so PermitNav never fetches them. A person runs the search in a
browser and saves the result pages; `parse_results` reads those saved pages. Each hit gives:

    <a title="Rättsligt ställningstagande. Ordningen för … - RS/005/2022 (Version 10.0)"
       href="https://lifos.migrationsverket.se/dokument?documentSummaryId=50385">
    <div id="hitsMetaData"> Källa: Migrationsverket · Upphovsdatum: 2026-11-02 · Dokumentnr: 50385

Single document pages and the RSS feed are allowed and are what the pipeline fetches.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date

DOCUMENT_URL = "https://lifos.migrationsverket.se/dokument?documentSummaryId={id}"
RS = re.compile(r"RS/(\d{3})/(\d{4})")
VERSION = re.compile(r"\(\s*version\s+(\d+(?:\.\d+)?)\s*\)", re.I)
HIT = re.compile(  # one hit: its header link, then everything up to the next hit's header
    r'class="hitsResults hitsResultHeader"[^>]*>\s*<a\s+title="(?P<title>[^"]*)"\s+'
    r'href="[^"]*documentSummaryId=(?P<id>\d+)"'
    r'(?P<rest>.*?)(?=class="hitsResults hitsResultHeader"|$)',
    re.S,
)


@dataclass(frozen=True)
class LifosHit:
    document_id: int
    title: str  # "Rättsligt ställningstagande. Ordningen för … - RS/005/2022 (Version 10.0)"
    rs: str | None  # "RS/005/2022"
    version: str | None  # "10.0"; None when the title gives none
    date: date | None  # "Upphovsdatum"
    source: str  # "Migrationsverket"

    @property
    def url(self) -> str:
        return DOCUMENT_URL.format(id=self.document_id)

    @property
    def is_position_paper(self) -> bool:
        return self.title.lower().startswith("rättsligt ställningstagande") and self.rs is not None


def parse_results(page: str) -> list[LifosHit]:
    """The hits of one saved Lifos result page, in page order."""
    hits = []
    for match in HIT.finditer(page):
        title = re.sub(r"\s+", " ", html.unescape(match["title"])).strip()
        meta = html.unescape(re.sub(r"<[^>]+>", " ", match["rest"]))
        rs = RS.search(title)
        version = VERSION.search(title)
        when = re.search(r"Upphovsdatum:\s*(\d{4}-\d{2}-\d{2})", meta)
        source = re.search(r"Källa:\s*([^\n·]+?)\s{2,}", meta + "  ")
        hits.append(
            LifosHit(
                document_id=int(match["id"]),
                title=title,
                rs=rs.group(0) if rs else None,
                version=_normal_version(version.group(1)) if version else None,
                date=date.fromisoformat(when.group(1)) if when else None,
                source=source.group(1).strip() if source else "",
            )
        )
    return hits


def _normal_version(value: str) -> str:
    return value if "." in value else f"{value}.0"


# --- the curated list --------------------------------------------------------------------

# Title words that put a paper in scope (work permits and the people around them), and words
# that put it out (asylum, return, detention, citizenship, procedure). A paper matching both,
# or neither, goes to review: a person decides. Matched on the lower-cased title.
IN_SCOPE = {
    "work": ("arbetstillstånd", "arbetstagare", "anställning", "arbetsgivar", "blåkort",
             "säsongsarbet", "forskare", "näringsverksamhet", "spårbyte", "företagsintern",
             "rätt att arbeta"),
    "family": ("anknytning", "anhörig", "familjemedlem", "familjeåterförening"),
    "maintenance": ("försörjningskrav", "egen försörjning"),
    "permanent residence": ("permanent uppehållstillstånd", "varaktigt"),
    "extension": ("förlängning av uppehållstillstånd", "förlängning av arbetstillstånd",
                  "fortsatt uppehållstillstånd"),
    "several grounds": ("flera grunder",),
    "revocation": ("återkallelse av",),
    "conduct": ("vandel",),
}  # fmt: skip
OUT_OF_SCOPE = (
    "asyl", "skydd", "förvar", "verkställighet", "återvänd", "återvandring", "avvisning",
    "utvisning", "utvisad", "medborgarskap", "tolk", "biträde", "ombud", "säkerhetsärend", "pass ",
    "pass)", "00-pass", "hemlandspass", "schengens informationssystem", " sis", "mottagande",
    "frivillig avresa", "flykting", "europakonventionen", "belastningsregister", "identitet",
    "medicinska", "dublin", "åldersbedömning", "barns ålder i ärenden om asyl", "21 kap.",
    "lma", "gymnasielagen", "återreseförbud", "preskription", "palestina", "förföljelse",
    "vidarebosättning", "säkerhetsavdelning", "konfliktbedömning", "barns bästa", "höra barn",
    "coronautbrott", "fristadskonstnär", "artikel 8", "12 kap.", "nytt land",
)  # fmt: skip


@dataclass(frozen=True)
class Paper:
    key: str  # the RS number, or the title without its version when the title has none
    latest: LifosHit
    superseded: tuple[LifosHit, ...]  # older versions found in the same results
    topics: tuple[str, ...]
    suggestion: str  # include | exclude | review


def paper_key(hit: LifosHit) -> str:
    if hit.rs:
        return hit.rs
    return re.sub(r"\s*\(\s*version[^)]*\)\s*$", "", hit.title, flags=re.I).strip()


def version_order(hit: LifosHit) -> tuple[int, ...]:
    """'10.0' > '9.0' > no version (the first version has no suffix)."""
    return tuple(int(p) for p in (hit.version or "1.0").split("."))


def classify(title: str) -> tuple[tuple[str, ...], str]:
    lowered = title.lower()
    topics = tuple(t for t, words in IN_SCOPE.items() if any(w in lowered for w in words))
    out = any(w in lowered for w in OUT_OF_SCOPE)
    if topics and not out:
        return topics, "include"
    if out and not topics:
        return topics, "exclude"
    return topics, "review"


def curate(hits: list[LifosHit]) -> list[Paper]:
    """One entry per paper (newest version first in each), newest papers first."""
    groups: dict[str, list[LifosHit]] = {}
    for hit in hits:
        groups.setdefault(paper_key(hit), []).append(hit)
    papers = []
    for key, versions in groups.items():
        ordered = sorted(
            {h.document_id: h for h in versions}.values(),
            key=lambda h: (version_order(h), h.date or date.min),
            reverse=True,
        )
        topics, suggestion = classify(ordered[0].title)
        papers.append(Paper(key, ordered[0], tuple(ordered[1:]), topics, suggestion))
    return sorted(papers, key=lambda p: (p.latest.date or date.min, p.key), reverse=True)


# --- the review file ---------------------------------------------------------------------

COLUMNS: tuple[str, ...] = (
    "decision", "suggestion", "key", "rs", "version", "date", "document_id", "topics", "title",
    "url", "superseded",
)  # fmt: skip


def merge_review(papers: list[Paper], existing: list[dict[str, str]]) -> list[dict[str, str]]:
    """The review file's rows: a person's decisions are kept; a newer version updates its row
    (the decision stays: it is the same paper); a new paper starts undecided unless the
    suggestion is clear (include/exclude), which the person can still change."""
    kept = {row["key"]: row for row in existing}
    rows = []
    for paper in papers:
        hit = paper.latest
        old = kept.pop(paper.key, None)
        if old and _newer(old.get("version"), hit.version or "1.0"):
            # these results are older than what the file already names: keep the newer row
            older = [str(h.document_id) for h in (paper.latest, *paper.superseded)]
            rows.append({**old, "superseded": ";".join(
                dict.fromkeys(s for s in [*old["superseded"].split(";"), *older] if s)
            )})  # fmt: skip
            continue
        decision = (
            old["decision"] if old else ("" if paper.suggestion == "review" else paper.suggestion)
        )
        if old and _newer(hit.version, old.get("version", "")):
            superseded = [old["document_id"], *(str(h.document_id) for h in paper.superseded)]
        else:
            superseded = [str(h.document_id) for h in paper.superseded]
        rows.append(
            {
                "decision": decision,
                "suggestion": paper.suggestion,
                "key": paper.key,
                "rs": hit.rs or "",
                "version": hit.version or "1.0",
                "date": hit.date.isoformat() if hit.date else "",
                "document_id": str(hit.document_id),
                "topics": ";".join(paper.topics),
                "title": hit.title,
                "url": hit.url,
                "superseded": ";".join(dict.fromkeys(s for s in superseded if s)),
            }
        )
    rows += kept.values()  # papers not in these results stay as they are
    return rows


def _newer(version: str | None, than: str) -> bool:
    def order(v: str | None) -> tuple[int, ...]:
        return tuple(int(p) for p in (v or "1.0").split("."))

    return bool(than) and order(version) > order(than)
