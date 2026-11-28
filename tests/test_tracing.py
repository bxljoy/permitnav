"""Tracing: span structure, model-call attributes, exporters, and the privacy rule
(metadata mode never records a question, answer, prompt or source text)."""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

import psycopg
import pytest
from opentelemetry import trace

from permitnav import tracing
from permitnav.llm.fake import ScriptedLLM
from permitnav.llm.port import CompletionRequest

QUESTION = "lönen medianlönen procent lönekravet"
ANSWER = "At least 90 percent of the median salary [S1]."


def request() -> CompletionRequest:
    return CompletionRequest(
        model="m", system="PROMPT TEXT", messages=[{"role": "user", "content": QUESTION}]
    )


def test_a_model_call_is_a_generation_with_model_tokens_and_cost(spans) -> None:
    tracing.complete(ScriptedLLM("reply"), request(), "generate")
    (span,) = spans.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert span.name == "generate"
    assert attrs["langfuse.observation.type"] == "generation"
    assert attrs["gen_ai.request.model"] == "m"
    assert (attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"]) == (100, 50)
    assert json.loads(str(attrs["langfuse.observation.cost_details"])) == {"total": 0.0}
    assert "langfuse.observation.input" not in attrs  # metadata mode
    assert "langfuse.observation.output" not in attrs


def test_full_content_mode_records_the_texts(spans) -> None:
    tracing.set_content_mode("full")
    tracing.complete(ScriptedLLM("reply"), request(), "generate")
    attrs = dict(spans.get_finished_spans()[0].attributes or {})
    assert QUESTION in str(attrs["langfuse.observation.input"])
    assert json.loads(str(attrs["langfuse.observation.output"])) == "reply"


def test_a_failing_call_marks_the_span_as_an_error(spans) -> None:
    def fail(_: CompletionRequest) -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        tracing.complete(ScriptedLLM(fail), request(), "generate")
    (span,) = spans.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert (span.attributes or {})["langfuse.observation.level"] == "ERROR"


def test_unknown_content_modes_are_rejected(spans) -> None:
    with pytest.raises(ValueError, match="trace content"):
        tracing.set_content_mode("everything")


def test_no_exporter_without_keys_or_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "PERMITNAV_TRACE_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert tracing._exporters() == []


def test_langfuse_exporter_uses_the_eu_endpoint_and_basic_auth(monkeypatch) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-2")
    monkeypatch.delenv("PERMITNAV_TRACE_FILE", raising=False)
    (exporter,) = tracing._exporters()
    assert isinstance(exporter, OTLPSpanExporter)
    assert tracing.langfuse_endpoint(tracing.LANGFUSE_EU) == (
        "https://cloud.langfuse.com/api/public/otel/v1/traces"
    )
    assert tracing.langfuse_headers("pk-lf-1", "sk-lf-2") == {
        "Authorization": "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-2").decode(),
        "x-langfuse-ingestion-version": "4",
    }


def test_the_file_exporter_writes_one_json_line_per_span(tmp_path: Path, spans) -> None:
    with tracing.span("retrieve", "retriever", **{"permitnav.k": 8}):
        pass
    path = tmp_path / "traces.jsonl"
    tracing.JsonLinesExporter(path).export(spans.get_finished_spans())
    (line,) = path.read_text().splitlines()
    record = json.loads(line)
    assert record["name"] == "retrieve" and record["attributes"]["permitnav.k"] == 8


@pytest.mark.db
def test_an_answer_traces_retrieval_and_generation_and_keeps_no_text(
    conn: psycopg.Connection, spans
) -> None:
    from permitnav.answering.answerer import AnswerConfig, Answerer
    from permitnav.corpus.indexing import build_chunks, embed_chunks
    from permitnav.retrieval.retriever import Retriever
    from tests.test_ingest import FIXTURE, ingest
    from tests.test_retriever import FAKE

    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)
    llm = ScriptedLLM(json.dumps({"kind": "answered", "answer": ANSWER}))
    answerer = Answerer(conn, Retriever(conn, FAKE), llm, AnswerConfig(model="test-model", k=3))
    answerer.answer(QUESTION, as_of=date(2026, 11, 20))

    finished = {s.name: s for s in spans.get_finished_spans()}
    assert {"answer", "retrieve", "embed", "generate"} <= finished.keys()
    root = finished["answer"]
    for child in ("retrieve", "generate"):
        assert finished[child].parent is not None
        assert finished[child].parent.span_id == root.context.span_id
    attrs = dict(root.attributes or {})
    assert attrs["permitnav.kind"] == "answered"
    assert attrs["permitnav.question_chars"] == len(QUESTION)
    assert list(attrs["permitnav.cited_units"]) == ["sfs:2005:716/6kap/2§"]
    assert "sfs:2005:716/6kap/2§" in list(finished["retrieve"].attributes["permitnav.units"])

    # The privacy rule: no question, answer, prompt or source text in any attribute.
    every_value = json.dumps(
        [{k: str(v) for k, v in (s.attributes or {}).items()} for s in spans.get_finished_spans()],
        ensure_ascii=False,
    )
    for secret in (QUESTION, "medianlönen", "90 percent", "Arbetstillstånd"):
        assert secret not in every_value


