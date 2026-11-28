"""The review of a shadow comparison (permitnav.shadow): `permitnav shadow report`.

Numbers come from the judge_shadow table. The words (the question, the answer, what each
judge quoted as unsupported and why) are in no table; for each case shown they are read
from the trace in Langfuse, where the maintainer's own questions are recorded in full. So
the report prints the maintainer's own questions and answers: it is for their eyes, and it
is written to the terminal or a file they name, nowhere else.

Disagreements come first and all of them are shown; then the answers both judges called
not faithful (did they quote the same claim?); then a random sample of the answers both
called faithful, the place where a miss by both would hide. Each case ends with a line for
the maintainer's own verdict, because a judge's verdict is not the truth of the matter.

The sample is the maintainer's own questions, asked while the shadow was on: a controlled
evaluation on real answers, not a statistically representative picture of production.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import random
import statistics
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC

from permitnav.evals.faithfulness import SHARED, shared_run
from permitnav.shadow import NOT_ELIGIBLE, SKIPPED, Row
from permitnav.tracing import LANGFUSE_EU

CAVEAT = (
    "These are the maintainer's own questions, asked while the shadow judge was on: a"
    " controlled evaluation on real answers, not a statistically representative production"
    " benchmark. A judge's verdict is not the truth; the review below is what decides."
)


SKIP_WORDS = {
    "busy": "the shadow judge was still reading the answer before",
    "sample_full": "the sample limit was reached",
    "budget": "the budget would have been passed",
    "duplicate": "the trace was in the sample already",
    "spend_cap": "the day's spend cap was reached",
    "spend_unreadable": "the day's spend could not be read",
}


@dataclass(frozen=True)
class Texts:
    """What the trace holds in words for one case; empty where the trace has none."""

    question: str = ""
    answer: str = ""
    deployed_claims: tuple[str, ...] = ()
    deployed_reason: str = ""
    shadow_claims: tuple[str, ...] = ()
    shadow_reason: str = ""
    problem: str = ""  # why there are no words, if there are none


Reader = Callable[[str], Texts]


def langfuse_reader(timeout_s: float = 30.0) -> Reader | None:
    """Reads a trace's texts from Langfuse with the keys in the environment; None when
    there are none."""
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public or not secret:
        return None
    host = os.environ.get("LANGFUSE_HOST", "").strip() or LANGFUSE_EU
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()

    def read(trace_id: str) -> Texts:
        query = urllib.parse.urlencode(
            {"traceId": trace_id, "limit": 100, "fields": "core,basic,io"}
        )
        request = urllib.request.Request(
            f"{host}/api/public/v2/observations?{query}",
            headers={"Authorization": f"Basic {auth}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                reply = json.load(response)
        except (OSError, http.client.HTTPException, ValueError) as error:
            # One trace that cannot be read is one case without words, not a lost report.
            return Texts(problem=f"the trace could not be read ({type(error).__name__})")
        observations = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(observations, list):
            return Texts(problem="the trace could not be read (an unexpected reply)")
        return texts([o for o in observations if isinstance(o, dict)])

    return read


def _value(raw: object) -> object:
    """An input or output as Langfuse returns it: JSON in a string, or the value itself."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _judgement(raw: object) -> tuple[tuple[str, ...], str]:
    value = _value(raw)
    if not isinstance(value, dict):
        return (), ""
    claims = value.get("unsupported") or []
    if not isinstance(claims, list):
        claims = [claims]
    reason = str(value.get("reason") or "").removeprefix("faithfulness: ")
    return tuple(str(c) for c in claims), reason


def texts(observations: list[dict]) -> Texts:
    """The words of one case from its trace's steps (their names are tracing's)."""
    named: dict[str, dict] = {}
    for observation in observations:
        named.setdefault(str(observation.get("name")), observation)
    answer_step = named.get("answer", {})
    question, answer = _value(answer_step.get("input")), _value(answer_step.get("output"))
    deployed = _judgement(named.get("live_judge", {}).get("output"))
    shadow = _judgement(named.get("shadow_judge", {}).get("output"))
    found = Texts(
        question=question if isinstance(question, str) else "",
        answer=answer if isinstance(answer, str) else "",
        deployed_claims=deployed[0],
        deployed_reason=deployed[1],
        shadow_claims=shadow[0],
        shadow_reason=shadow[1],
    )
    if not found.question and not found.answer:
        return Texts(problem="the trace holds no text (not found, or older than Langfuse shows)")
    return found


