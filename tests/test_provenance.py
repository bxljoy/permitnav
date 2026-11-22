"""Eval reports name the version that produced them."""

from __future__ import annotations

import hashlib
import subprocess

from permitnav.evals.provenance import PROMPTS, git_version, prompt_hash, provenance
from permitnav.prompting import load_prompt
from permitnav.retrieval.retriever import RetrievalConfig


def test_prompt_hash_changes_with_the_prompt_text():
    expected = hashlib.sha256(load_prompt("answer.md").encode("utf-8")).hexdigest()[:12]
    assert prompt_hash("answer.md") == expected
    assert prompt_hash("answer.md") != prompt_hash("translate.md")


def test_provenance_lists_every_prompt_model_and_setting():
    config = RetrievalConfig(rerank=True, keyword_weight=0.5)
    lines = provenance(config, {"answer": "a-model", "judge": None, "embedder": "e-model"})
    text = "\n".join(lines)
    assert lines[0].startswith("version: ")
    for name in PROMPTS:
        assert f"{name} {prompt_hash(name)}" in text
    assert "answer=a-model" in text and "embedder=e-model" in text
    assert "judge=" not in text  # a model not in use is left out
    assert "rerank=True" in text and "keyword_weight=0.5" in text
    assert "rerank_depth=20" in text  # defaults too: the full config, not only the changes


def test_git_version_marks_uncommitted_changes(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "f.txt").write_text("one")
    git("add", "f.txt")
    git("commit", "-q", "-m", "first")
    clean = git_version(tmp_path)
    assert len(clean) == 12 and "uncommitted" not in clean
    (tmp_path / "f.txt").write_text("two")
    assert git_version(tmp_path) == f"{clean} (uncommitted changes)"
    (tmp_path / "new.txt").write_text("untracked files do not count")
    (tmp_path / "f.txt").write_text("one")
    assert git_version(tmp_path) == clean


def test_git_version_outside_a_checkout(tmp_path):
    assert git_version(tmp_path) == "unknown"
