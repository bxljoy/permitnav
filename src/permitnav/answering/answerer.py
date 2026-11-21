"""`Answerer.answer(question, as_of)` (docs/DESIGN.md, "Phase 1 internals"; D11, D12, D21).

Plain RAG for phase 1: retrieve, assemble sources, one model call, parse, check citations.
The prompt (prompts/answer.md) already allows one follow-up question, "not settled" and
declining out-of-scope questions, so phase 5's agent is compared against a fair baseline (D21).

With a floor set (`AnswerConfig.min_score` > 0), a question whose sources all score below it,
and that names no section, is declined without a model call: nothing in the corpus is about it.

Citations are checked against the sources the model was given: a marker that points nowhere
is removed and counted. Checking that each claim is supported by its source is phase 5's
verifier (D13).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

import psycopg

from permitnav.answering.context import Context, Source, build_context
from permitnav.llm.port import LLM, Completion, CompletionRequest
from permitnav.parsing.references import named_units
from permitnav.prompting import load_prompt
from permitnav.retrieval.retriever import Hit, Retriever

KINDS = ("answered", "follow_up", "not_settled", "out_of_scope")
MARKER = re.compile(r"\[(S\d+)\]")
# "[S1, S2]", "[S1–S3]", "[S1; S4]": groups the model sometimes writes instead of [S1][S2]
GROUP = re.compile(r"\[(S\d+(?:\s*[,;–-]\s*S\d+)+)\]")
MAX_TOKENS_CAP = 16000
# The relevance floor for bge-m3 cosine scores, calibrated on the dev set (phase 2, slice 2):
# in-scope questions' best source scored 0.589–0.809, unrelated ones 0.491–0.641. The ranges
# overlap, so the floor sits below every in-scope question with a margin and only catches
# clearly unrelated ones; the prompt's out-of-scope rule handles the rest.
BGE_M3_FLOOR = 0.55
NOTHING_RELEVANT = (
    "PermitNav found nothing about this in its sources. It covers Swedish work permits and "
    "their extensions, family members of work-permit holders, permanent residence and EU "
    "long-term resident status. For other questions, contact the authority responsible, or "
    "see Migrationsverket (www.migrationsverket.se).\n\n"
    "PermitNav hittade inget om detta i sina källor. Tjänsten handlar om arbetstillstånd och "
    "förlängningar, familjemedlemmar till personer med arbetstillstånd, permanent "
    "uppehållstillstånd och status som varaktigt bosatt i EU."
)


@dataclass(frozen=True)
class Citation:
    marker: str
    unit_id: str
    heading: str
    url: str
    authority: str
    dates: str
    quote: str = ""  # Swedish, copied from the source
    translation: str = ""  # unofficial (D12)


@dataclass
class Answer:
    kind: str  # answered | follow_up | not_settled | out_of_scope | failed
    text: str
    citations: list[Citation] = field(default_factory=list)
    dropped_markers: list[str] = field(default_factory=list)  # cited but not a given source
    sources: list[Source] = field(default_factory=list)  # everything the model was shown
    completion: Completion | None = None


@dataclass(frozen=True)
class AnswerConfig:
    model: str
    k: int = 8
    max_tokens: int = 4000
    # Decline without a model call when every source scores below this (0 = off). Scores are
    # the embedding model's cosine similarities, so the value belongs to that model.
    min_score: float = 0.0


class Answerer:
    def __init__(
        self, conn: psycopg.Connection, retriever: Retriever, llm: LLM, config: AnswerConfig
    ) -> None:
        self._conn = conn
        self._retriever = retriever
        self._llm = llm
        self.config = config
        self._template = load_prompt()

    def answer(self, question: str, as_of: date | None = None) -> Answer:
        as_of = as_of or date.today()
        hits = self._retriever.search(question, as_of=as_of, k=self.config.k)
        if not _relevant(question, hits, self.config.min_score):
            return Answer(kind="out_of_scope", text=NOTHING_RELEVANT)
        context = build_context(self._conn, hits, as_of)
        system = self._template.format(as_of=as_of.isoformat(), sources=context.render())
        max_tokens = self.config.max_tokens
        while True:
            completion = self._llm.complete(
                CompletionRequest(
                    model=self.config.model,
                    system=system,
                    messages=[{"role": "user", "content": question}],
                    max_tokens=max_tokens,
                )
            )
            cut_off = completion.extra.get("finish_reason") == "length"
            if not cut_off or max_tokens >= MAX_TOKENS_CAP:
                break
            max_tokens = min(
                MAX_TOKENS_CAP, max_tokens * 2
            )  # the reply was cut off: retry once larger
        answer = parse_answer(completion, context)
        if cut_off and answer.kind == "failed":
            answer.text = "The reply was too long and was cut off. Please ask a narrower question."
        return answer


def _relevant(question: str, hits: list[Hit], min_score: float) -> bool:
    """False only when a floor is set, the question names no section (whether or not the
    retrieval mode looked it up), and no source reaches the floor."""
    if min_score <= 0 or named_units(question):
        return True
    return any(h.score >= min_score for h in hits)


def parse_answer(completion: Completion, context: Context) -> Answer:
    data = _json_object(completion.text)
    raw_answer = data.get("answer") if data else None
    if data is None or not isinstance(raw_answer, str) or not raw_answer.strip():
        return Answer(
            kind="failed",
            text="The model's reply could not be read. Please try again.",
            sources=context.sources,
            completion=completion,
        )
    raw_kind = data.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind in KINDS else "answered"
    text = GROUP.sub(lambda m: "".join(f"[{x}]" for x in _expand(m[1])), raw_answer.strip())
    given = context.by_marker()

    dropped = sorted({m for m in MARKER.findall(text) if m not in given})
    for marker in dropped:  # remove citations that point to no source the model was given
        text = text.replace(f"[{marker}]", "")
    quotes: dict[str, dict] = {}  # the first exact quote per source
    raw_quotes = data.get("quotes")
    for quote in raw_quotes if isinstance(raw_quotes, list) else []:
        if not isinstance(quote, dict):
            continue
        source, swedish = quote.get("source"), quote.get("swedish")
        if not isinstance(source, str) or source not in given or source in quotes:
            continue
        if (
            isinstance(swedish, str)
            and swedish
            and _normalise(swedish) in _normalise(given[source].text)
        ):
            quotes[source] = quote

    cited = list(dict.fromkeys(m for m in MARKER.findall(text) if m in given))
    citations = []
    for marker in cited:
        source = given[marker]
        quote = quotes.get(marker, {})  # only exact quotes made it in
        swedish = str(quote.get("swedish", ""))
        translation = quote.get("translation", "")
        citations.append(
            Citation(
                marker=marker,
                unit_id=source.unit_id,
                heading=source.heading,
                url=source.url,
                authority=source.authority,
                dates=source.dates,
                quote=swedish,
                translation=translation if isinstance(translation, str) else "",
            )
        )
    return Answer(
        kind=kind,
        text=re.sub(r"[ \t]+([.,;:])", r"\1", text),
        citations=citations,
        dropped_markers=dropped,
        sources=context.sources,
        completion=completion,
    )


def _expand(group: str) -> list[str]:
    """'S1, S3' -> [S1, S3]; 'S1–S3' -> [S1, S2, S3]."""
    markers: list[str] = []
    for part in re.split(r"\s*[,;]\s*", group):
        bounds = re.split(r"\s*[–-]\s*", part)
        if len(bounds) == 2:
            low, high = (int(b.lstrip("S")) for b in bounds)
            markers += [f"S{n}" for n in range(low, high + 1)] if 0 < high - low < 20 else bounds
        else:
            markers.append(part)
    return markers


def _json_object(text: str) -> dict | None:
    """The first JSON object in the reply, tolerating code fences or text around it."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()
