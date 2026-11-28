"""Online evals: what can be measured about a question asked on the live site.

A question from a person has no reference answer, so nothing here says whether an answer was
right; that stays with the golden sets. What can be told without one:
- facts the pipeline already knows, checked in code with no further model call: whether
  there was a usable answer, whether the reply cited only sources it was given, whether
  the answer is in the question's language, whether the model had to be asked a second
  time: for every question;
- whether the answer stays within the sources it was shown: the eval judge's faithfulness
  check, run after the answer has gone out, on a share of the answers (LIVE_JUDGE_SHARE);
- whether the person found it helpful: a click under the answer.

Each goes to two places, neither of which gets any text: a score on the question's trace in
Langfuse (permitnav.scores), to find and filter single cases, and a count per day in
Postgres (permitnav.stats), because Langfuse's free plan shows only the last 30 days.

Scores are quality signals only (each costs a Langfuse unit): `output_valid`,
`citation_id_valid`, `language_match`, `second_call`, `faithful`, `judge_status`,
`helpful`. What merely describes a question (its answer kind, the versions that produced
it) is a tag or an attribute on the trace.

"Faithful" is narrow on purpose: every factual claim in the answer is stated by a source the
answer model was shown. An answer can be faithful and wrong: it can apply a real rule to a
permit it does not cover, or leave out a condition. Those are questions of correctness,
measured against reference answers by the offline evals, not here.

An answer the judge was not asked about, or could not judge, has no `faithful` verdict at
all: it is never counted as faithful or as unfaithful. `judge_status` and the `judge` count
say what happened instead.

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

from permitnav import db, shadow, spend, stats, tracing
from permitnav.agent.intake import MAX_HISTORY
from permitnav.answering.answerer import MARKER, Answer
from permitnav.evals.judge import CalibrationItem, Judge, Verdict
from permitnav.llm.port import LLM
from permitnav.retrieval.translate import language_of
from permitnav.scores import Scores

log = logging.getLogger("permitnav.online")

LANGUAGE_MATCH = {"match": "yes", "mismatch": "no", "unknown": "unknown"}
JUDGED_KINDS = ("answered", "not_settled")  # the kinds that state something about the rules
MAX_JUDGES_WAITING = 8  # more answers than this waiting for the judge: the next is skipped
CONNECT_TIMEOUT_S = 3  # for the counts: a database that does not answer is given up on
STOP_WAIT_S = 4.0  # at shutdown: how long a judge call under way is waited for
# A reasoning model's thinking counts against the token limit (cli._judge); the same limit
# is given to a shadow judge, whatever its own settings.
REASONING_TOKENS, PLAIN_TOKENS = 8000, 2000


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def cites_only_its_sources(answer: Answer) -> bool:
    """A check on the cleaning: the answer as shown names no source it was not given."""
    given = {source.marker for source in answer.sources}
    return all(marker in given for marker in MARKER.findall(answer.text))


def facts(answer: Answer, question: str, history: list[str]) -> dict[str, str]:
    """What is counted for every question (the vocabulary of permitnav.stats)."""
    language = language_of(question)
    counted = {
        "kind": answer.kind,
        "language": language if language in ("sv", "en") else "unknown",
        "follow_on": _yes_no(history),
        "second_call": _yes_no(answer.earlier),
        "recovered": _yes_no(answer.recovered),
        "output": "invalid" if answer.kind == "failed" else "valid",
    }
    if answer.kind != "failed":  # there is an answer to say these of
        counted["citation_generated"] = "invalid" if answer.dropped_markers else "valid"
        counted["citation_final"] = "valid" if cites_only_its_sources(answer) else "invalid"
        counted["language_match"] = LANGUAGE_MATCH.get(answer.language, "unknown")
    return counted


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


def judge_settings() -> tuple[str, str | None] | None:
    """The live judge's model and reasoning effort (JUDGE_MODEL, JUDGE_REASONING_EFFORT),
    when LIVE_JUDGE_SHARE asks for a judge; None when it does not."""
    if judge_share() == 0:
        return None
    model = os.environ.get("JUDGE_MODEL", "").strip()
    if not model:
        raise ValueError("LIVE_JUDGE_SHARE is set but JUDGE_MODEL is not; see .env.example")
    effort = os.environ.get("JUDGE_REASONING_EFFORT", "").strip() or None
    if effort and effort not in ("low", "medium", "high"):
        raise ValueError(f"JUDGE_REASONING_EFFORT must be low, medium or high, not {effort!r}")
    return model, effort


def live_judge(llm: LLM) -> Judge | None:
    """The eval judge on the given client, when LIVE_JUDGE_SHARE asks for one. The client is
    the service's metered one, so the judge's calls count towards the daily spend cap."""
    settings = judge_settings()
    if settings is None:
        return None
    model, effort = settings
    return Judge(llm, model, max_tokens=token_limit(effort), reasoning_effort=effort)


def token_limit(effort: str | None) -> int:
    """The live judge's token limit, set by whether it is a reasoning model."""
    return REASONING_TOKENS if effort else PLAIN_TOKENS


