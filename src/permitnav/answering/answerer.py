"""`Answerer.answer(question, as_of)` (docs/DESIGN.md, "Phase 1 internals"; D11, D12, D21).

Plain RAG: retrieve, assemble sources, call the model, parse, check citations. A reply cut off
at the token limit is retried with twice the tokens, up to MAX_TOKENS_CAP; an unreadable reply
is asked for once more. So one answer takes one call, for about one question in eight two
(measured 2026-11-27: DESIGN.md), at most four.
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
import logging
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import psycopg

from permitnav import tracing
from permitnav.answering.context import Context, Source, build_context
from permitnav.llm.port import LLM, Completion, CompletionRequest, LLMError
from permitnav.negation import rules_out
from permitnav.parsing.references import named_units
from permitnav.prompting import load_prompt
from permitnav.retrieval.retriever import Hit, Retriever
from permitnav.retrieval.translate import clear_language, language_evidence, language_of

if TYPE_CHECKING:
    from permitnav.agent.intake import Intake, IntakeRewriter
    from permitnav.agent.second_search import SecondSearch, SecondSearcher

KINDS = ("answered", "follow_up", "not_settled", "out_of_scope")
# The reply's shape as a JSON Schema (the same object the prompt describes), for models that
# can be held to one (AnswerConfig.structured).
ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "answer": {"type": "string"},
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "swedish": {"type": "string"},
                    "translation": {"type": "string"},
                },
                "required": ["source", "swedish", "translation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["kind", "answer", "quotes"],
    "additionalProperties": False,
}
log = logging.getLogger("permitnav.answer")
MARKER = re.compile(r"\[(S\d+)\]")
# "[S1, S2]", "[S1–S3]", "[S1; S4]": groups the model sometimes writes instead of [S1][S2]
GROUP = re.compile(r"\[(S\d+(?:\s*[,;–-]\s*S\d+)+)\]")
MAX_TOKENS_CAP = 16000
# The stages an answer goes through, in order, as `progress` reports them: "understanding" only
# with earlier questions (intake), "checking" only with the second search.
STAGES = ("understanding", "searching", "checking", "writing")

ANSWER_LANGUAGE = {
    "sv": "Swedish, the language of the question",
    "en": "English, the language of the question",
    None: "the language of the question",
}
# Sent once when a reply cannot be read (no JSON, or broken JSON such as unescaped quotes);
# about 1-3% of replies in phase 2; 10% on 2026-11-27, nearly all the answer as plain text
# with no JSON (DESIGN.md, "Second calls").
REASK = (
    "That reply could not be used: it was not the JSON object described in the instructions "
    "(no JSON, invalid JSON such as an unescaped quote inside a string, or no non-empty "
    '"answer" field). Reply again with only that JSON object.'
)
# Sent once when the reply was readable but its answer was not an answer (unusable_answer).
REASK_ANSWER = (
    'That reply could not be used: its "answer" field did not hold the answer itself, only a '
    'pointer to text that is not there (such as "see above") or only source markers. Reply '
    'again with the same JSON object, with the whole answer written out in the "answer" field.'
)
# Sent once when a readable answer is in the other language than the question (about one
# Swedish question in ninety was answered in English, never the reverse; DESIGN.md).
REASK_LANGUAGE = (
    "That answer is in the wrong language: the question was asked in {language}. Reply again "
    'with the same JSON object, with the "answer" written in {language}. Keep the quotes '
    "from the sources in Swedish."
)
LANGUAGE_NAMES = {"sv": "Swedish", "en": "English"}
# An "answer" that is only a pointer to text that is not there. Seen 2026-11-26: the whole
# answer was "(se ovan)"; the model had written the answer in its hidden reasoning. Matched
# against the whole answer (see unusable_answer), never against part of one.
PLACEHOLDERS = frozenset(
    {
        "above",
        "see above",
        "as above",
        "same as above",
        "as stated above",
        "as explained above",
        "see the answer above",
        "see my answer above",
        "see answer above",
        "see the above",
        "see previous answer",
        "see the previous answer",
        "see earlier",
        "see below",
        "see reasoning",
        "see my reasoning",
        "see reasoning above",
        "see the reasoning above",
        "see analysis",
        "see analysis above",
        "see the analysis above",
        "ibid",
        "ovan",
        "se ovan",
        "som ovan",
        "enligt ovan",
        "samma som ovan",
        "se svaret ovan",
        "se ovanstående",
        "se föregående svar",
        "se tidigare svar",
        "se nedan",
        "se resonemanget",
        "se resonemanget ovan",
        "se analysen ovan",
    }
)
# Words that may stand in front of a pointer without making it an answer ("Svar: se ovan").
FILLERS = frozenset({"answer", "please", "svar", "svaret", "vänligen"})
BARE_MARKER = re.compile(r"s\d+")  # a source marker written without its brackets: "(S1)"
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
    # The exact wording cited: an older wording shares its unit_id with the current one.
    version_id: int | None = None


@dataclass
class Answer:
    kind: str  # answered | follow_up | not_settled | out_of_scope | failed
    text: str
    citations: list[Citation] = field(default_factory=list)
    dropped_markers: list[str] = field(default_factory=list)  # cited but not a given source
    sources: list[Source] = field(default_factory=list)  # everything the model was shown
    completion: Completion | None = None
    # The other calls made for the same answer: a reply cut off or unreadable and then asked
    # again, or a second reply (asked for in the question's language) that was not used.
    earlier: list[Completion] = field(default_factory=list)
    # The second search's decision and additions (phase 5), when it ran.
    search: SecondSearch | None = None
    # What intake made of the question, when it ran: a follow-on made self-contained
    # (phase 5), and the search question when the person ruled something out (phase 6).
    intake: Intake | None = None
    # Why a "failed" answer failed, for logs and traces (never shown): the reply was not the
    # JSON asked for, or its answer field held no answer.
    problem: str = ""
    # The answer is in the other language than the question, even after asking again: shown
    # all the same (a right answer in the wrong language still helps), and logged.
    other_language: bool = False
    # The reply was not valid JSON and its fields were read by name (_recovered_object): an
    # ordinary answer, counted in logs and eval output so the recovery can be watched.
    recovered: bool = False


# Only the efforts that were measured (DESIGN.md, 2026-11-25). "medium" and "high" would also
# need a larger max_tokens: the thinking counts against it.
REASONING_EFFORTS = ("none", "minimal", "low")


def answer_reasoning_effort() -> str | None:
    """ANSWER_REASONING_EFFORT from the environment; unset or empty: the model's default."""
    value = os.environ.get("ANSWER_REASONING_EFFORT", "").strip().lower()
    if not value:
        return None
    if value not in REASONING_EFFORTS:
        raise ValueError(f"ANSWER_REASONING_EFFORT must be one of {REASONING_EFFORTS}: {value!r}")
    return value


