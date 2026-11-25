"""Tracing with OpenTelemetry (phase 3): one trace per question, sent to Langfuse.

Spans: `ask` (an API request) > `answer` > `retrieve` (> `embed`, `translate`, `embed`,
`rerank`) and `generate` (a model call; again when a reply is asked for a second time).
From the CLI, `answer` is the root. Model calls are Langfuse "generations" with model,
token usage and cost, so a trace's cost is the question's cost.

Two content modes, chosen in code, never by configuration, so no setting can make the
service record what users write:
- `metadata` (the API, `permitnav ask`): lengths, timings, token counts, costs, the answer
  kind and the retrieved unit ids. Never the question, the answer, a translation, a prompt,
  a source's text or an error message. This is the privacy rule phase 6 builds on.
- `full` (eval commands only, where every question is from the golden set): the texts too.

Exporters, from the environment: Langfuse when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY
are set (LANGFUSE_HOST defaults to the EU region), a JSON-lines file when PERMITNAV_TRACE_FILE
is set, neither otherwise (spans are then never recorded, at almost no cost). The provider
is installed once per process (OpenTelemetry allows one); `flush()` sends what is buffered.
Only questions are traced: a sampler drops every other HTTP request (health checks, scanners),
which FastAPI would otherwise trace (`keep_trace`). The CLI traces `ask`, and eval commands
only with `--trace` (`cli.traces`).

Plain OpenTelemetry, no vendor SDK: the backend is a configuration choice.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from permitnav.llm.port import LLM, Completion, CompletionRequest

TRACER = trace.get_tracer("permitnav")
LANGFUSE_EU = "https://cloud.langfuse.com"
CONTENT_MODES = ("metadata", "full")
_content = "metadata"


def content_mode() -> str:
    return _content


def set_content_mode(mode: str) -> None:
    global _content
    if mode not in CONTENT_MODES:
        raise ValueError(f"trace content must be one of {CONTENT_MODES}, not {mode!r}")
    _content = mode


_provider: Any = None


QUESTION_PATHS = ("/ask", "/ask/stream")


def keep_trace(name: str, attributes: Mapping[str, Any] | None = None) -> bool:
    """Whether a trace is worth recording, from its root span as it starts. FastAPI traces
    every request once a tracer is installed: health checks (every minute, from the uptime
    check, Docker and deploys) and internet scanners made ~97% of the traces and used half
    the Langfuse plan's monthly units in a day. Kept: questions (`POST /ask`) and every trace
    that does not start at an HTTP request (`answer`, `retrieve`, … from the CLI and evals).

    FastAPI names the span only by its method ("POST") when it starts and adds the route
    after routing, so the request is read from the start attributes, not the name."""
    attributes = attributes or {}
    method = attributes.get("http.request.method")
    if method is None:
        return True
    return method == "POST" and attributes.get("url.path") in QUESTION_PATHS


def _questions_only():
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        Decision,
        ParentBased,
        Sampler,
        SamplingResult,
    )

    class RootByName(Sampler):
        def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None,
                          links=None, trace_state=None):  # fmt: skip
            keep = keep_trace(name, attributes)
            decision = Decision.RECORD_AND_SAMPLE if keep else Decision.DROP
            return SamplingResult(decision, attributes if keep else None, trace_state)

        def get_description(self) -> str:
            return "RootByName(questions only)"

    # a dropped root drops its children too (fastapi.endpoint, fastapi.serialization, …)
    return ParentBased(root=RootByName(), remote_parent_sampled=ALWAYS_ON,
                       remote_parent_not_sampled=ALWAYS_OFF)  # fmt: skip


def configure(service: str, content: str = "metadata") -> bool:
    """Set the content mode and, the first time, install a tracer provider with the
    exporters the environment asks for. True when spans are being exported."""
    global _provider
    set_content_mode(content)
    if _provider is not None:
        return True
    exporters = _exporters()
    if not exporters:
        return False
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service, "deployment.environment": _environment()})
    provider = TracerProvider(resource=resource, sampler=_questions_only())
    for exporter in exporters:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _provider = provider
    return True


def flush() -> None:
    """Send buffered spans (before a command exits, or the API shuts down)."""
    if _provider is not None:
        _provider.force_flush()


def _environment() -> str:
    return os.environ.get("PERMITNAV_ENV", "development")


def _exporters() -> list[Any]:
    exporters: list[Any] = []
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if public and secret:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        host = os.environ.get("LANGFUSE_HOST", "").strip() or LANGFUSE_EU
        exporters.append(
            OTLPSpanExporter(
                endpoint=langfuse_endpoint(host), headers=langfuse_headers(public, secret)
            )
        )
    if path := os.environ.get("PERMITNAV_TRACE_FILE", "").strip():
        exporters.append(JsonLinesExporter(Path(path)))
    return exporters


def langfuse_endpoint(host: str) -> str:
    """Langfuse's OTLP/HTTP traces endpoint (it accepts no gRPC)."""
    return f"{host.rstrip('/')}/api/public/otel/v1/traces"