def test_an_error_is_recorded_by_type_never_by_its_message(spans) -> None:
    def fail(_: CompletionRequest) -> str:
        raise RuntimeError("OpenRouter HTTP 402: acct_123 credits; question: " + QUESTION)

    with pytest.raises(RuntimeError), tracing.span("answer", "chain"):
        tracing.complete(ScriptedLLM(fail), request(), "generate")
    for span in spans.get_finished_spans():
        assert span.status.description == "RuntimeError"
        assert not span.events  # no exception event with message and stack
        assert "acct_123" not in json.dumps({k: str(v) for k, v in (span.attributes or {}).items()})


def test_configure_once_keeps_the_provider_and_sets_the_mode(spans) -> None:
    assert tracing.configure("again", content="full") is True  # the test provider stays
    assert tracing.content_mode() == "full"
    tracing.complete(ScriptedLLM("reply"), request(), "generate")
    assert len(spans.get_finished_spans()) == 1  # still recorded


def test_cli_commands_reset_to_metadata_after_an_eval(spans, monkeypatch) -> None:
    from permitnav import cli

    monkeypatch.setattr(cli, "cmd_eval_answers", lambda args: 0)
    cli.main(["eval-answers", "--file", "x.jsonl"])
    assert tracing.content_mode() == "metadata"


def test_eval_commands_send_traces_only_when_asked_and_ask_always(spans, monkeypatch) -> None:
    """Eval runs (~10 observations per judged question) and the nightly retrieval check used
    most of the Langfuse plan's monthly units; their --out files already hold the details."""
    from permitnav import cli

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tracing,
        "configure",
        lambda service, content="metadata": calls.append((service, content)) or True,
    )
    for name in ("cmd_eval", "cmd_eval_answers", "cmd_smoke", "cmd_refresh", "cmd_ask"):
        monkeypatch.setattr(cli, name, lambda args: 0)
    cli.main(["eval", "--file", "x.jsonl"])
    cli.main(["eval-answers", "--file", "x.jsonl"])
    cli.main(["smoke"])
    cli.main(["refresh"])
    assert calls == []  # nothing configured: spans are no-ops, nothing is sent
    cli.main(["eval-answers", "--file", "x.jsonl", "--trace"])
    cli.main(["eval", "--file", "x.jsonl", "--trace"])
    cli.main(["ask", "Vilken lön krävs?"])
    assert calls == [
        ("permitnav-cli", "full"),  # golden-set questions: text allowed
        ("permitnav-cli", "metadata"),
        ("permitnav-cli", "metadata"),  # a real question: never its text
    ]


def test_only_questions_and_non_http_work_are_traced() -> None:
    from permitnav.tracing import keep_trace

    def request(method: str, path: str) -> dict[str, str]:
        return {"http.request.method": method, "url.path": path}

    # FastAPI's span is named by the method alone when sampling happens
    assert keep_trace("POST", request("POST", "/ask"))
    assert keep_trace("answer") and keep_trace("retrieve", {})  # CLI, evals, refresh check
    for method, path in [("GET", "/health"), ("HEAD", "/"), ("GET", "/wp-login.php"),
                         ("POST", "/"), ("GET", "/ask"), ("POST", "/ask/x")]:  # fmt: skip
        assert not keep_trace(method, request(method, path)), (method, path)


