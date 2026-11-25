"""The HTTP API with a fake answering service: auth, validation, response shape, failures."""

from __future__ import annotations

from datetime import date
from pathlib import Path

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

    def answer(self, question: str, as_of: date, history: list[str], progress=None) -> Answer:
        self.asked.append((question, as_of))
        self.histories.append(history)
        for stage in ("understanding",) * bool(history) + ("searching", "writing"):
            if progress:
                progress(stage)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer or Answer(kind="answered", text="Svar [S1].", citations=CITATIONS)

    def healthy(self) -> bool:
        return self._healthy

    def save_feedback(self, case) -> None:
        from permitnav.feedback import FeedbackRefused, identifiers_in

        if identifiers_in(case.question, case.answer, case.comment):
            raise FeedbackRefused("The text contains an email address. Remove it and save again.")
        self.feedback = {**getattr(self, "feedback", {}), case.withdraw_code: case}

    def withdraw_feedback(self, code: str) -> bool:
        return getattr(self, "feedback", {}).pop(code, None) is not None

    def source(self, version_id: int) -> dict[str, str] | None:
        if version_id != 7:
            return None
        return {
            "unit_id": "sfs:2005:716/6kap/2§",
            "heading": "6 kap. 2 §",
            "authority": "law",
            "url": "https://x",
            "dates": "applies from 2026-06-01",
            "text": "Arbetstillstånd får ges (older wording)",
        }


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
        "version_id": None,
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


# --- phase 6: the chat UI's endpoints ----------------------------------------------------


