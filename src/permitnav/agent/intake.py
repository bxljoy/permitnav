"""Intake for follow-on questions (phase 5, step 3).

"And can my spouse work too?" cannot be searched: the facts are in an earlier turn. When a
question comes with earlier questions (the conversation's memory, kept by the client: the
server stores nothing), a cheap model call rewrites it into one self-contained question in
the same language, carrying over only facts the user stated, and records the user's
situation (for the editable "your situation" panel in phase 6). Search and answer then use
the rewritten question, exactly as plain RAG does.

No earlier questions: no call, so single questions cost what they always did. Any failure
(a model error, an unreadable reply) falls back to the question as asked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from permitnav import tracing
from permitnav.answering.answerer import _json_object
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
    question: str  # what is searched and answered: the rewrite, or `asked` as a fallback
    rewritten: bool
    situation: dict[str, str] = field(default_factory=dict)  # only what the user stated
    completion: Completion | None = None
    # Why memory was not used, when it was not: "model error", or the reply's finish reason
    # when it was unreadable (e.g. "length": cut off). None when the rewrite was read.
    fallback: str | None = None


class IntakeRewriter:
    def __init__(
        self,
        llm: LLM,
        model: str,
        max_tokens: int = 2500,
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
        if not earlier:
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
                result = parse_intake(completion.text, question)
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
                    # which facts the user gave, never what they were
                    "permitnav.situation_fields": sorted(result.situation),
                },
            )
            return result


def parse_intake(text: str, asked: str) -> Intake:
    """The rewrite's reply; unreadable or empty → the question as asked."""
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
    return Intake(
        asked=asked, question=rewrite if changed else asked, rewritten=changed, situation=situation
    )


def _unreadable(text: str) -> bool:
    reply = _json_object(text)
    return not (reply and isinstance(reply.get("question"), str) and reply["question"].strip())