def shadow_judge(llm: LLM, release: str | None = None) -> shadow.Shadow | None:
    """The shadow judge (permitnav.shadow), when SHADOW_JUDGE_MODEL asks for one: a second
    judge on the same metered client, with the deployed judge's prompt and token limit. It
    needs a deployed judge to be read against."""
    from permitnav import versions

    wanted = shadow.settings()
    if wanted is None:
        return None
    deployed = judge_settings()
    if deployed is None:
        raise ValueError("SHADOW_JUDGE_MODEL is set but there is no live judge (LIVE_JUDGE_SHARE)")
    if wanted.effort and not deployed[1]:
        # Its thinking would have to fit the plain limit and every reply would come back
        # empty; and the two judges are to have the same limit.
        raise ValueError(
            "SHADOW_JUDGE_REASONING_EFFORT needs JUDGE_REASONING_EFFORT: both judges get the"
            " deployed judge's token limit"
        )
    limit = token_limit(deployed[1])
    return shadow.Shadow(
        judge=Judge(llm, wanted.model, max_tokens=limit, reasoning_effort=wanted.effort),
        model=wanted.model,
        effort=wanted.effort,
        version=versions.judge_version(wanted.model, wanted.effort),
        deployed=(*deployed, versions.judge_version(*deployed)),
        token_limit=limit,
        limit=wanted.limit,
        budget_usd=wanted.budget_usd,
        release=release,
    )