def same_claim(a: tuple[str, ...], b: tuple[str, ...]) -> bool | None:
    """Whether the two judges quoted a claim in common (the same words, or a run of SHARED
    characters in both); None when one of them quoted none. A short claim that merely
    occurs inside a longer one ("30 days") is not taken for the same claim."""
    if not a or not b:
        return None
    return any(shared_run(x, y) >= SHARED or shared_run(x, y, whole=True) for x in a for y in b)


@dataclass
class Summary:
    rows: list[Row]
    compared: list[Row] = field(init=False)  # both judges gave a verdict
    failed: list[Row] = field(init=False)
    unfinished: list[Row] = field(init=False)

    def __post_init__(self) -> None:
        self.compared = [r for r in self.rows if r.shadow_status == "completed"]
        self.failed = [r for r in self.rows if r.shadow_status == "failed"]
        self.unfinished = [r for r in self.rows if r.state == "reserved"]

    def cell(self, deployed_faithful: bool, shadow_faithful: bool) -> list[Row]:
        return [
            r
            for r in self.compared
            if r.deployed_faithful is deployed_faithful and r.shadow_faithful is shadow_faithful
        ]


def _usd(values: list[float | None]) -> str:
    known = [v for v in values if v is not None]
    note = f" ({len(values) - len(known)} unpriced)" if len(known) < len(values) else ""
    return f"${sum(known):.4f}{note}"


def _latency(values: list[int]) -> str:
    if not values:
        return "n/a"
    return (
        f"median {statistics.median(values) / 1000:.1f} s, mean"
        f" {statistics.fmean(values) / 1000:.1f} s, longest {max(values) / 1000:.1f} s"
    )


def _judge_lines(name: str, rows: list[Row], prefix: str) -> list[str]:
    get = lambda r, k: getattr(r, f"{prefix}_{k}")  # noqa: E731
    settings = sorted(
        {(get(r, "model"), get(r, "effort") or "default", get(r, "version") or "") for r in rows}
    )
    flagged = sum(get(r, "faithful") is False for r in rows)
    times = [get(r, "latency_ms") for r in rows if get(r, "latency_ms") is not None]
    lines = [
        f"- **{name}**: " + "; ".join(f"`{m}`, effort {e}, version `{v}`" for m, e, v in settings),
        f"  - called not faithful: {flagged} of {len(rows)}",
        f"  - tokens: {sum(get(r, 'input_tokens') or 0 for r in rows):,} in,"
        f" {sum(get(r, 'output_tokens') or 0 for r in rows):,} out;"
        f" model calls: {sum(get(r, 'calls') or 0 for r in rows)}",
        f"  - cost: {_usd([get(r, 'cost_usd') for r in rows])}",
        f"  - latency: {_latency(times)}",
    ]
    if len(settings) > 1:
        lines.append("  - **more than one setting in this sample: read the rows apart**")
    return lines


def _verdict(faithful: bool | None) -> str:
    return "?" if faithful is None else ("faithful" if faithful else "NOT faithful")


def _case(number: int, row: Row, read: Reader | None) -> list[str]:
    lines = [
        f"### {number}. trace `{row.trace_id}` ({row.at.astimezone(UTC):%Y-%m-%d %H:%M} UTC)",
        "",
        f"- deployed: **{_verdict(row.deployed_faithful)}**, {row.deployed_unsupported} claims"
        f" listed, {row.deployed_latency_ms / 1000:.1f} s",
        f"- shadow: **{_verdict(row.shadow_faithful)}**, {row.shadow_unsupported or 0} claims"
        f" listed, {(row.shadow_latency_ms or 0) / 1000:.1f} s",
    ]
    if read is None:
        return [*lines, ""]
    found = read(row.trace_id)
    if found.problem:
        return [*lines, f"- {found.problem}", ""]
    lines += ["", "**Question**", "", found.question, "", "**Answer**", "", found.answer, ""]
    for name, claims, reason in (
        ("Deployed judge", found.deployed_claims, found.deployed_reason),
        ("Shadow judge", found.shadow_claims, found.shadow_reason),
    ):
        lines.append(f"**{name} quoted as unsupported**")
        lines.append("")
        lines += [f"- {claim}" for claim in claims] or ["- (nothing)"]
        if reason:
            lines += ["", f"Its reason: {reason}"]
        lines.append("")
    same = same_claim(found.deployed_claims, found.shadow_claims)
    if same is not None:
        lines += [f"Both quoted a claim in common: **{'yes' if same else 'no'}**", ""]
    lines += [
        "Review: [ ] faithful  [ ] not faithful. The unsupported claim, if any, was named"
        " by: [ ] deployed  [ ] shadow  [ ] neither",
        "",
    ]
    return lines