def answer_structured() -> bool:
    """ANSWER_STRUCTURED from the environment: "1" or "true" holds the reply to ANSWER_SCHEMA;
    unset, empty, "0" or "false": free text. Anything else is an error, so that a mistyped
    value is not taken for "off" in a measurement."""
    value = os.environ.get("ANSWER_STRUCTURED", "").strip().lower()
    if value in ("", "0", "false"):
        return False
    if value in ("1", "true"):
        return True
    raise ValueError(f"ANSWER_STRUCTURED must be 1, true, 0 or false: {value!r}")


@dataclass(frozen=True)
class AnswerConfig:
    model: str
    k: int = 8
    max_tokens: int = 4000
    # Decline without a model call when every source scores below this (0 = off). Scores are
    # the embedding model's cosine similarities, so the value belongs to that model.
    min_score: float = 0.0
    # How much hidden thinking the answer model does before it writes (REASONING_EFFORTS);
    # None leaves the model's default. From ANSWER_REASONING_EFFORT unless given.
    reasoning_effort: str | None = field(default_factory=lambda: answer_reasoning_effort())
    # Hold the reply to ANSWER_SCHEMA (structured output). Off: measured on 2026-11-27, it
    # ends the second calls (12% of answers) but gave "not settled" more often where an answer
    # is expected (DESIGN.md). From ANSWER_STRUCTURED unless given, to re-measure with one
    # setting when the model changes. When on there is no fallback to free text: a model
    # whose endpoints cannot hold a reply to a schema fails every question.
    structured: bool = field(default_factory=lambda: answer_structured())