def test_a_token_can_be_checked_without_asking_a_question() -> None:
    service = FakeService()
    with client(service) as c:
        assert c.get("/auth-check").status_code == 401
        assert c.get("/auth-check", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/auth-check", headers=AUTH).json() == {"ok": True}
    assert service.asked == []  # no question, no model call


def test_a_cited_source_is_returned_by_its_exact_version() -> None:
    """An older wording shares its unit with the current one: the citation carries the
    version, and the panel asks for it in a POST body (never a URL an access log keeps)."""
    with client() as c:
        assert c.post("/source", json={"version_id": 7}).status_code == 401
        body = c.post("/source", json={"version_id": 7}, headers=AUTH).json()
        assert body["text"] == "Arbetstillstånd får ges (older wording)"
        assert c.post("/source", json={"version_id": 8}, headers=AUTH).status_code == 404
        for bad in ({"version_id": 0}, {"version_id": "x"}, {}):
            assert c.post("/source", json=bad, headers=AUTH).status_code == 422
        assert c.get("/source", headers=AUTH).status_code in (404, 405)


def test_citations_carry_the_version_they_quote() -> None:
    cited = Answer(kind="answered", text="Svar [S1].", citations=[
        Citation("S1", "sfs:2005:716/6kap/2§", "6 kap. 2 §", "https://x", "law", "d", version_id=42)
    ])  # fmt: skip
    with client(FakeService(cited)) as c:
        body = c.post("/ask", json={"question": "Lönekrav?"}, headers=AUTH).json()
    assert body["citations"][0]["version_id"] == 42


def test_the_built_ui_is_served_and_the_api_still_wins(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<title>PermitNav</title>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    with TestClient(create_app(FakeService(), token=TOKEN, web_dir=str(tmp_path))) as c:
        assert "PermitNav" in c.get("/").text
        assert c.get("/assets/app.js").status_code == 200
        assert c.get("/health").json() == {"status": "ok"}
        assert c.post("/ask", json={"question": "q?"}).status_code == 401  # still protected
        assert c.get("/no-such-page").status_code == 404  # no catch-all
        moved = c.post("/ask/", json={"question": "q?"}, follow_redirects=False)
        assert moved.status_code == 307  # the API's trailing-slash redirect still works
    with TestClient(create_app(FakeService(), token=TOKEN, web_dir=str(tmp_path / "none"))) as c:
        assert c.get("/").status_code == 404  # no build: API only


# --- phase 6 slice 2: progress as server-sent events -------------------------------------


def events(body: str) -> list[tuple[str, dict]]:
    import json

    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_the_stream_sends_each_stage_then_the_answer() -> None:
    with client() as c:
        body = {"question": "Och lönen?", "history": ["Jag har arbetstillstånd."]}
        r = c.post("/ask/stream", json=body, headers=AUTH)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "no-transform" in r.headers["cache-control"]  # proxies must not buffer or compress it
    got = events(r.text)
    assert [d["stage"] for e, d in got if e == "stage"] == ["understanding", "searching", "writing"]
    kind, answer = got[-1]
    assert kind == "answer" and answer["text"] == "Svar [S1]." and answer["citations"]


def test_a_failure_arrives_as_an_error_event_and_auth_comes_first() -> None:
    with client(FakeService(LLMError("OpenRouter HTTP 402: billing details"))) as c:
        assert c.post("/ask/stream", json={"question": "q?"}).status_code == 401
        assert c.post("/ask/stream", json={"question": ""}, headers=AUTH).status_code == 422
        got = events(c.post("/ask/stream", json={"question": "q?"}, headers=AUTH).text)
    assert got[-1] == ("error", {"status": 502, "message": "model call failed"})  # no billing text


def test_a_streamed_question_is_one_trace(spans) -> None:
    with client() as c:
        c.post("/ask/stream", json={"question": "Lönekrav?"}, headers=AUTH)
    finished = spans.get_finished_spans()
    (root,) = [s for s in finished if s.parent is None]
    (ask,) = [s for s in finished if s.name == "ask"]
    assert root.name.startswith("POST") and ask.context.trace_id == root.context.trace_id
    assert ask.attributes["permitnav.streamed"] is True


class SlowService(FakeService):
    """Stages a little apart, recording how far it got."""

    def __init__(self) -> None:
        super().__init__()
        self.reached: list[str] = []
        self.stopped = False

    def answer(self, question, as_of, history, progress=None):
        import time

        import anyio

        from permitnav.api import ClientGone

        try:
            for stage in ("searching", "writing"):
                if progress:
                    progress(stage)
                self.reached.append(stage)
                time.sleep(0.8)  # a model call's worth of time
        except (ClientGone, anyio.BrokenResourceError, anyio.ClosedResourceError):
            self.stopped = True  # told the client left, or nobody listens to its stages
            raise
        return super().answer(question, as_of, history)


def test_a_client_that_goes_away_stops_the_answer_at_the_next_stage() -> None:
    """Deleting a conversation aborts the request; the server must not keep paying for
    model calls nobody will read. A real server (uvicorn) and a real connection: the test
    client never reports a disconnect."""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    service = SlowService()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(service, token=TOKEN), port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port}/ask/stream"
        with httpx.stream("POST", url, json={"question": "q?"}, headers=AUTH, timeout=5) as r:
            assert next(r.iter_lines()) == "event: stage"
        deadline = time.time() + 3
        while not service.stopped and time.time() < deadline:
            time.sleep(0.05)
        assert service.stopped and service.reached == ["searching"]  # never started writing
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# --- phase 6 slice 3: feedback with consent -----------------------------------------------

CODE = "c" * 43
CASE = {
    "consent": True, "withdraw_code": CODE, "question": "Lönekrav?", "answer": "Svar [S1].",
    "comment": "Fel belopp.", "kind": "answered", "as_of": "2026-11-25",
    "cited": ["sfs:2005:716/6kap/2§"],
}  # fmt: skip


def test_feedback_is_saved_only_with_the_explicit_consent() -> None:
    service = FakeService()
    with client(service) as c:
        assert c.post("/feedback", json=CASE).status_code == 401
        assert c.post("/feedback", json=CASE, headers=AUTH).json() == {"saved": True}
        for consent in (False, None, "yes", 1, "true"):
            refused = c.post("/feedback", json={**CASE, "consent": consent}, headers=AUTH)
            assert refused.status_code == 422, consent
        for missing in ("consent", "withdraw_code"):
            without = {k: v for k, v in CASE.items() if k != missing}
            assert c.post("/feedback", json=without, headers=AUTH).status_code == 422
        weak = {**CASE, "withdraw_code": "short"}
        assert c.post("/feedback", json=weak, headers=AUTH).status_code == 422
    assert list(service.feedback) == [CODE] and service.feedback[CODE].question == "Lönekrav?"


def test_identifying_text_is_refused_with_a_message_for_the_person() -> None:
    with client() as c:
        r = c.post("/feedback", json={**CASE, "comment": "mejla anna@example.com"}, headers=AUTH)
    assert r.status_code == 422 and "email address" in r.json()["detail"]


def test_a_submission_is_withdrawn_with_the_code_it_was_saved_under() -> None:
    with client() as c:
        c.post("/feedback", json=CASE, headers=AUTH)
        mine = {"withdraw_code": CODE}
        assert c.post("/feedback/withdraw", json=mine).status_code == 401
        other = {"withdraw_code": "d" * 43}
        assert c.post("/feedback/withdraw", json=other, headers=AUTH).status_code == 404
        assert c.post("/feedback/withdraw", json=mine, headers=AUTH).json() == {"withdrawn": True}
        assert c.post("/feedback/withdraw", json=mine, headers=AUTH).status_code == 404


def test_feedback_leaves_no_trace_and_no_log_line(spans, caplog) -> None:
    import logging

    with caplog.at_level(logging.DEBUG), client() as c:
        c.post("/feedback", json={**CASE, "question": "En ovanlig formulering xyzzy"}, headers=AUTH)
        c.post("/feedback/withdraw", json={"withdraw_code": CODE}, headers=AUTH)
    assert spans.get_finished_spans() == ()  # the sampler keeps only questions
    assert "xyzzy" not in caplog.text
    ours = [r for r in caplog.records if r.name.startswith(("permitnav", "uvicorn"))]
    assert not any("feedback" in r.getMessage().lower() for r in ours)  # the client's own aside


def test_the_feedback_endpoints_are_kept_out_of_the_access_log() -> None:
    import logging

    with client():
        access = logging.getLogger("uvicorn.access")

        def kept(path: str) -> bool:
            args = ("203.0.113.9:5000", "POST", path, "1.1", 200)
            record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s", args, None)
            return access.filter(record) is not False

        assert not kept("/feedback") and not kept("/feedback/withdraw")
        assert not kept("/feedback/") and not kept("/feedback?x=1") and kept("/feedbacks")
        assert kept("/ask/stream") and kept("/health")
    caddy = (Path(__file__).parent.parent / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "log_skip @feedback\n" in caddy and "@feedback path /feedback /feedback/*" in caddy
    # a failed request too (Caddy writes errors to a second log unless they are handled)
    assert "handle_errors {" in caddy and "log_skip @feedback_failed" in caddy