def langfuse_headers(public: str, secret: str) -> dict[str, str]:
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        # Without it, Langfuse may show directly ingested traces up to ~10 minutes late.
        "x-langfuse-ingestion-version": "4",
    }


class JsonLinesExporter:
    """Writes each finished span as one JSON line (local development, tests)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        with self._path.open("a", encoding="utf-8") as f:
            for span in spans:
                f.write(json.dumps(json.loads(span.to_json())) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@contextmanager
def span(name: str, kind: str = "span", **attributes: Any) -> Iterator[Span]:
    """A span with a Langfuse observation type ("span", "retriever", "generation", …).
    Attribute values that are None are left out."""
    # Exceptions are recorded by type only: their messages can hold provider account
    # details or echo the question (OpenTelemetry's default records message and stack).
    with TRACER.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as current:
        current.set_attribute("langfuse.observation.type", kind)
        set_attributes(current, **attributes)
        try:
            yield current
        except Exception as error:
            current.set_status(Status(StatusCode.ERROR, type(error).__name__))
            current.set_attribute("langfuse.observation.level", "ERROR")
            raise


def set_attributes(current: Span, **attributes: Any) -> None:
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        current.set_attribute(key, value)


def text(current: Span, *, input: Any = None, output: Any = None) -> None:
    """Record input and output texts, in full-content mode only."""
    if _content != "full":
        return
    if input is not None:
        current.set_attribute("langfuse.observation.input", json.dumps(input, ensure_ascii=False))
    if output is not None:
        current.set_attribute("langfuse.observation.output", json.dumps(output, ensure_ascii=False))


def complete(llm: LLM, request: CompletionRequest, name: str) -> Completion:
    """A model call as a Langfuse generation: model, tokens, cost, finish reason."""
    with span(name, "generation", **{"gen_ai.request.model": request.model}) as current:
        current.set_attribute("gen_ai.request.max_tokens", request.max_tokens)
        if request.reasoning_effort:  # it changes latency, cost and the answer: a setting, not text
            current.set_attribute("permitnav.reasoning_effort", request.reasoning_effort)
        text(current, input={"system": request.system, "messages": request.messages})
        completion = llm.complete(request)
        record_completion(current, completion)
        text(current, output=completion.text)
        return completion


def record_completion(current: Span, completion: Completion) -> None:
    set_attributes(
        current,
        **{
            "gen_ai.response.model": completion.model or None,
            "gen_ai.usage.input_tokens": completion.input_tokens,
            "gen_ai.usage.output_tokens": completion.output_tokens,
            "langfuse.observation.usage_details": {
                "input": completion.input_tokens,
                "output": completion.output_tokens,
            },
            "langfuse.observation.cost_details": (
                {"total": completion.cost_usd} if completion.cost_usd is not None else None
            ),
            "permitnav.finish_reason": completion.extra.get("finish_reason"),
        },
    )
