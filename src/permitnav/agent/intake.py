"""Intake for follow-on questions (phase 5, step 3).

"And can my spouse work too?" cannot be searched: the facts are in an earlier turn. When a
question comes with earlier questions (the conversation's memory, kept by the client: the
server stores nothing), a cheap model call rewrites it into one self-contained question in
the same language, carrying over only facts the user stated, and records the user's
situation (for the editable "your situation" panel in phase 6). Search and answer then use
the rewritten question, exactly as plain RAG does.

Search cannot read "not": a question that rules out the EU Blue Card finds the Blue Card
rules, and they crowd out the rule that answers it (phase 6, from the first saved feedback
case). So the same call also writes a search question without what the person ruled out.
The answer model still gets the full question; only the search uses the other one, and
only when the question does rule something out (`rules_out`).

A single question that rules nothing out: no call, so it costs what it always did. Any
failure (a model error, an unreadable reply) falls back to the question as asked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from permitnav import tracing
from permitnav.answering.answerer import _json_object, rules_out
from permitnav.llm.port import LLM, Completion, CompletionRequest, LLMError
from permitnav.prompting import load_prompt

SITUATION_FIELDS = (
    "permit",
    "permit_granted",
    "application_registered",
    "employer_change",
    "family",
    "other",
)
MAX_HISTORY = 6  # earlier questions the rewrite sees (the last several turns, D14)
# Earlier questions the API accepts: a client may send a whole conversation; only the last
# MAX_HISTORY are used. A bound, so one request cannot carry an unbounded list.
MAX_HISTORY_ACCEPTED = 50
MAX_QUESTION_CHARS = 2000  # a question's limit (the API's too); a longer rewrite is not used
# What models write for "not said"; never shown back as if the user had said it.
PLACEHOLDERS = {
    *("null", "none", "unknown", "n/a", "na", "-"),
    *("not stated", "not mentioned", "not specified", "not given"),
    *("okänt", "ej angivet", "inte angivet", "saknas"),
}


@dataclass
class Intake:
    """What intake made of a question (for answers, evals and the situation panel)."""

    asked: str
    # What is answered (and searched, unless `search` is set): the rewrite, or `asked`.
    question: str
    rewritten: bool
    situation: dict[str, str] = field(default_factory=dict)  # only what the user stated
    # What to search with instead of `question`, when the question rules something out:
    # the same question without those things. None: search with `question`.
    search: str | None = None
    completion: Completion | None = None
    # Why memory was not used, when it was not: "model error", or the reply's finish reason
    # when it was unreadable (e.g. "length": cut off). None when the rewrite was read.
    fallback: str | None = None


class IntakeRewriter:
    def __init__(
        self,
        llm: LLM,
        model: str,
        max_tokens: int = 4000,  # the reply carries the question twice (and the reasoning)
        reasoning_effort: str | None = "low",
    ):
        self._llm = llm
        self.model = model
        # The answer model reasons before replying, and that counts against max_tokens.
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self._template = load_prompt("intake.md")

    def prepare(self, question: str, history: list[str] | tuple[str, ...]) -> Intake:
        earlier = [h for h in history if h.strip()][-MAX_HISTORY:]
        if not earlier and not rules_out(question):
            return Intake(asked=question, question=question, rewritten=False)
        with tracing.span(
            "intake", "chain", **{"permitnav.history_turns": len(earlier)}
        ) as current:
            # The conversation is data, not instructions: a JSON object in the user message,
            # each question on one line, so no text a user wrote can pose as the prompt.
            conversation = {
                "earlier_questions": [" ".join(h.split()) for h in earlier],
                "latest_question": " ".join(question.split()),
            }
            request = CompletionRequest(
                model=self.model,
                system=self._template,
                messages=[
                    {"role": "user", "content": json.dumps(conversation, ensure_ascii=False)}
                ],
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
            )
            try:
                completion = tracing.complete(self._llm, request, "rewrite")
            except LLMError:
                # The answer call that follows meets the same provider; this only says that
                # memory was skipped, so the trace shows why a follow-on was answered alone.
                result = Intake(asked=question, question=question, rewritten=False)
                result.fallback = "model error"
            else:
                # The user ruled something out, in this question or an earlier one (the
                # rewrite may or may not repeat it).
                ruled_out = any(rules_out(text) for text in (question, *earlier))
                result = parse_intake(completion.text, question, ruled_out)
                if not earlier:
                    # A single question is answered as asked; the call was only for the
                    # search question (no rewrite to show back, no situation to remember).
                    result = Intake(
                        asked=question,
                        question=question,
                        rewritten=False,
                        # an unreadable reply is a fallback: nothing from it is used
                        search=None
                        if _unreadable(completion.text)
                        else _search_question(
                            _json_object(completion.text) or {}, question, ruled_out
                        ),
                    )
                result.completion = completion
                if (
                    result.question == question
                    and not result.rewritten
                    and _unreadable(completion.text)
                ):
                    result.fallback = str(completion.extra.get("finish_reason") or "unreadable")
            tracing.set_attributes(
                current,
                **{
                    "permitnav.intake_fallback": result.fallback or "",
                    "permitnav.rewritten": result.rewritten,
                    "permitnav.question_chars": len(result.question),
                    "permitnav.search_question": result.search is not None,
                    # which facts the user gave, never what they were
                    "permitnav.situation_fields": sorted(result.situation),
                },
            )
            return result


def parse_intake(text: str, asked: str, ruled_out: bool = False) -> Intake:
    """The rewrite's reply; unreadable or empty → the question as asked. `ruled_out`: the
    user ruled something out in this conversation, so the search question is used."""
    reply = _json_object(text)
    rewrite = reply.get("question") if reply else None
    if not isinstance(rewrite, str) or not rewrite.strip():
        return Intake(asked=asked, question=asked, rewritten=False)
    raw = reply.get("situation") if reply else None
    situation = {
        k: v.strip()
        for k, v in (raw.items() if isinstance(raw, dict) else [])
        if k in SITUATION_FIELDS
        and isinstance(v, str)
        and v.strip()
        and v.strip().strip(".").lower() not in PLACEHOLDERS
    }
    rewrite = " ".join(rewrite.split())
    if len(rewrite) > MAX_QUESTION_CHARS:  # longer than any question the API accepts
        return Intake(asked=asked, question=asked, rewritten=False, situation=situation)
    # Whitespace is collapsed in the rewrite, so an echo of a question with a line break in
    # it must not count as a rewrite (it would be shown back as "understood as").
    changed = rewrite != " ".join(asked.split())
    question = rewrite if changed else asked
    return Intake(
        asked=asked,
        question=question,
        rewritten=changed,
        situation=situation,
        search=_search_question(reply or {}, question, ruled_out),
    )


def _search_question(reply: dict, question: str, ruled_out: bool) -> str | None:
    """The search question, only when something was ruled out and the reply gives a usable
    one that differs from the question: every other question is searched exactly as before."""
    text = reply.get("search_question")
    if not isinstance(text, str) or not (ruled_out or rules_out(question)):
        return None
    search = " ".join(text.split())
    if not search or len(search) > MAX_QUESTION_CHARS or search == " ".join(question.split()):
        return None
    # It may only take words out. A paraphrase (of a question that set nothing aside, or
    # with something put in place of what was removed) is not used: search as before.
    added = _words(search) - _words(question)
    if len(added) > MAX_NEW_WORDS:
        return None
    return search


MAX_NEW_WORDS = 2  # grammar around a removal ("a" for "an", a capital after a cut)
_WORD = re.compile(r"\w+")


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def _unreadable(text: str) -> bool:
    reply = _json_object(text)
    return not (reply and isinstance(reply.get("question"), str) and reply["question"].strip())
