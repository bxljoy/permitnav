"""What an eval report was produced by: commit, prompt versions, models and retrieval config.

A reported number (above all a hold-out result) is only meaningful for the version that
produced it; printing it with the report makes the run reproducible and comparable.
"""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
from pathlib import Path

from permitnav.prompting import load_prompt
from permitnav.retrieval.retriever import RetrievalConfig

PROMPTS = ("answer.md", "translate.md", "judge_correctness.md", "judge_faithfulness.md")
REPO = Path(__file__).resolve().parents[3]


def prompt_hash(name: str) -> str:
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()[:12]


def git_version(repo: Path = REPO) -> str:
    """The commit, marked when tracked files differ from it; "unknown" outside a checkout."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        commit = git("rev-parse", "--short=12", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit} (uncommitted changes)" if dirty else commit


def provenance(config: RetrievalConfig, models: dict[str, str | None]) -> list[str]:
    """Report lines: the version, the prompts' hashes, the models in use, the full config."""
    used = ", ".join(f"{role}={name}" for role, name in models.items() if name)
    settings = ", ".join(f"{f.name}={getattr(config, f.name)}" for f in dataclasses.fields(config))
    return [
        f"version: {git_version()}",
        "prompts: " + ", ".join(f"{name} {prompt_hash(name)}" for name in PROMPTS),
        f"models: {used}",
        f"retrieval: {settings}",
    ]
