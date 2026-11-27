"""Online evals: what can be measured about a question asked on the live site.

A question from a person has no reference answer, so nothing here says whether an answer was
right; that stays with the golden sets. What can be told without one:
- facts the pipeline already knows (the answer's kind, whether the model had to be asked a
  second time, the question's language): counted for every question;
- whether the answer stays within the sources it was shown: the eval judge's faithfulness
  check, run after the answer has gone out, on a share of the answers (LIVE_JUDGE_SHARE);
- whether the person found it helpful: a click under the answer.

Each goes to two places, neither of which gets any text: a score on the question's trace in
Langfuse (permitnav.scores), to find and filter single cases, and a count per day in
Postgres (permitnav.stats), because Langfuse's free plan shows only the last 30 days.

None of it can delay or fail an answer: the counts and the judge run on background threads,
and every error here is logged by its type and dropped. Stopping the service waits a few
seconds for them, not for a judge call to finish.

The judge is another model at another provider than the answer model, so a judged answer's
question, answer and sources are sent there too. While the maintainer is the only one asking
that is their own choice; it has to be decided again, with the rest of the privacy work,
before other people get access.
"""

from __future__ import annotations

import contextvars
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import psycopg

from permitnav import db, spend, stats, tracing
from permitnav.agent.intake import MAX_HISTORY
from permitnav.answering.answerer import Answer
from permitnav.evals.judge import CalibrationItem, Judge
from permitnav.llm.port import LLM
from permitnav.retrieval.translate import language_of
from permitnav.scores import Scores

log = logging.getLogger("permitnav.online")

JUDGED_KINDS = ("answered", "not_settled")  # the kinds that state something about the rules
MAX_JUDGES_WAITING = 8  # more answers than this waiting for the judge: the next is skipped
CONNECT_TIMEOUT_S = 3  # for the counts: a database that does not answer is given up on
STOP_WAIT_S = 4.0  # at shutdown: how long a judge call under way is waited for


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def facts(answer: Answer, question: str, history: list[str]) -> dict[str, str]:
    """What is counted for every question (the vocabulary of permitnav.stats)."""
    language = language_of(question)
    return {
        "kind": answer.kind,
        "language": language if language in ("sv", "en") else "unknown",
        "follow_on": _yes_no(history),
        "second_call": _yes_no(answer.earlier),
        "recovered": _yes_no(answer.recovered),
        "other_language": _yes_no(answer.other_language),
    }


def judge_share() -> float:
    """LIVE_JUDGE_SHARE from the environment: the share of answers the live judge reads,
    0 (unset: none) to 1 (all). Anything else is an error, so a mistyped value is not taken
    for "off"."""
    value = os.environ.get("LIVE_JUDGE_SHARE", "").strip()
    if not value:
        return 0.0
    try:
        share = float(value)
    except ValueError:
        share = -1.0
    if not 0 <= share <= 1:
        raise ValueError(f"LIVE_JUDGE_SHARE must be a number from 0 to 1: {value!r}")
    return share


def live_judge(llm: LLM) -> Judge | None:
    """The eval judge (JUDGE_MODEL, JUDGE_REASONING_EFFORT) on the given client, when
    LIVE_JUDGE_SHARE asks for one. The client is the service's metered one, so the judge's
    calls count towards the daily spend cap."""
    if judge_share() == 0:
        return None
    model = os.environ.get("JUDGE_MODEL", "").strip()
    if not model:
        raise ValueError("LIVE_JUDGE_SHARE is set but JUDGE_MODEL is not; see .env.example")
    effort = os.environ.get("JUDGE_REASONING_EFFORT", "").strip() or None
    if effort and effort not in ("low", "medium", "high"):
        raise ValueError(f"JUDGE_REASONING_EFFORT must be low, medium or high, not {effort!r}")
    if effort:  # a reasoning model: its thinking counts against the token limit (cli._judge)
        return Judge(llm, model, max_tokens=8000, reasoning_effort=effort)
    return Judge(llm, model)


