"""The HTTP API with a fake answering service: auth, validation, response shape, failures."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from permitnav.answering.answerer import Answer, Citation
from permitnav.api import MAX_QUESTION_CHARS, NOTICE, create_app
from permitnav.llm.port import LLMError

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeService:
    def __init__(self, answer: Answer | Exception | None = None, healthy: bool = True) -> None:
        self._answer = answer
        self._healthy = healthy
        self.asked: list[tuple[str, date]] = []
        self.histories: list[list[str]] = []

    def answer(self, question: str, as_of: date, history: list[str]) -> Answer:
        self.asked.append((question, as_of))
        self.histories.append(history)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer or Answer(kind="answered", text="Svar [S1].", citations=CITATIONS)

    def healthy(self) -> bool:
        return self._healthy


CITATIONS = [
    Citation(
        "S1",
        "sfs:2005:716/6kap/2§",
        "6 kap. 2 §",
        "https://x",
        "law",
        "applies from 2026-06-01",
        "lönen",
        "the salary",
    ),
    Citation("S2", "mig:2017:24", "MIG 2017:24", "https://y", "ruling", "decided on 2017-12-13"),
    Citation("S3", "sfs:2005:716/6kap/3§", "6 kap. 3 §", "https://z", "law", "applies"),
]


def client(service: FakeService | None = None, token: str | None = TOKEN) -> TestClient:
    return TestClient(create_app(service or FakeService(), token=token))


def test_ask_needs_the_token() -> None:
    with client() as c:
        assert c.post("/ask", json={"question": "Lönekrav?"}).status_code == 401
        wrong = {"Authorization": "Bearer nope"}
        assert c.post("/ask", json={"question": "Lönekrav?"}, headers=wrong).status_code == 401
        assert c.post("/ask", json={"question": "Lönekrav?"}, headers=AUTH).status_code == 200


def test_ask_returns_the_answer_with_citations_credits_and_notice() -> None:
    service = FakeService()
    with client(service) as c:
        body = c.post(
            "/ask", json={"question": "Lönekrav?", "as_of": "2026-11-01"}, headers=AUTH
        ).json()
    assert service.asked == [("Lönekrav?", date(2026, 11, 1))]
    assert body["kind"] == "answered" and body["text"] == "Svar [S1]."
    assert body["as_of"] == "2026-11-01" and body["notice"] == NOTICE
    assert body["citations"][0] == {
        "marker": "S1",
        "unit_id": "sfs:2005:716/6kap/2§",
        "heading": "6 kap. 2 §",
        "url": "https://x",
        "authority": "law",
        "dates": "applies from 2026-06-01",
        "quote": "lönen",
        "translation": "the salary",
    }
    assert body["credits"] == ["Källa: Sveriges riksdag", "Källa: Sveriges Domstolar"]  # once each


def test_as_of_defaults_to_today() -> None:
    service = FakeService()
    with client(service) as c:
        c.post("/ask", json={"question": "Lönekrav?"}, headers=AUTH)
    assert service.asked[0][1] == date.today()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "x" * (MAX_QUESTION_CHARS + 1)},
        {"question": "q", "as_of": "nope"},
    ],
)
def test_bad_requests_are_rejected_before_any_model_call(payload: dict) -> None:
    service = FakeService()
    with client(service) as c:
        assert c.post("/ask", json=payload, headers=AUTH).status_code == 422
    assert service.asked == []


def test_a_failed_model_call_is_a_502() -> None:
    with client(FakeService(LLMError("upstream timeout"))) as c:
        response = c.post("/ask", json={"question": "Lönekrav?"}, headers=AUTH)
    assert response.status_code == 502


def test_health_needs_no_token_and_reports_the_database() -> None:
    with client(FakeService(healthy=True)) as c:
        assert c.get("/health").json() == {"status": "ok"}
    with client(FakeService(healthy=False)) as c:
        assert c.get("/health").status_code == 503


def test_the_app_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("PERMITNAV_ALLOW_NO_AUTH", raising=False)
    with pytest.raises(RuntimeError, match="API_TOKEN"), client(token=None):
        pass


def test_local_use_without_a_token_must_be_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("PERMITNAV_ALLOW_NO_AUTH", "1")
    with client(token=None) as c:
        assert c.post("/ask", json={"question": "Lönekrav?"}).status_code == 200


def test_the_token_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_TOKEN", "from-env")
    with client(token=None) as c:
        assert c.post("/ask", json={"question": "q"}, headers=AUTH).status_code == 401
        ok = {"Authorization": "Bearer from-env"}
        assert c.post("/ask", json={"question": "q"}, headers=ok).status_code == 200


def test_the_bearer_scheme_is_required_and_case_insensitive() -> None:
    with client() as c:
        ask = {"question": "Lönekrav?"}
        assert c.post("/ask", json=ask, headers={"Authorization": TOKEN}).status_code == 401
        lower = {"Authorization": f"bearer {TOKEN}"}
        assert c.post("/ask", json=ask, headers=lower).status_code == 200


def test_a_whitespace_question_is_rejected_and_questions_are_trimmed() -> None:
    service = FakeService()
    with client(service) as c:
        assert c.post("/ask", json={"question": "   "}, headers=AUTH).status_code == 422
        c.post("/ask", json={"question": "  Lönekrav?  "}, headers=AUTH)
    assert [q for q, _ in service.asked] == ["Lönekrav?"]


def test_provider_errors_are_not_passed_to_the_client() -> None:
    error = LLMError("OpenRouter HTTP 402: insufficient credits on account acct_123")
    with client(FakeService(error)) as c:
        response = c.post("/ask", json={"question": "Lönekrav?"}, headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": "model call failed"}


def test_an_unreadable_reply_is_a_502_not_a_success() -> None:
    with client(FakeService(Answer(kind="failed", text=""))) as c:
        response = c.post("/ask", json={"question": "Lönekrav?"}, headers=AUTH)
    assert response.status_code == 502


# --- phase 5: conversation memory (history) ---------------------------------------------


def test_earlier_questions_reach_the_service_and_none_means_none() -> None:
    service = FakeService()
    with client(service) as c:
        c.post("/ask", json={"question": "Och min make?"}, headers=AUTH)
        c.post(
            "/ask",
            json={"question": "Och min make?", "history": [" Jag har arbetstillstånd. "]},
            headers=AUTH,
        )
    assert service.histories == [[], ["Jag har arbetstillstånd."]]  # stripped like the question


def test_a_whole_conversation_is_accepted_up_to_a_bound() -> None:
    """A client may send the whole conversation; intake uses only the last few."""
    from permitnav.agent.intake import MAX_HISTORY, MAX_HISTORY_ACCEPTED

    service = FakeService()
    with client(service) as c:

        def status(history) -> int:
            body = {"question": "q?", "history": history}
            return c.post("/ask", json=body, headers=AUTH).status_code

        assert status(["q"] * (MAX_HISTORY + 1)) == 200  # more than intake reads: still fine
        assert status(["q"] * MAX_HISTORY_ACCEPTED) == 200
        assert status(["q"] * (MAX_HISTORY_ACCEPTED + 1)) == 422
        assert status(["x" * (MAX_QUESTION_CHARS + 1)]) == 422
        assert status([""]) == 422
        assert status("one string") == 422


def test_how_a_follow_on_was_understood_is_returned_to_the_asker() -> None:
    from permitnav.agent.intake import Intake

    rewritten = Answer(kind="answered", text="Svar [S1].", citations=CITATIONS)
    rewritten.intake = Intake(
        asked="Och min make?", question="Jag har arbetstillstånd. Kan min make komma hit?",
        rewritten=True, situation={"permit": "arbetstillstånd", "family": "make"},
    )  # fmt: skip
    with client(FakeService(rewritten)) as c:
        body = c.post(
            "/ask", json={"question": "Och min make?", "history": ["x"]}, headers=AUTH
        ).json()
    assert body["understood_as"] == "Jag har arbetstillstånd. Kan min make komma hit?"
    assert body["situation"] == {"permit": "arbetstillstånd", "family": "make"}
    with client() as c:  # a single question: nothing to show
        body = c.post("/ask", json={"question": "Lönekrav?"}, headers=AUTH).json()
    assert body["understood_as"] is None and body["situation"] == {}


def test_traces_record_how_many_earlier_questions_never_their_text(spans) -> None:
    secret = "Mitt namn är Anna och jag jobbar på Volvo."
    with client() as c:
        c.post("/ask", json={"question": "Och lönen?", "history": [secret]}, headers=AUTH)
    (ask,) = [s for s in spans.get_finished_spans() if s.name == "ask"]
    assert ask.attributes["permitnav.history_turns"] == 1
    for span in spans.get_finished_spans():
        assert all(secret not in str(v) for v in span.attributes.values())
