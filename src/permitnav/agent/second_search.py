"""A second search (phase 5, step 2).

After the first search (fixed code, as in plain RAG), a model reads short excerpts of the
sources and decides whether they are enough to answer. If not, it names at most two steps,
which deterministic tools carry out:

- `get_section(statute, chapter, section)`: one law or ordinance section, in the wording
  that applies on `as_of`;
- `follow_citations(from)`: the sections a found source cites, the closest to the question;
- `search_rules(query, sources)`: a new search in one kind of source, with the model's words.

New sources are added after the first search's. Any failure (a model error, an unreadable
reply, a step that finds nothing) leaves the first search's sources as they were: the
second search can add, never take away.

The failure analysis behind it is in docs/DESIGN.md ("Phase 5 plan", step 1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from permitnav import tracing
from permitnav.answering.answerer import _json_object
from permitnav.llm.port import LLM, Completion, CompletionRequest, LLMError
from permitnav.prompting import load_prompt
from permitnav.retrieval.retriever import Hit, Retriever

STATUTES = {"utlänningslagen": "sfs:2005:716", "utlänningsförordningen": "sfs:2006:97"}
SOURCE_KINDS: dict[str, tuple[str, ...]] = {
    "law": ("statute", "transitional"),
    "guidance": ("web_page",),
    "papers": ("position_paper",),
    "rulings": ("ruling",),
}
TOOLS = ("get_section", "follow_citations", "search_rules")
MAX_STEPS = 2
# Sources one step may add: a cited section or a search brings a few, not a page of results
# (following every citation would add a median of 9 sections, step 1).
PER_STEP = {"get_section": 1, "follow_citations": 2, "search_rules": 3}
EXCERPT_CHARS = 400
# The chapters the service covers, in the check's table of contents: residence permits,
# work permits, EU Blue Card, ICT, seasonal work, revocation; the ordinance's matching ones.
CONTENTS: dict[str, tuple[str, ...]] = {
    "sfs:2005:716": ("5", "6", "6a", "6b", "6c", "7"),
    "sfs:2006:97": ("4", "5", "6"),
}
NAMES = {prefix: name for name, prefix in STATUTES.items()}
MARKER = re.compile(r"^S(\d+)$")
SECTION_NUMBER = re.compile(r"^\d+\s?[a-z]?$")


@dataclass(frozen=True)
class Step:
    tool: str
    args: dict[str, str]


@dataclass
class SecondSearch:
    """What the second search decided and added (for evals and traces)."""

    enough: bool | None  # None: the check failed or was unreadable; nothing was added
    missing: str = ""
    steps: list[Step] = field(default_factory=list)
    added: list[str] = field(default_factory=list)  # unit ids, in the order added
    completion: Completion | None = None


class SecondSearcher:
    def __init__(
        self,
        retriever: Retriever,
        llm: LLM,
        model: str,
        max_tokens: int = 2000,
        reasoning_effort: str | None = "low",
    ):
        self._retriever = retriever
        self._llm = llm
        self.model = model
        # The answer model reasons before replying, and that counts against max_tokens: at
        # 800 the reply was cut off empty. Low effort keeps the check short.
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self._template = load_prompt("evidence_check.md")
        self._contents: dict[date, str] = {}  # as_of -> table of contents

    def expand(self, question: str, as_of: date, hits: list[Hit]) -> tuple[list[Hit], SecondSearch]:
        """The first search's hits, plus what the steps found."""
        with tracing.span("second_search", "chain") as current:
            result = self._check(question, as_of, hits)
            new: list[Hit] = []
            if result.enough is False:
                seen = {h.unit_id for h in hits}
                for step in result.steps:
                    for hit in self._run(step, question, as_of, hits, seen):
                        seen.add(hit.unit_id)
                        new.append(hit)
                        result.added.append(hit.unit_id)
            tracing.set_attributes(
                current,
                **{
                    "permitnav.enough": "unknown" if result.enough is None else result.enough,
                    "permitnav.steps": [s.tool for s in result.steps],
                    "permitnav.added_units": result.added,  # ids, never text
                },
            )
            return hits + new, result

    def _check(self, question: str, as_of: date, hits: list[Hit]) -> SecondSearch:
        request = CompletionRequest(
            model=self.model,
            system=self._template.format(
                as_of=as_of.isoformat(), contents=self._table(as_of), sources=render(hits)
            ),
            messages=[{"role": "user", "content": question}],
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )
        try:
            completion = tracing.complete(self._llm, request, "evidence_check")
        except LLMError:
            return SecondSearch(enough=None)
        plan = parse_plan(completion.text, len(hits))
        plan.completion = completion
        return plan

    def _table(self, as_of: date) -> str:
        if as_of not in self._contents:
            rows = [
                row
                for statute, chapters in CONTENTS.items()
                for chapter in chapters
                for row in self._retriever.section_headings(f"{statute}/{chapter}kap/", as_of)
            ]
            self._contents[as_of] = table_of_contents(rows)
        return self._contents[as_of]

    def _run(
        self, step: Step, question: str, as_of: date, hits: list[Hit], seen: set[str]
    ) -> list[Hit]:
        limit = PER_STEP[step.tool]
        if step.tool == "get_section":
            unit = section_id(step.args)
            if unit is None or unit in seen:
                return []
            return self._retriever.rank_units(question, as_of, [unit], 1)
        if step.tool == "follow_citations":
            source = hits[int(step.args["from"][1:]) - 1]
            cited = sorted(self._retriever.cited_sections(source) - seen)
            return self._retriever.rank_units(question, as_of, cited, limit)
        found = self._retriever.search_types(
            step.args["query"], as_of, limit + len(seen), SOURCE_KINDS[step.args["sources"]]
        )
        return [h for h in found if h.unit_id not in seen][:limit]


