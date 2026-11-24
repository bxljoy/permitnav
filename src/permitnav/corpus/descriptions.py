"""Plain-language descriptions of law sections, used only to find them (phase 5 experiment).

Swedish law often says whom a rule is for only through section numbers: utlänningsförordningen
4 kap. 4 a § is about the family of someone with a work permit, but its text says "a person
granted a permit under 5 kap. 10 § utlänningslagen in cases under 6 kap. 2 § första stycket".
Meaning-based search cannot follow the numbers, so the section ranked outside the top 100 for
every phrasing of that question; a model-written description ranked level with the top hit.

Each statute and transitional wording gets a description (Swedish and English) from the
answer model, with the headings of the sections it cites to resolve its references. It is
stored in `unit_description` (model, prompt hash) and indexed as two chunks of kind
'description' (Swedish and English, each with its own text-search configuration). The
retriever uses them only when `RetrievalConfig.descriptions` is on, and a section found
through one is always shown to the answer model with its own text, never the description.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import psycopg

from permitnav.llm.port import LLM, CompletionRequest, LLMError
from permitnav.parsing.references import named_units
from permitnav.prompting import load_prompt

DESCRIBED_TYPES = ("statute", "transitional")
STRATEGY = "structure"
FIRST_ORDINAL = 1000  # description chunks sit after any text chunk of the wording
SEPARATOR = "---"


@dataclass(frozen=True)
class Wording:
    version_id: int
    unit_id: str
    heading: str
    text: str
    cited: tuple[str, ...]  # headings of the sections it cites


def prompt_hash() -> str:
    return hashlib.sha256(load_prompt("describe.md").encode()).hexdigest()[:12]


def undescribed(conn: psycopg.Connection, limit: int | None = None) -> list[Wording]:
    """Statute and transitional wordings (not withdrawn) without a description yet."""
    rows = conn.execute(
        "SELECT v.id, u.id, u.heading, v.text FROM unit_version v JOIN unit u ON u.id = v.unit_id"
        " WHERE u.source_type = ANY(%s) AND NOT v.withdrawn"
        " AND NOT EXISTS (SELECT 1 FROM unit_description d WHERE d.version_id = v.id)"
        " ORDER BY v.id LIMIT %s",
        (list(DESCRIBED_TYPES), limit),
    ).fetchall()
    headings = dict(conn.execute("SELECT id, heading FROM unit WHERE source_type = 'statute'"))
    return [
        Wording(vid, uid, heading, text, _cited(text, headings)) for vid, uid, heading, text in rows
    ]


def _cited(text: str, headings: dict[str, str]) -> tuple[str, ...]:
    return tuple(headings[unit] for unit in sorted(named_units(text)) if unit in headings)


MAX_HALF_CHARS = 900  # 2–3 sentences; longer means notes or a second version crept in


def split(description: str) -> tuple[str, str] | None:
    """(Swedish, English), or None when the reply is not exactly two halves of a reasonable
    length separated by one "---" line (a reply with notes or a revision is not indexed)."""
    lines = description.strip().splitlines()
    breaks = [i for i, line in enumerate(lines) if line.strip() == SEPARATOR]
    if len(breaks) != 1:
        return None
    swedish = "\n".join(lines[: breaks[0]]).strip()
    english = "\n".join(lines[breaks[0] + 1 :]).strip()
    if not swedish or not english or max(len(swedish), len(english)) > MAX_HALF_CHARS:
        return None
    return swedish, english


def describe(
    conn: psycopg.Connection,
    llm: LLM,
    model: str,
    wordings: list[Wording],
    workers: int = 8,
    on_done: Callable[[int, int], None] | None = None,
) -> tuple[int, list[str], float]:
    """Describe the wordings (model calls in parallel, writes here): returns how many were
    stored, the unit ids whose reply was unusable, and the cost."""
    system = load_prompt("describe.md")
    version = prompt_hash()

    def ask(w: Wording):
        refs = "; ".join(w.cited) or "(none)"
        request = CompletionRequest(
            model=model,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": f"Provision: {w.heading}\n\n{w.text}\n\n"
                    f"Headings of the sections it cites: {refs}",
                }
            ],  # fmt: skip
            max_tokens=1500,
            reasoning_effort="low",
        )
        try:
            return w, llm.complete(request)
        except LLMError:
            return w, None

    stored, unusable, cost = 0, [], 0.0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, (w, completion) in enumerate(pool.map(ask, wordings), start=1):
            halves = split(completion.text) if completion else None
            if completion is not None:
                cost += completion.cost_usd or 0.0
            if halves is None:
                unusable.append(w.unit_id)
            else:
                store(conn, w, halves, completion.model or model, version)  # type: ignore[union-attr]
                stored += 1
            if on_done:
                on_done(done, len(wordings))
    return stored, unusable, cost


def store(
    conn: psycopg.Connection, w: Wording, halves: tuple[str, str], model: str, version: str
) -> None:
    swedish, english = halves
    with conn.transaction():
        conn.execute(
            "INSERT INTO unit_description (version_id, text, model, prompt_hash)"
            " VALUES (%s, %s, %s, %s)",
            (w.version_id, f"{swedish}\n{SEPARATOR}\n{english}", model, version),
        )
        for ordinal, (text, config) in enumerate(((swedish, "swedish"), (english, "english"))):
            conn.execute(
                "INSERT INTO chunk (version_id, strategy, ordinal, text, ts_config, kind)"
                " VALUES (%s, %s, %s, %s, %s::regconfig, 'description')",
                (w.version_id, STRATEGY, FIRST_ORDINAL + ordinal, f"{w.heading}\n\n{text}", config),
            )
