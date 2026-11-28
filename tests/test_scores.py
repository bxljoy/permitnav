"""Scores sent to Langfuse (permitnav.scores): the object sent, and that sending can
neither delay nor fail an answer."""

from __future__ import annotations

import threading

import pytest

from permitnav import scores
from permitnav.scores import Scores

TRACE = "0123456789abcdef0123456789abcdef"


def sender(post) -> Scores:
    return Scores("https://langfuse.example/", "pk", "sk", "production", post=post)


def test_a_score_is_named_by_its_trace_and_typed_by_its_value() -> None:
    kind = scores.body(TRACE, "kind", "answered", "production")
    assert kind == {
        "id": f"{TRACE}-kind",  # the same score sent twice replaces itself
        "traceId": TRACE,
        "name": "kind",
        "value": "answered",
        "dataType": "CATEGORICAL",
        "environment": "production",
    }
    assert scores.body(TRACE, "faithful", False, "p")["dataType"] == "BOOLEAN"
    assert scores.body(TRACE, "faithful", False, "p")["value"] == 0
    assert scores.body(TRACE, "helpful", True, "p")["value"] == 1
    assert scores.body(TRACE, "sources", 8.0, "p")["dataType"] == "NUMERIC"


def test_scores_are_posted_in_the_background_in_order() -> None:
    sent: list[dict] = []
    s = sender(sent.append)
    s.add(TRACE, "kind", "answered")
    s.add(TRACE, "second_call", False)
    assert s.flush(2)
    assert [(x["name"], x["value"]) for x in sent] == [("kind", "answered"), ("second_call", 0)]
    assert s._url == "https://langfuse.example/api/public/scores"


def test_an_untraced_answer_gets_no_score() -> None:
    sent: list[dict] = []
    s = sender(sent.append)
    s.add(None, "kind", "answered")
    s.add("", "kind", "answered")
    assert s.flush(1) and sent == [] and s._thread is None


def test_a_failing_langfuse_drops_scores_and_keeps_going(caplog) -> None:
    sent: list[dict] = []

    def post(score: dict) -> None:
        if score["name"] == "kind":
            raise OSError("connection refused to https://langfuse.example with key sk")
        sent.append(score)

    s = sender(post)
    s.add(TRACE, "kind", "answered")
    s.add(TRACE, "second_call", True)
    assert s.flush(2)
    assert s.dropped == 1 and [x["name"] for x in sent] == ["second_call"]
    assert "OSError" in caplog.text and "connection refused" not in caplog.text  # type only


def test_a_stuck_langfuse_never_blocks_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scores, "MAX_WAITING", 3)
    release = threading.Event()
    s = sender(lambda _: release.wait(5))
    for _ in range(10):  # returns at once each time: the queue fills, the rest are dropped
        s.add(TRACE, "kind", "answered")
    assert s.dropped >= 6
    assert not s.flush(0.05)  # still stuck: flush gives up at its timeout
    release.set()
    assert s.flush(2)


def test_no_sender_without_the_langfuse_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert scores.from_environment() is None
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("PERMITNAV_ENV", "production")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    made = scores.from_environment()
    assert made is not None and made._url == "https://cloud.langfuse.com/api/public/scores"
    assert made._environment == "production"


def test_a_score_can_name_the_version_that_made_it() -> None:
    made = scores.body(TRACE, "faithful", True, "production", {"judge_version": "abcdef012345"})
    assert made["metadata"] == {"judge_version": "abcdef012345"}
    assert "metadata" not in scores.body(TRACE, "faithful", True, "production")
    assert "comment" not in made  # nothing here takes free text