class Answerer:
    def __init__(
        self,
        conn: psycopg.Connection,
        retriever: Retriever,
        llm: LLM,
        config: AnswerConfig,
        second_search: SecondSearcher | None = None,
        intake: IntakeRewriter | None = None,
    ) -> None:
        self._conn = conn
        self._retriever = retriever
        self._llm = llm
        self.config = config
        self._second_search = second_search  # None: plain RAG, one search
        # None: plain RAG (earlier questions are ignored, the question is searched as asked)
        self._intake = intake
        self._template = load_prompt()

    def answer(
        self,
        question: str,
        as_of: date | None = None,
        history: tuple[str, ...] | list[str] = (),
        progress: Callable[[str], None] | None = None,
    ) -> Answer:
        """`history`: the user's earlier questions in the conversation, oldest first, sent by
        the client (memory lives there). Used only with intake; plain RAG asks `question`
        alone. With intake, a question that rules something out ("not an EU Blue Card") is
        searched without it, with or without history. `progress` hears each stage as it
        starts (STAGES), for a client to show."""
        as_of = as_of or date.today()
        heard: list[str] = []

        def report(stage: str) -> None:  # each stage once, in the order first reached
            if stage not in heard:
                heard.append(stage)
                if progress:
                    progress(stage)

        with tracing.span(
            "answer",
            "chain",
            **{
                "permitnav.as_of": as_of.isoformat(),
                "permitnav.question_chars": len(question),
                "permitnav.question_language": language_of(question) or "unknown",
                "permitnav.k": self.config.k,
            },
        ) as current:
            tracing.text(current, input=question)
            asked = question
            prepared = None
            if self._intake is not None and (history or rules_out(question)):
                # A stage before the call, so a client sees a step (and one that has gone
                # is noticed before the call is paid for). Without history the call only
                # prepares the search.
                report("understanding" if history else "searching")
                prepared = self._intake.prepare(question, history)
                question = prepared.question
            try:
                # The answer's language comes from the question as asked: a rewrite can carry
                # the earlier turns' language into a follow-on asked in another one.
                search_with = (prepared.search if prepared else None) or question
                if named_units(question):
                    search_with = question  # a named section is looked up from the full text
                answer = self._answer(question, as_of, language_of(asked), report, search_with)
            except LLMError as error:
                error.intake = prepared  # type: ignore[attr-defined]  # paid for: evals count it
                raise
            answer.intake = prepared
            calls = [
                prepared.completion if prepared else None,
                answer.search.completion if answer.search else None,
                *answer.earlier,
                answer.completion,
            ]
            tracing.set_attributes(
                current,
                **{
                    "permitnav.kind": answer.kind,
                    "permitnav.citations": len(answer.citations),
                    "permitnav.sources": len(answer.sources),
                    "permitnav.model_calls": sum(c is not None for c in calls),
                    "permitnav.cited_units": [c.unit_id for c in answer.citations],
                },
            )
            tracing.text(current, output=answer.text)
            return answer

    def _answer(
        self,
        question: str,
        as_of: date,
        language: str | None,
        report: Callable[[str], None],
        search_with: str | None = None,
    ) -> Answer:
        """`search_with`: the question without what the person ruled out, which search
        would otherwise find (intake). The model is still given `question` in full."""
        report("searching")
        search_with = search_with or question
        hits = self._retriever.search(search_with, as_of=as_of, k=self.config.k)
        if search_with != question and not _relevant(search_with, hits, self.config.min_score):
            # The shortened question found nothing: it may have lost what made the question
            # searchable. Never decline on it; search as before.
            search_with = question
            hits = self._retriever.search(question, as_of=as_of, k=self.config.k)
        if not _relevant(search_with, hits, self.config.min_score):
            return Answer(kind="out_of_scope", text=NOTHING_RELEVANT)
        search = None
        if self._second_search is not None:
            report("checking")
            hits, search = self._second_search.expand(question, as_of, hits, search_with)
        context = build_context(self._conn, hits, as_of)
        report("writing")
        # Told, not left to the model: with an English prompt and mixed sources it answered
        # more than half of the Swedish dev questions in English (phase 2). Only when the
        # question is clearly Swedish or English; otherwise (a tie, another language) the
        # model follows the question.
        # The language to hold the answer to: only when the question is in one language alone.
        only_language = language if language == single_language(question) else None
        language = ANSWER_LANGUAGE.get(language, ANSWER_LANGUAGE[None])
        system = self._template.format(
            as_of=as_of.isoformat(), sources=context.render(), language=language
        )
        max_tokens = self.config.max_tokens
        earlier: list[Completion] = []
        messages = [{"role": "user", "content": question}]

        def ask(messages: list[dict]) -> Completion:
            return tracing.complete(
                self._llm,
                CompletionRequest(
                    model=self.config.model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                    reasoning_effort=self.config.reasoning_effort,
                    response_schema=ANSWER_SCHEMA if self.config.structured else None,
                ),
                "generate",
            )

        while True:
            completion = ask(messages)
            cut_off = completion.extra.get("finish_reason") == "length"
            if not cut_off or max_tokens >= MAX_TOKENS_CAP:
                break
            earlier.append(completion)
            max_tokens = min(MAX_TOKENS_CAP, max_tokens * 2)  # cut off: double, up to the cap
        answer = parse_answer(completion, context)
        if answer.kind == "failed" and not cut_off:  # unreadable: ask once more
            # An empty reply cannot be shown back (an empty assistant turn is rejected), so
            # the same request is simply repeated.
            reask = messages
            if completion.text.strip():
                reask = [
                    *messages,
                    {"role": "assistant", "content": completion.text},
                    {
                        "role": "user",
                        "content": REASK_ANSWER
                        if answer.problem.startswith("no answer")
                        else REASK,
                    },
                ]
            try:
                second = ask(reask)
            except LLMError:
                second = None  # keep the first answer and what it cost
            if second is not None:
                earlier.append(completion)
                answer = parse_answer(second, context)
                cut_off = second.extra.get("finish_reason") == "length"
        elif answer.kind != "failed" and wrong_language(answer.text, only_language):
            # A readable answer in the other language: ask once more. Unlike an unusable
            # answer it is still worth showing, so it stands unless the second reply is the
            # same answer in the right language.
            try:
                second = ask(
                    [
                        *messages,
                        {"role": "assistant", "content": completion.text},
                        {
                            "role": "user",
                            "content": REASK_LANGUAGE.format(
                                language=LANGUAGE_NAMES[only_language or ""]
                            ),
                        },
                    ]
                )
            except LLMError as error:
                second = None  # the first answer stands; the provider's text is not logged
                log.warning(
                    "asking again for the answer's language failed: %s", type(error).__name__
                )
            if second is not None:
                again = parse_answer(second, context)
                better = (
                    again.kind == answer.kind  # not a different verdict
                    and clear_language(again.text) == only_language  # not merely undetectable
                    and (bool(again.citations) or not answer.citations)  # not without its sources
                )
                if better:
                    earlier.append(completion)
                    answer = again
                else:  # the first answer stands; the second call is still counted
                    earlier.append(second)
        if answer.kind != "failed":  # also after a re-ask for an unreadable reply
            answer.other_language = wrong_language(answer.text, only_language)
        if cut_off and answer.kind == "failed":
            answer.text = "The reply was too long and was cut off. Please ask a narrower question."
        answer.earlier = earlier
        answer.search = search
        return answer