def test_health_checks_leave_no_trace_and_questions_do(spans) -> None:
    """FastAPI traces every request: health checks (every minute) and scanners used up the
    Langfuse plan's monthly units in two days."""
    from tests.test_api import TOKEN, client

    api = client()
    for _ in range(3):
        assert api.get("/health").status_code == 200
    assert api.get("/no-such-page").status_code == 404
    assert spans.get_finished_spans() == ()
    api.post("/ask", json={"question": "Vilken lön krävs?"},
             headers={"Authorization": f"Bearer {TOKEN}"})  # fmt: skip
    names = {s.name for s in spans.get_finished_spans()}
    assert {"POST /ask", "ask", "fastapi.endpoint"} <= names, names  # the whole request trace


def test_one_request_can_record_its_texts_without_changing_the_mode_for_others(spans) -> None:
    """The maintainer's own question (api: MAINTAINER_TOKEN) is traced in full; the process
    stays in metadata mode, also for a request running at the same time on another thread."""
    import threading

    inside = threading.Event()
    done = threading.Event()

    def another_request() -> None:
        inside.wait(2)
        tracing.complete(ScriptedLLM("other reply"), request(), "other")
        done.set()

    other = threading.Thread(target=another_request)
    other.start()
    with tracing.request(full=True):
        assert tracing.content_mode() == "full"
        inside.set()
        done.wait(2)
        tracing.complete(ScriptedLLM("reply"), request(), "mine")
    other.join()
    assert tracing.content_mode() == "metadata"
    tracing.complete(ScriptedLLM("reply"), request(), "after")
    by_name = {s.name: dict(s.attributes or {}) for s in spans.get_finished_spans()}
    assert QUESTION in str(by_name["mine"]["langfuse.observation.input"])
    for name in ("other", "after"):
        assert "langfuse.observation.input" not in by_name[name]
        assert "langfuse.observation.output" not in by_name[name]


def test_a_requests_conversation_and_user_are_on_every_span(spans) -> None:
    mine = tracing.request(session="c0nversation-id-0001", user="maintainer")
    with mine, tracing.span("ask"):
        tracing.complete(ScriptedLLM("reply"), request(), "generate")
    with tracing.span("ask"):  # the next request: nothing carried over
        pass
    first, second, later = (dict(s.attributes or {}) for s in spans.get_finished_spans())
    for attributes in (first, second):
        assert attributes["langfuse.session.id"] == "c0nversation-id-0001"
        assert attributes["langfuse.user.id"] == "maintainer"
    assert "langfuse.session.id" not in later and "langfuse.user.id" not in later
    with tracing.request(), tracing.span("ask"):  # neither given: neither set
        pass
    assert "langfuse.session.id" not in dict(spans.get_finished_spans()[-1].attributes or {})


def test_the_trace_id_is_the_one_langfuse_knows_the_trace_by(spans) -> None:
    assert tracing.trace_id() is None  # outside any span
    with tracing.span("ask") as current:
        inside = tracing.trace_id()
        assert inside == tracing.trace_id(current)
    (span,) = spans.get_finished_spans()
    assert inside == format(span.context.trace_id, "032x") and len(inside or "") == 32
    # A request the sampler drops (a health check) has no trace to score.
    with tracing.TRACER.start_as_current_span(
        "GET", attributes={"http.request.method": "GET", "url.path": "/health"}
    ) as dropped:
        assert tracing.trace_id(dropped) is None


def test_text_is_not_written_to_a_span_that_has_ended(spans, caplog) -> None:
    tracing.set_content_mode("full")
    with tracing.span("ask") as current:
        pass
    tracing.text(current, output="late")  # a worker finishing after its request's span
    (span,) = spans.get_finished_spans()
    assert "langfuse.observation.output" not in dict(span.attributes or {})
    assert "ended span" not in caplog.text


def test_every_span_names_the_versions_that_made_it(spans) -> None:
    tracing.set_versions(release="0ab9798c1d2e", version="abcdef012345")
    try:
        with tracing.span("ask") as current:
            tracing.complete(ScriptedLLM("reply"), request(), "generate")
            tracing.tag(current, "kind:answered")
    finally:
        tracing.set_versions()
    with tracing.span("later"):
        pass
    generate, ask, later = (dict(s.attributes or {}) for s in spans.get_finished_spans())
    for attributes in (generate, ask):  # Langfuse reads release and version per span
        assert attributes["langfuse.release"] == "0ab9798c1d2e"
        assert attributes["langfuse.version"] == "abcdef012345"
    assert list(ask["langfuse.trace.tags"]) == ["kind:answered"]  # a label, not a score
    assert "langfuse.release" not in later and "langfuse.trace.tags" not in generate