def report(
    rows: list[Row],
    *,
    limit: int | None = None,
    budget_usd: float | None = None,
    agreements: int = 10,
    seed: int = 0,
    read: Reader | None = None,
    skips: dict[str, int] | None = None,
) -> list[str]:
    """The review as Markdown lines. `read` fetches a case's words; None: numbers only."""
    s = Summary(rows)
    charged = sum(float(r.charged_usd) for r in rows)
    of_limit = f" of {limit}" if limit else ""
    of_budget = f" of ${budget_usd:.2f}" if budget_usd else ""
    lines = ["# Shadow judge review", "", CAVEAT, ""]
    skips = skips or {}
    skipped = {reason: skips.get(reason, 0) for reason in SKIPPED if skips.get(reason)}
    not_eligible = sum(skips.get(reason, 0) for reason in NOT_ELIGIBLE)
    why = ", ".join(f"{SKIP_WORDS[r]}: {n}" for r, n in skipped.items())
    lines += [
        "## Totals",
        "",
        "Eligible: one of the maintainer's own answers that the deployed judge completed"
        " while the shadow was on.",
        "",
        f"- eligible answers: {len(rows) + sum(skipped.values())}",
        f"- sampled (a shadow call was made or begun): {len(rows)}",
        f"- skipped: {sum(skipped.values())}" + (f" ({why})" if why else ""),
        f"- judged answers that were not the maintainer's, never eligible: {not_eligible}",
        f"- answers both judges read: {len(s.compared)}{of_limit}",
        f"- shadow calls that failed: {len(s.failed)} (charged at the estimate, place given back)",
        f"- unfinished (cut off; they keep a place and an estimate): {len(s.unfinished)}",
        f"- held against the budget: ${charged:.4f}{of_budget}",
    ]
    hashes = {r.prompt_hash for r in rows}
    if len(hashes) > 1:
        lines.append(f"- **{len(hashes)} different prompt versions in this sample**")
    if not s.compared:
        return [*lines, "", "Nothing to review yet."]
    agree = len(s.cell(True, True)) + len(s.cell(False, False))
    lines += [
        f"- the judges agree on {agree} of {len(s.compared)} ({agree / len(s.compared):.0%})",
        "",
        "| | shadow: faithful | shadow: not faithful |",
        "|---|---|---|",
        f"| deployed: faithful | {len(s.cell(True, True))} | {len(s.cell(True, False))} |",
        f"| deployed: not faithful | {len(s.cell(False, True))} | {len(s.cell(False, False))} |",
        "",
        "## The two judges",
        "",
        *_judge_lines("Deployed", s.compared, "deployed"),
        *_judge_lines("Shadow", s.compared, "shadow"),
        "",
    ]
    newest = lambda found: sorted(found, key=lambda r: r.id, reverse=True)  # noqa: E731
    both_faithful = s.cell(True, True)
    picked = random.Random(seed).sample(both_faithful, min(agreements, len(both_faithful)))
    sections = [
        ("Only the shadow judge says not faithful", newest(s.cell(True, False)), "all"),
        ("Only the deployed judge says not faithful", newest(s.cell(False, True)), "all"),
        ("Both say not faithful", newest(s.cell(False, False)), "all"),
        (
            "Both say faithful",
            sorted(picked, key=lambda r: r.id),
            f"a random {len(picked)} of {len(both_faithful)}, seed {seed}",
        ),
    ]
    number = 0
    for title, found, which in sections:
        lines += [f"## {title} ({which}: {len(found)})", ""]
        if not found:
            lines += ["None.", ""]
        for row in found:
            number += 1
            lines += _case(number, row, read)
    return lines