def _relevant(question: str, hits: list[Hit], min_score: float) -> bool:
    """False only when a floor is set, the question names no section (whether or not the
    retrieval mode looked it up), and no source reaches the floor."""
    if min_score <= 0 or named_units(question):
        return True
    return any(h.score >= min_score for h in hits)


# Words that are English evidence for the detector and ordinary Swedish too ("lämna in",
# "6 kap. 2 a §"): not counted when asking whether a Swedish question has English in it.
SHARED_WORDS = re.compile(r"\b(?:in|a)\b", re.IGNORECASE)


def single_language(question: str) -> str | None:
    """The language a question is written in, only when nothing in it speaks for the other:
    a person who writes in English and pastes a Swedish sentence has not asked for the
    majority language of their text, and holding the answer to it would be wrong."""
    swedish, english = language_evidence(question)
    if swedish >= 2 and language_evidence(SHARED_WORDS.sub(" ", question))[1] == 0:
        return "sv"
    if english >= 2 and swedish == 0:
        return "en"
    return None


def wrong_language(text: str, asked_in: str | None) -> bool:
    """Whether an answer is overwhelmingly in the other language than the question was asked
    in. False whenever that cannot be said for sure (a short answer such as "Ja [S1].", an
    answer that mixes the two, a question whose language is not single): only a clear
    mismatch is worth another call."""
    if asked_in not in LANGUAGE_NAMES:
        return False
    written_in = clear_language(text)
    return written_in is not None and written_in != asked_in