@dataclass
class Online:
    database_url: str | None = None
    scores: Scores | None = None
    judge: Judge | None = None
    judge_share: float = 0.0
    spend_cap_usd: float | None = None  # at the cap, the judge stops too
    pick: Callable[[], float] = random.random
    _pool: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-judge"),
        repr=False,
    )
    _counts: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="counts"),
        repr=False,
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _waiting: int = 0  # answers handed to the judge and not finished
    _judging: bool = False  # a judge call is under way

    def answered(
        self, answer: Answer, question: str, history: list[str], trace_id: str | None
    ) -> None:
        """Count a finished question, score its trace, and perhaps hand it to the judge.
        Called inside the question's trace, so the judge's call becomes part of it."""
        counted = facts(answer, question, history)
        self._counts.submit(self._count, counted)  # not on the answer's way out
        if self.scores is not None:
            self.scores.add(trace_id, "kind", answer.kind)
            self.scores.add(trace_id, "second_call", bool(answer.earlier))
        if self._to_judge(answer):
            item = CalibrationItem(
                id=trace_id or "",
                question=question,
                language=counted["language"],
                expected_kind="",
                reference_answer="",
                kind=answer.kind,
                answer=answer.text,
                sources=[
                    {
                        "marker": s.marker,
                        "role": s.role,
                        "heading": s.heading,
                        "dates": s.dates,
                        "text": s.text,
                    }
                    for s in answer.sources
                ],
                # What the answer was built from: the last few earlier questions (intake),
                # not everything a client sent along.
                turns=[h for h in history if h.strip()][-MAX_HISTORY:],
            )
            try:
                # A copy of this request's context: its trace, and its trace settings.
                self._pool.submit(contextvars.copy_context().run, self._judge, item, trace_id)
            except RuntimeError:  # shutting down
                with self._lock:
                    self._waiting -= 1

    def rated(self, trace_id: str | None, helpful: bool) -> bool:
        """A click under an answer; whether it was counted. The count is of clicks: a
        click sent twice (a lost reply, two tabs) counts twice, while its score in Langfuse,
        one per trace, is replaced."""
        counted = self._count({"rating": "helpful" if helpful else "not_helpful"})
        if counted and self.scores is not None:
            self.scores.add(trace_id, "helpful", helpful)
        return counted

    def settle(self, timeout_s: float = 10.0) -> bool:
        """Wait until no answer is with the judge or waiting for it (tests, scripts).
        False on a timeout."""
        deadline = time.monotonic() + timeout_s
        while self._waiting > 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, wait_s: float = STOP_WAIT_S) -> None:
        """Shutdown: write the counts, give a judge call under way a few seconds (answers
        still waiting for the judge are dropped), then send the scores. Bounded, so the
        traces can still be flushed before the process is stopped."""
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._counts.shutdown(wait=True)  # each bounded by the connect timeout
        deadline = time.monotonic() + wait_s
        while self._judging and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.scores is not None:
            self.scores.flush(wait_s)

    def _to_judge(self, answer: Answer) -> bool:
        """Whether this answer goes to the judge; if so it is counted as waiting."""
        if self.judge is None or answer.kind not in JUDGED_KINDS or not answer.sources:
            return False
        if self.pick() >= self.judge_share:
            return False
        with self._lock:
            if self._waiting >= MAX_JUDGES_WAITING:
                log.warning("live judge skipped: %d answers already waiting", self._waiting)
                return False
            self._waiting += 1
        return True

    def _judge(self, item: CalibrationItem, trace_id: str | None) -> None:
        self._judging = True
        try:
            if self.judge is None or self._at_cap():
                return
            with tracing.span("live_judge", "evaluator") as current:
                verdict = self.judge.faithfulness(item)
                value = "unread" if verdict.faithful is None else _yes_no(verdict.faithful)
                tracing.set_attributes(
                    current,
                    **{
                        "permitnav.faithful": value,
                        "permitnav.unsupported_claims": len(verdict.unsupported),
                    },
                )
                if verdict.faithful is not None:  # an unread verdict's reason is an error text
                    tracing.text(
                        current,
                        output={"unsupported": list(verdict.unsupported), "reason": verdict.reason},
                    )
            self._count({"faithful": value})
            if verdict.faithful is not None and self.scores is not None:
                self.scores.add(trace_id, "faithful", verdict.faithful)
        except Exception as error:  # the answer is long gone; never the text, only the type
            log.error("live judge failed: %s", type(error).__name__)
        finally:
            self._judging = False
            with self._lock:
                self._waiting -= 1

    def _at_cap(self) -> bool:
        if self.spend_cap_usd is None:
            return False
        try:
            with self._connect() as conn:
                return spend.spent_today(conn) >= self.spend_cap_usd
        except psycopg.Error:
            return True  # cannot tell: do not spend

    def _count(self, counted: dict[str, str]) -> bool:
        try:
            with self._connect() as conn:
                stats.add(conn, counted)
            return True
        except (psycopg.Error, ValueError) as error:
            log.error("daily counts not written: %s", type(error).__name__)
            return False

    def _connect(self) -> psycopg.Connection:
        url = self.database_url or db.database_url()
        return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_S)


def from_environment(
    llm: Any, database_url: str | None = None, spend_cap_usd: float | None = None
) -> Online:
    """The production wiring. A mistyped setting stops the start."""
    from permitnav import scores

    return Online(
        database_url=database_url,
        scores=scores.from_environment(),
        judge=live_judge(llm),
        judge_share=judge_share(),
        spend_cap_usd=spend_cap_usd,
    )
