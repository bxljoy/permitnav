"""A shadow judge: a second faithfulness judge read against the deployed one on real
answers, before it is allowed to replace it.

After the deployed live judge has given its verdict on an answer (permitnav.online), the
shadow judge is asked about the very same thing: the same question, answer and sources as
captured for the first judge, the same prompt, the same token limit. Nothing is searched or
answered again. Both verdicts, and what each judge cost and how long it took, go to one
table (migration 010), from which `permitnav shadow report` makes the review.

What it must never do, and how that is kept:
- change anything a person sees or anything already measured: it runs on the judge's
  background thread after the deployed judge's scores and counts are written, sends no
  score, counts nothing in the daily counts, and every error in it is logged by its type
  and dropped;
- read anyone else's question: only a request made with the maintainer's token, which is
  recorded in full already, is shadowed. So every row can be opened and read by the
  maintainer, and no other person's answer goes to a second model;
- store text: the table has verdicts, counts, tokens, costs, times and hashes;
- run on: it stops by itself after SHADOW_JUDGE_LIMIT answers or SHADOW_JUDGE_BUDGET_USD,
  whichever comes first, and the day's spend cap stops it like any other model call.

Off unless SHADOW_JUDGE_MODEL is set. The sample is the maintainer's own questions: a
controlled look at real answers, not a picture of what other people would ask.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from permitnav import spend, tracing
from permitnav.evals.judge import CalibrationItem, Judge, Verdict, _faithfulness_material
from permitnav.evals.provenance import prompt_hash

log = logging.getLogger("permitnav.shadow")

PROMPT = "judge_faithfulness.md"
DEFAULT_LIMIT = 100
DEFAULT_BUDGET_USD = 1.0
# Held against the budget for a call whose cost is not known: while it is under way, and
# for good when it failed or the provider reported no cost. Measured cost on the benchmark
# is about $0.004 a verdict; a long answer asked twice stays under this.
ESTIMATE_USD = 0.02
_LOCK = 7_326_410  # advisory lock: one reservation at a time, across processes
# Why an answer the deployed judge completed is not in the sample. The first is not the
# maintainer's question, so it was never eligible; the others were eligible and skipped.
NOT_ELIGIBLE = ("not_maintainer",)
SKIPPED = ("busy", "sample_full", "budget", "duplicate", "spend_cap", "spend_unreadable")
REASONS = (*NOT_ELIGIBLE, *SKIPPED)


@dataclass(frozen=True)
class Settings:
    model: str
    effort: str | None
    limit: int
    budget_usd: float


def settings() -> Settings | None:
    """The shadow judge's settings from the environment; None when SHADOW_JUDGE_MODEL is
    not set (the default: no shadow judge). A mistyped value is an error, not "off"."""
    model = os.environ.get("SHADOW_JUDGE_MODEL", "").strip()
    if not model:
        return None
    effort = os.environ.get("SHADOW_JUDGE_REASONING_EFFORT", "").strip() or None
    if effort and effort not in ("low", "medium", "high"):
        raise ValueError(
            f"SHADOW_JUDGE_REASONING_EFFORT must be low, medium or high, not {effort!r}"
        )
    return Settings(
        model,
        effort,
        _number("SHADOW_JUDGE_LIMIT", DEFAULT_LIMIT, int),
        _number("SHADOW_JUDGE_BUDGET_USD", DEFAULT_BUDGET_USD, float),
    )


def _number(name: str, default, kind):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        number = kind(value)
    except ValueError:
        number = -1
    if not 0 < number < float("inf"):
        raise ValueError(f"{name} must be a positive number: {value!r}")
    return number


@dataclass(frozen=True)
class Reading:
    """What one judge made of one answer, without any of its words."""

    model: str
    effort: str | None
    version: str | None
    status: str  # completed | failed
    faithful: bool | None
    unsupported: int
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None: a call was made whose cost the provider did not report
    latency_ms: int


def reading(
    model: str, effort: str | None, version: str | None, verdict: Verdict, latency_s: float
) -> Reading:
    costs = [c.cost_usd for c in verdict.completions]
    # A cost of zero or less, or none at all, is not a price (permitnav.spend): unknown.
    known = bool(costs) and all(spend.priced(c) for c in costs)
    return Reading(
        model=model,
        effort=effort,
        version=version,
        status="failed" if verdict.faithful is None else "completed",
        faithful=verdict.faithful,
        unsupported=len(verdict.unsupported),
        calls=len(verdict.completions),
        input_tokens=sum(c.input_tokens for c in verdict.completions),
        output_tokens=sum(c.output_tokens for c in verdict.completions),
        cost_usd=sum(c or 0.0 for c in costs) if known else None,
        latency_ms=round(latency_s * 1000),
    )


def context_hash(item: CalibrationItem) -> str:
    """A hash of exactly what a faithfulness judge is given for this answer (the question,
    earlier questions, answer and sources as one text): two rows with the same hash were
    judged on the same material."""
    return hashlib.sha256(_faithfulness_material(item).encode("utf-8")).hexdigest()


def charge(shadow: Reading, estimate: float = ESTIMATE_USD) -> float:
    """What a finished shadow call takes from the budget: its reported cost when every call
    was priced and the judgement was read; otherwise the estimate, or what is known if that
    is more (a failed call may have been retried at the provider's end unseen)."""
    known = shadow.cost_usd if spend.priced(shadow.cost_usd) else None
    if shadow.status == "completed" and known is not None:
        return known
    return max(estimate, known or 0.0)


def reserve(
    conn: psycopg.Connection,
    *,
    trace_id: str,
    limit: int,
    budget_usd: float,
    release: str | None,
    hashes: tuple[str, str],
    token_limit: int,
    deployed: Reading,
    shadow: tuple[str, str | None, str | None],
    estimate: float = ESTIMATE_USD,
) -> int | str:
    """Take one place in the sample and hold the estimate against the budget: the row's
    id. Or why not: "sample_full", "budget" (it would be passed) or "duplicate" (this trace
    has a row already). One reservation at a time (an advisory lock), so two processes
    cannot both take the last place. An unfinished row keeps its place and its estimate: a
    call cut off by a restart is counted as if it had been made."""
    prompt, context = hashes
    model, effort, version = shadow
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
        sampled, charged = _used(conn)
        if sampled >= limit:
            return "sample_full"
        if charged + estimate > budget_usd:
            return "budget"
        row = conn.execute(
            """
            INSERT INTO judge_shadow (
              trace_id, release, prompt_hash, context_hash, token_limit, state, charged_usd,
              deployed_model, deployed_effort, deployed_version, deployed_faithful,
              deployed_unsupported, deployed_calls, deployed_input_tokens,
              deployed_output_tokens, deployed_cost_usd, deployed_latency_ms,
              shadow_model, shadow_effort, shadow_version)
            VALUES (%s, %s, %s, %s, %s, 'reserved', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s)
            ON CONFLICT (trace_id) DO NOTHING
            RETURNING id
            """,
            (
                trace_id,
                release,
                prompt,
                context,
                token_limit,
                estimate,
                deployed.model,
                deployed.effort,
                deployed.version,
                deployed.faithful,
                deployed.unsupported,
                deployed.calls,
                deployed.input_tokens,
                deployed.output_tokens,
                deployed.cost_usd,
                deployed.latency_ms,
                model,
                effort,
                version,
            ),
        ).fetchone()
    return row[0] if row else "duplicate"


def skipped(conn: psycopg.Connection, reason: str) -> None:
    """Count an answer the shadow judge was not asked about. Only a known reason."""
    if reason not in REASONS:
        raise ValueError(f"not a reason to skip: {reason!r}")
    with conn.transaction():
        conn.execute(
            "INSERT INTO judge_shadow_skip (reason, n) VALUES (%s, 1)"
            " ON CONFLICT (reason) DO UPDATE SET n = judge_shadow_skip.n + 1",
            (reason,),
        )


def skips(conn: psycopg.Connection) -> dict[str, int]:
    with conn.transaction():
        return dict(conn.execute("SELECT reason, n FROM judge_shadow_skip").fetchall())


def finish(conn: psycopg.Connection, row_id: int, shadow: Reading) -> None:
    """Complete a reserved row with what the shadow judge did, and replace the estimate
    held against the budget with the charge (see `charge`)."""
    with conn.transaction():
        conn.execute(
            """
            UPDATE judge_shadow SET state = 'done', charged_usd = %s, shadow_status = %s,
              shadow_faithful = %s, shadow_unsupported = %s, shadow_calls = %s,
              shadow_input_tokens = %s, shadow_output_tokens = %s, shadow_cost_usd = %s,
              shadow_latency_ms = %s
            WHERE id = %s
            """,
            (
                charge(shadow),
                shadow.status,
                shadow.faithful,
                shadow.unsupported,
                shadow.calls,
                shadow.input_tokens,
                shadow.output_tokens,
                shadow.cost_usd,
                shadow.latency_ms,
                row_id,
            ),
        )


def _used(conn: psycopg.Connection) -> tuple[int, float]:
    """Places taken in the sample (a failed judgement gives its place back, not its cost)
    and dollars held against the budget."""
    sampled, charged = conn.execute(
        "SELECT count(*) FILTER (WHERE state = 'reserved' OR shadow_status = 'completed'),"
        " coalesce(sum(charged_usd), 0) FROM judge_shadow"
    ).fetchone()  # type: ignore[misc]
    return int(sampled), float(charged)


def used(conn: psycopg.Connection) -> tuple[int, float]:
    with conn.transaction():
        return _used(conn)


@dataclass(frozen=True)
class Row:
    id: int
    at: datetime
    trace_id: str
    release: str | None
    prompt_hash: str
    context_hash: str
    token_limit: int
    state: str
    charged_usd: float
    deployed_model: str
    deployed_effort: str | None
    deployed_version: str | None
    deployed_faithful: bool
    deployed_unsupported: int
    deployed_calls: int
    deployed_input_tokens: int
    deployed_output_tokens: int
    deployed_cost_usd: float | None
    deployed_latency_ms: int
    shadow_model: str
    shadow_effort: str | None
    shadow_version: str | None
    shadow_status: str | None
    shadow_faithful: bool | None
    shadow_unsupported: int | None
    shadow_calls: int | None
    shadow_input_tokens: int | None
    shadow_output_tokens: int | None
    shadow_cost_usd: float | None
    shadow_latency_ms: int | None


def rows(conn: psycopg.Connection) -> list[Row]:
    with conn.cursor(row_factory=class_row(Row)) as cursor:
        return cursor.execute("SELECT * FROM judge_shadow ORDER BY id").fetchall()


def clear(conn: psycopg.Connection) -> int:
    """Remove every shadow row and the skip counts (after the review); how many rows there
    were. The sample and the budget start again from nothing, so unset SHADOW_JUDGE_MODEL
    first unless another round is wanted."""
    with conn.transaction():
        conn.execute("DELETE FROM judge_shadow_skip")
        return conn.execute("DELETE FROM judge_shadow").rowcount


@dataclass
class Shadow:
    """The shadow judge and the deployed judge it is read against."""

    judge: Judge
    model: str
    effort: str | None
    version: str | None
    deployed: tuple[str, str | None, str | None]  # model, effort, version
    token_limit: int
    limit: int = DEFAULT_LIMIT
    budget_usd: float = DEFAULT_BUDGET_USD
    release: str | None = None

    def compare(
        self,
        item: CalibrationItem,
        trace_id: str | None,
        verdict: Verdict,
        latency_s: float,
        connect: Callable[[], psycopg.Connection],
    ) -> str:
        """Ask the shadow judge about an answer the deployed judge has just judged, and
        record both. Returns what happened: "completed" or "failed", or the reason nothing
        was asked (one of REASONS, counted in the table). The caller logs what it raises."""
        if verdict.faithful is None:
            return "no_verdict"  # nothing to compare with: not counted, the caller's business
        mine = reading(*self.deployed, verdict, latency_s)
        with connect() as conn:
            if not trace_id or tracing.content_mode() != "full":
                skipped(conn, "not_maintainer")  # not the maintainer's own question
                return "not_maintainer"
            row_id = reserve(
                conn,
                trace_id=trace_id,
                limit=self.limit,
                budget_usd=self.budget_usd,
                release=self.release,
                hashes=(prompt_hash(PROMPT), context_hash(item)),
                token_limit=self.token_limit,
                deployed=mine,
                shadow=(self.model, self.effort, self.version),
            )
            if isinstance(row_id, str):
                skipped(conn, row_id)
                return row_id
        result = Reading(self.model, self.effort, self.version, "failed", None, 0, 0, 0, 0, None, 0)
        try:
            with tracing.span("shadow_judge", "evaluator") as current:
                started = time.monotonic()
                theirs = self.judge.faithfulness(item)  # the same item: nothing is rebuilt
                result = reading(
                    self.model, self.effort, self.version, theirs, time.monotonic() - started
                )
                tracing.set_attributes(
                    current,
                    **{
                        "permitnav.judge_status": result.status,
                        "permitnav.judge_version": self.version,
                        "permitnav.faithful": _yes_no(result.faithful),
                        "permitnav.unsupported_claims": result.unsupported,
                        "permitnav.agrees_with_deployed": _yes_no(
                            None if result.faithful is None else result.faithful == mine.faithful
                        ),
                    },
                )
                if result.faithful is not None:  # full-content traces only (tracing.text)
                    tracing.text(
                        current,
                        output={"unsupported": list(theirs.unsupported), "reason": theirs.reason},
                    )
        finally:  # whatever happened, the row is closed and the budget charged
            with connect() as conn:
                finish(conn, row_id, result)
        return result.status


def _yes_no(value: bool | None) -> str | None:
    return None if value is None else ("yes" if value else "no")