def unusable_answer(text: str) -> bool:
    """Whether an answer text is not an answer at all: empty, only source markers or
    punctuation, or only a pointer to text that is not there ("(se ovan)"). Not a judgement
    of length: "Ja [S1]." and "No [S2]." are answers. The pointer phrases are compared with
    the whole text, ignoring case, source markers however they are written, brackets,
    punctuation and spacing; a pointer inside a real sentence is left alone."""
    text = unicodedata.normalize("NFC", text)  # "å" as one character, not "a" + a ring
    without_markers = MARKER.sub(" ", GROUP.sub(" ", text))
    words = "".join(c if c.isalnum() else " " for c in without_markers).casefold().split()
    loose_marker = any(BARE_MARKER.fullmatch(w) for w in words) or without_markers != text
    # Numbers and "and" count as part of the source markers only when there are markers
    # ("[S1 och S2]", "[S1, 2]"): "4 år" and "90" keep their number.
    said = [
        w
        for w in words
        if not BARE_MARKER.fullmatch(w)
        and not (loose_marker and (w.isdigit() or w in {"and", "och"}))
    ]
    if not said:
        return True
    pointer = [w for w in said if not w.isdigit()]  # "see above [1]"
    while pointer and pointer[0] in FILLERS:
        pointer = pointer[1:]
    return " ".join(pointer) in PLACEHOLDERS