@dataclass
class Online:
    database_url: str | None = None
    scores: Scores | None = None
    judge: Judge | None = None
    judge_share: float = 0.0
    spend_cap_usd: float | None = None  # at the cap, the judge stops too
    pick: Callable[[], float] = random.random
    # Version names (permitnav.versions) counted with each question and sent with the
    # judge's scores; None: not recorded.
    release: str | None = None
    judge_version: str | None = None
    # A second judge read against the first on the maintainer's own questions; None: off.
    shadow: shadow.Shadow | None = None
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
    # The shadow judge has a thread of its own, so it never holds up the deployed judge's
    # queue; it reads one answer at a time and an answer that arrives meanwhile is skipped.
    _shadow_pool: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow"),
        repr=False,
    )
    _shadowing: bool = False
    _judging: bool = False  # a judge call is under way

    def answered(
        self, answer: Answer, question: str, history: list[str], trace_id: str | None
    ) -> str:
        """Count a finished question, score its trace, and perhaps hand it to the judge.
        Called inside the question's trace, so the judge's call becomes part of it.
        Returns what became of the judge for this answer so far: "queued", or why it was
        not asked ("off", "ineligible", "not_sampled", "queue_full")."""
        counted = facts(answer, question, history)
        status = self._judge_status(answer)
        handed_over = False
        try:
            self._record(answer, counted, status, trace_id)
            if status == "queued":
                self._hand_to_judge(answer, question, history, counted["language"], trace_id)
                handed_over = True
        finally:
            if status == "queued" and not handed_over:  # the judge's slot is given back
                with self._lock:
                    self._waiting -= 1
        return status

    def _record(
        self, answer: Answer, counted: dict[str, str], status: str, trace_id: str | None
    ) -> None:
        if status != "queued":
            counted["judge"] = status
        if self.release:
            counted["release"] = self.release
        self._counts.submit(self._count, counted)  # not on the answer's way out
        if self.scores is not None:
            self.scores.add(trace_id, "output_valid", answer.kind != "failed")
            self.scores.add(trace_id, "second_call", bool(answer.earlier))
            if "citation_generated" in counted:
                self.scores.add(
                    trace_id, "citation_id_valid", counted["citation_generated"] == "valid"
                )
            if counted.get("language_match") in ("yes", "no"):  # unknown: no score
                self.scores.add(trace_id, "language_match", counted["language_match"] == "yes")
            if status == "queue_full":  # chosen for the judge, and not judged
                self.scores.add(trace_id, "judge_status", status, self._judge_metadata())

    def _hand_to_judge(
        self,
        answer: Answer,
        question: str,
        history: list[str],
        language: str,
        trace_id: str | None,
    ) -> None:
        """Queue the answer for the judge; raises when the service is shutting down."""
        item = CalibrationItem(
            id=trace_id or "",
            question=question,
            language=language,
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
        # A copy of this request's context: its trace, and its trace settings.
        self._pool.submit(contextvars.copy_context().run, self._judge, item, trace_id)

    def errored(self, trace_id: str | None) -> None:
        """A question that got no answer because the model call failed."""
        counted = {"output": "error"}
        if self.release:
            counted["release"] = self.release
        self._counts.submit(self._count, counted)
        if self.scores is not None:
            self.scores.add(trace_id, "output_valid", False)

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
        while self._waiting > 0 or self._shadowing:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, wait_s: float = STOP_WAIT_S) -> None:
        """Shutdown: write the counts, give a judge call under way a few seconds (answers
        still waiting for the judge are dropped, and counted nowhere), then send the scores.
        Bounded, so the traces can still be flushed before the process is stopped."""
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._shadow_pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + wait_s
        while (self._judging or self._shadowing) and time.monotonic() < deadline:
            time.sleep(0.02)
        self._counts.shutdown(wait=True)  # each bounded by the connect timeout
        if self.scores is not None:
            self.scores.flush(wait_s)

    def _judge_status(self, answer: Answer) -> str:
        """Whether this answer goes to the judge ("queued": it is then counted as waiting),
        or why not."""
        if self.judge is None:
            return "off"
        if answer.kind not in JUDGED_KINDS or not answer.sources:
            return "ineligible"
        if self.pick() >= self.judge_share:
            return "not_sampled"
        with self._lock:
            if self._waiting >= MAX_JUDGES_WAITING:
                log.warning("live judge skipped: %d answers already waiting", self._waiting)
                return "queue_full"
            self._waiting += 1
        return "queued"

    def _judge_metadata(self) -> dict[str, str] | None:
        return {"judge_version": self.judge_version} if self.judge_version else None

    def _judge(self, item: CalibrationItem, trace_id: str | None) -> None:
        """One answer's judgement, and what became of it: "completed" with a verdict,
        "failed" without one, "capped" when the day's spend cap forbade the call. Only a
        completed judgement gives a `faithful` verdict."""
        self._judging = True
        status, faithful = "failed", None
        judged: tuple[Verdict, float] | None = None  # the verdict, and how long it took
        try:
            if self.judge is None:
                return
            at_cap = self._at_cap()
            if at_cap is None:
                return  # the day's spend cannot be read: no call, and "failed", not "capped"
            if at_cap:
                status = "capped"
                return
            with tracing.span("live_judge", "evaluator") as current:
                started = time.monotonic()
                verdict = self.judge.faithfulness(item)
                judged = (verdict, time.monotonic() - started)
                faithful = verdict.faithful
                status = "failed" if faithful is None else "completed"
                tracing.set_attributes(
                    current,
                    **{
                        "permitnav.judge_status": status,
                        "permitnav.judge_version": self.judge_version,
                        "permitnav.faithful": None if faithful is None else _yes_no(faithful),
                        "permitnav.unsupported_claims": len(verdict.unsupported),
                    },
                )
                if faithful is not None:  # a failed judgement's reason is an error text
                    tracing.text(
                        current,
                        output={"unsupported": list(verdict.unsupported), "reason": verdict.reason},
                    )
        except Exception as error:  # the answer is long gone; never the text, only the type
            log.error("live judge failed: %s", type(error).__name__)
        finally:
            try:
                counted = {"judge": status}
                if faithful is not None:
                    counted["faithful"] = _yes_no(faithful)
                if self.judge_version and status != "capped":  # the judge was called
                    counted["judge_version"] = self.judge_version
                self._count(counted)
                if self.scores is not None:
                    metadata = self._judge_metadata()
                    self.scores.add(trace_id, "judge_status", status, metadata)
                    if faithful is not None:
                        self.scores.add(trace_id, "faithful", faithful, metadata)
            finally:
                self._judging = False
                with self._lock:
                    self._waiting -= 1
                # After everything of the deployed judge's is recorded and its place in
                # the queue is free again.
                if judged is not None and status == "completed":
                    self._hand_to_shadow(item, trace_id, *judged)

    def _hand_to_shadow(
        self, item: CalibrationItem, trace_id: str | None, verdict: Verdict, latency_s: float
    ) -> None:
        """Pass a judged answer to the shadow judge's own thread, unless there is none or
        it is busy: then this answer is simply not in the sample."""
        if self.shadow is None:
            return
        with self._lock:
            busy = self._shadowing
            self._shadowing = True
        if busy:
            self._shadow_skipped("busy")
            return
        try:
            self._shadow_pool.submit(
                contextvars.copy_context().run, self._shadow, item, trace_id, verdict, latency_s
            )
        except Exception as error:  # shutting down
            self._shadowing = False
            log.info("shadow judge skipped: %s", type(error).__name__)

    def _shadow(
        self, item: CalibrationItem, trace_id: str | None, verdict: Verdict, latency_s: float
    ) -> None:
        """The shadow judge on the same answer, if there is one. It can change nothing
        above: the deployed judge's verdict, scores and counts are written already, and
        whatever goes wrong here is logged by its type and dropped."""
        try:
            if self.shadow is None:
                return
            at_cap = self._at_cap()
            if at_cap is not False:
                self._shadow_skipped("spend_cap" if at_cap else "spend_unreadable")
                return
            self.shadow.compare(item, trace_id, verdict, latency_s, self._connect)
        except Exception as error:
            log.error("shadow judge failed: %s", type(error).__name__)
        finally:
            self._shadowing = False

    def _shadow_skipped(self, reason: str) -> None:
        """Count an answer the shadow was not asked about; a count lost is only logged."""
        try:
            with self._connect() as conn:
                shadow.skipped(conn, reason)
        except Exception as error:
            log.error("shadow skip not counted: %s", type(error).__name__)

    def _at_cap(self) -> bool | None:
        """Whether the day's spend has reached the cap; None when that cannot be read (the
        database is away): then nothing is spent, and it is not called the cap."""
        if self.spend_cap_usd is None:
            return False
        try:
            with self._connect() as conn:
                return spend.spent_today(conn) >= self.spend_cap_usd
        except psycopg.Error:
            return None

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
    from permitnav import scores, versions

    settings = judge_settings()
    release = versions.release()
    return Online(
        database_url=database_url,
        scores=scores.from_environment(),
        judge=live_judge(llm),
        judge_share=judge_share(),
        spend_cap_usd=spend_cap_usd,
        release=release,
        judge_version=versions.judge_version(*settings) if settings else None,
        shadow=shadow_judge(llm, release),
    )
