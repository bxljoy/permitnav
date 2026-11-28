"""Version names (permitnav.versions): short hex names from settings and prompt texts."""

from __future__ import annotations

import pytest

from permitnav import versions


def test_the_release_is_the_commit_the_image_was_built_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERMITNAV_VERSION", "0AB9798C1D2E3F4A5B6C7D8E9F0A1B2C3D4E5F6A")
    assert versions.release() == "0ab9798c1d2e"
    for unusable in ("unknown", "not a commit", "v1.2"):  # never passed on as it stands
        monkeypatch.setenv("PERMITNAV_VERSION", unusable)
        assert versions.release() == "unknown"
    monkeypatch.delenv("PERMITNAV_VERSION")
    assert versions.valid(versions.release())  # the checkout's commit, in development
    assert not versions.valid("0ab9798c1d2e\n") and not versions.valid("0ab9798 c1d2e")


def test_a_version_changes_with_what_it_names(monkeypatch: pytest.MonkeyPatch) -> None:
    model = "anthropic/claude-haiku-5.5"
    haiku = versions.answer_version(model)
    assert versions.valid(haiku) and haiku == versions.answer_version(model, model)
    assert versions.answer_version("another/model") != haiku
    assert versions.answer_version(model, "another/translator") != haiku  # it shapes the search
    for same in ("0", "false", ""):  # spellings of "off" are one configuration
        monkeypatch.setenv("ANSWER_STRUCTURED", same)
        assert versions.answer_version(model) == haiku
    monkeypatch.setenv("ANSWER_STRUCTURED", "1")
    on = versions.answer_version(model)
    monkeypatch.setenv("ANSWER_STRUCTURED", "true")
    assert on != haiku and versions.answer_version(model) == on  # a setting counts, once
    monkeypatch.delenv("ANSWER_STRUCTURED")
    monkeypatch.setenv("ANSWER_REASONING_EFFORT", "low")
    assert versions.answer_version(model) != haiku
    lite = versions.judge_version("google/gemini-3.5-flash-lite", "low")
    assert versions.valid(lite)
    assert versions.judge_version("google/gemini-3.5-flash-lite", None) != lite
    assert versions.judge_version("google/gemini-3.5-flash", "low") != lite
    monkeypatch.setattr(versions, "prompt_hash", lambda name: "changed")
    assert versions.judge_version("google/gemini-3.5-flash-lite", "low") != lite  # the prompt
