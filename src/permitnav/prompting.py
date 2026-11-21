"""Prompts ship inside the package (permitnav/prompts), versioned in git (D15)."""

from __future__ import annotations

from importlib import resources


def load_prompt(name: str = "answer.md") -> str:
    return resources.files("permitnav").joinpath("prompts", name).read_text(encoding="utf-8")
