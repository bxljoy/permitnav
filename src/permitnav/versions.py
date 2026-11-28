"""Which version produced a trace, a score or a day's counts (online evals).

A rate that moves means little unless it can be set against what changed: the deployed
code, the answer model and its prompts, the judge and its prompt. Each is a short hex name
made from settings and prompt texts in the code, never from anything a person wrote.
"""

from __future__ import annotations

import hashlib
import os
import re

from permitnav.evals.provenance import git_version, prompt_hash

UNKNOWN = "unknown"
NAME = re.compile(r"(?:[0-9a-f]{7,40}|unknown)")  # what a version name is: `valid`


def valid(name: str) -> bool:
    """Whether a value is a version name and nothing more (not even a trailing newline)."""
    return NAME.fullmatch(name) is not None


def _name(*parts: str | None) -> str:
    return hashlib.sha256("\x1f".join(p or "" for p in parts).encode()).hexdigest()[:12]


def release() -> str:
    """The deployed commit: PERMITNAV_VERSION (set when the image is built), or the
    checkout's commit in development; "unknown" when neither can be told."""
    value = os.environ.get("PERMITNAV_VERSION", "").strip().lower()
    if not value:
        try:
            value = git_version().split()[0].lower()
        except Exception:
            value = ""
    value = value[:12]
    return value if valid(value) and value != UNKNOWN else UNKNOWN


def answer_version(model: str, translate_model: str | None = None) -> str:
    """Everything set outside the code that shapes an answer: the answer model, the model
    that translates the question for the search, the answer settings as the service reads
    them (so two spellings of one setting are one version), and the prompts."""
    from permitnav.answering.answerer import answer_reasoning_effort, answer_structured

    return _name(
        model,
        translate_model or model,
        answer_reasoning_effort() or "",
        "structured" if answer_structured() else "",
        prompt_hash("answer.md"),
        prompt_hash("intake.md"),
        prompt_hash("translate.md"),
    )


def judge_version(model: str, reasoning_effort: str | None) -> str:
    """The live judge: its model, reasoning effort and faithfulness prompt."""
    return _name(model, reasoning_effort, prompt_hash("judge_faithfulness.md"))