def render(hits: list[Hit]) -> str:
    """Short excerpts for the check: the marker, heading, id and the start of the text."""
    parts = []
    for i, hit in enumerate(hits, start=1):
        excerpt = " ".join(hit.text.split())[:EXCERPT_CHARS]
        parts.append(f"[S{i}] {hit.heading} ({hit.unit_id})\n{excerpt}")
    return "\n\n".join(parts) or "(none)"


def parse_plan(text: str, n_sources: int) -> SecondSearch:
    """The check's reply. Unreadable → enough unknown (nothing added); invalid steps are
    dropped, so a half-right plan still runs its valid steps."""
    reply = _json_object(text)
    if reply is None or not isinstance(reply.get("enough"), bool):
        return SecondSearch(enough=None)
    steps = []
    for raw in reply.get("steps") or []:
        step = _step(raw, n_sources)
        if step is not None and step not in steps:
            steps.append(step)
    missing = reply.get("missing")
    return SecondSearch(
        enough=reply["enough"],
        missing=missing if isinstance(missing, str) else "",
        steps=steps[:MAX_STEPS] if not reply["enough"] else [],
    )


def _step(raw: object, n_sources: int) -> Step | None:
    if not isinstance(raw, dict) or raw.get("tool") not in TOOLS:
        return None
    args = {k: v.strip() for k, v in raw.items() if k != "tool" and isinstance(v, str)}
    tool = raw["tool"]
    if tool == "get_section":
        return Step(tool, args) if section_id(args) else None
    if tool == "follow_citations":
        marker = MARKER.match(args.get("from", ""))
        ok = marker is not None and 1 <= int(marker.group(1)) <= n_sources
        return Step(tool, {"from": args["from"]}) if ok else None
    if args.get("query") and args.get("sources") in SOURCE_KINDS:
        return Step(tool, {"query": args["query"], "sources": args["sources"]})
    return None


def section_id(args: dict[str, str]) -> str | None:
    """`get_section`'s arguments as a unit id: ("utlänningsförordningen", "4", "4 a") →
    sfs:2006:97/4kap/4a§. None when the statute is unknown or a number malformed."""
    statute = STATUTES.get(args.get("statute", "").lower())
    chapter = args.get("chapter", "").lower().removesuffix("kap.").removesuffix("kap").strip()
    section = args.get("section", "").lower().rstrip("§ ").strip()
    if statute is None or not SECTION_NUMBER.match(section):
        return None
    section = section.replace(" ", "")
    if not chapter:
        return f"{statute}/{section}§"
    if not SECTION_NUMBER.match(chapter):
        return None
    return f"{statute}/{chapter.replace(' ', '')}kap/{section}§"


UNIT = re.compile(r"^(sfs:\d{4}:\d+)/(\d+[a-z]?)kap/(\d+)([a-z]?)§$")


def table_of_contents(rows: list[tuple[str, str]]) -> str:
    """Chapters and their subheadings with the sections under each, from the sections'
    headings ("5 kap. Uppehållstillstånd, Särskilda krav för permanent uppehållstillstånd,
    7 §"). A section without a subheading of its own falls under the one above it, as in the
    printed law. Lets the check name real sections instead of guessing numbers."""
    sections = []
    for unit, heading in rows:
        m = UNIT.match(unit)
        if m:
            sfs, chapter, number, letter = m.groups()
            sections.append((sfs, chapter, int(number), letter, heading))
    sections.sort(key=lambda s: (s[0], int(re.match(r"\d+", s[1]).group()), s[1], s[2], s[3]))  # type: ignore[union-attr]
    lines: list[str] = []
    current: tuple[str, str] | None = None
    groups: list[list] = []  # [subheading, [section labels]]

    def flush() -> None:
        for sub, labels in groups:
            span = labels[0] if len(labels) == 1 else f"{labels[0]}–{labels[-1]}"
            lines.append(f"  {span} §: {sub}" if sub else f"  {span} §")

    for sfs, chapter, number, letter, heading in sections:
        if (sfs, chapter) != current:
            flush()
            groups = []
            current = (sfs, chapter)
            title = heading.split(",")[0].strip()
            lines.append(f"{NAMES.get(sfs, sfs)} {title}")
        parts = [p.strip() for p in heading.split(",")]
        sub = ", ".join(parts[1:-1])
        label = f"{number} {letter}".strip()
        if sub and (not groups or groups[-1][0] != sub):
            groups.append([sub, [label]])
        elif groups:
            groups[-1][1].append(label)
        else:
            groups.append(["", [label]])
    flush()
    return "\n".join(lines)