def parse_answer(completion: Completion, context: Context) -> Answer:
    data = _json_object(completion.text)
    recovered = None
    # No object, or one without the answer (after a slip in the JSON the first object that
    # parses is one of the quotes). A reply cut off is never pieced together.
    if (data is None or "answer" not in data) and (
        completion.extra.get("finish_reason") != "length"
    ):
        recovered = _recovered_object(completion.text)
        data = recovered or data
    raw_answer = data.get("answer") if data else None
    problem = ""
    if data is None or not isinstance(raw_answer, str) or not raw_answer.strip():
        problem = "not the JSON object asked for"
    elif unusable_answer(raw_answer):
        problem = "no answer in the answer field"  # only a pointer or source markers
    if problem:
        # Never shown as an answer: asked for once more, then this message (see _answer).
        return Answer(
            kind="failed",
            text="The model's reply could not be read. Please try again.",
            sources=context.sources,
            completion=completion,
            problem=problem,
        )
    assert data is not None and isinstance(raw_answer, str)  # checked above
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
                version_id=source.version_id,
            )
        )
    return Answer(
        kind=kind,
        text=re.sub(r"[ \t]+([.,;:])", r"\1", text),
        citations=citations,
        dropped_markers=dropped,
        sources=context.sources,
        completion=completion,
        recovered=recovered is not None,
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
                        # strict=False: the model sometimes writes real line breaks inside
                        # strings, which strict JSON rejects.
                        value = json.loads(text[start : i + 1], strict=False)
                    except json.JSONDecodeError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


_KIND_FIELD = re.compile(r'"kind"\s*:\s*"([a-z_]+)"')
_ANSWER_FIELD = re.compile(r'"answer"\s*:\s*"')
# The end of the answer string and the start of the quotes, whatever stands between them: the
# comma may be missing, or a stray bracket may stand in its place.
_QUOTES_FIELD = re.compile(r'"[\s\]},]*"quotes"\s*:\s*')
_OBJECT_END = re.compile(r"\s*}\s*(?:```)?\s*")
# The start of another field inside what would be the answer: `", "name":`.
_FIELD_START = re.compile(r'"\s*[,\]}]*\s*"[A-Za-z_]+"\s*:')


def _recovered_object(text: str) -> dict | None:
    """The reply's fields when it is the object asked for with a slip in its JSON, else None.

    About 1 reply in 250 was unreadable twice (2026-11-27), each time for a small slip right
    after the long "answer" string: no comma before "quotes", or a stray "]" there. A
    quotation mark inside the answer that was not escaped is handled the same way (not seen
    in the saved replies; the re-ask names it). The fields are read by their names, and only
    when the reply is exactly one such object:

    - one "answer" field, at most one "quotes" field after it, and the object's end after that;
    - "kind" given once outside the answer (or the same each time), one of the known kinds;
    - the answer is the text between `"answer": "` and the quotes field (or the object's
      end), as written, and holds no start of another field;
    - quotes, when given, must be a valid list by themselves.

    Nothing is guessed: any other reply stays unreadable and is asked for again."""
    starts = list(_ANSWER_FIELD.finditer(text))
    if len(starts) != 1:
        return None
    start = starts[0]
    ends = list(_QUOTES_FIELD.finditer(text, start.end() - 1))
    quotes: object = []
    if len(ends) > 1:
        return None
    if ends:
        answer_end, after = ends[0].start(), ends[0].end()
        try:
            quotes, after = json.JSONDecoder(strict=False).raw_decode(text, after)
        except json.JSONDecodeError:
            return None
        if not isinstance(quotes, list) or not _OBJECT_END.fullmatch(text, after):
            return None
    else:
        answer_end = text.rfind('"', start.end())
        if answer_end == -1 or not _OBJECT_END.fullmatch(text, answer_end + 1):
            return None
        after = answer_end
    body = text[start.end() : answer_end]
    if _FIELD_START.search(body):
        return None
    kinds = {m[1] for m in _KIND_FIELD.finditer(text[: start.start()] + text[after:])}
    if len(kinds) != 1 or not kinds <= set(KINDS):
        return None
    try:
        answer = json.loads(f'"{_escaped_quotes(body)}"', strict=False)
    except json.JSONDecodeError:
        return None
    return {"kind": kinds.pop(), "answer": answer, "quotes": quotes}


def _escaped_quotes(body: str) -> str:
    """The inside of a JSON string with every quotation mark escaped (those already escaped
    are kept as they are)."""
    out, escaped = [], False
    for ch in body:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()
