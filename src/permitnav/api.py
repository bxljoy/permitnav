"""HTTP API (phase 3): `POST /ask` and `GET /health`.

Run with `uvicorn permitnav.api:app` (the `api` extra). Every request except `/health`
needs `Authorization: Bearer <API_TOKEN>`: until the public beta (phase 7) the service is for
its author only, so other people's questions never reach the development LLM route (D15).
Without API_TOKEN the app refuses to start, unless PERMITNAV_ALLOW_NO_AUTH=1 (local only).

MAINTAINER_TOKEN, when set, is a second token that is accepted everywhere API_TOKEN is. A
question sent with it is the maintainer's own: its trace records the texts and is labelled
"maintainer" (permitnav.tracing). Every other question is traced as metadata only.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Literal, Protocol

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span
from pydantic import BaseModel, Field, StrictBool, StringConstraints, field_validator

from permitnav import tracing
from permitnav.agent.intake import MAX_HISTORY_ACCEPTED, MAX_QUESTION_CHARS
from permitnav.answering.answerer import Answer
from permitnav.feedback import (
    MAX_CITED,
    MAX_COMMENT_CHARS,
    MAX_TEXT_CHARS,
    FeedbackRefused,
    quiet_access_log,
)

NOTICE = "Not legal advice; check with Migrationsverket. You are talking to an AI."
CREDITS = {
    "law": "Källa: Sveriges riksdag",
    "ordinance": "Källa: Sveriges riksdag",
    "web_page": "Källa: Migrationsverket",
    "ruling": "Källa: Sveriges Domstolar",
    "position_paper": "Källa: Migrationsverket",
}
log = logging.getLogger("permitnav.api")


MIN_MAINTAINER_TOKEN_CHARS = 32
SPEND_CAP_MESSAGE = "PermitNav has reached its limit for today. Try again tomorrow."


class Service(Protocol):
    def answer(
        self,
        question: str,
        as_of: date,
        history: list[str],
        progress: Callable[[str], None] | None = None,
    ) -> Answer: ...
    def source(self, version_id: int) -> dict[str, str] | None: ...
    def record(
        self, answer: Answer, question: str, history: list[str], trace_id: str | None
    ) -> None: ...
    def rate(self, trace_id: str | None, helpful: bool) -> bool: ...
    def save_feedback(self, case: FeedbackIn) -> None: ...
    def withdraw_feedback(self, code: str) -> bool: ...
    def healthy(self) -> bool: ...


Question = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
]


ConversationId = Annotated[
    str, StringConstraints(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
]
TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class AskRequest(BaseModel):
    question: Question
    as_of: date | None = None  # the date the question is asked about (D8); default today
    # The user's earlier questions in this conversation, oldest first (phase 5 memory). The
    # client keeps them; the server uses the last few for this answer only, never stores or
    # traces their text. A whole conversation may be sent; the list is only bounded.
    history: Annotated[list[Question], Field(max_length=MAX_HISTORY_ACCEPTED)] = []
    # A random id the browser makes for a conversation and forgets when it is deleted. It
    # groups the conversation's traces (Langfuse's Sessions); it is tied to no person, is
    # never stored by the server, and a client may leave it out.
    conversation: ConversationId | None = None


class RatingIn(BaseModel):
    """A click under an answer. No text: the trace it belongs to and yes or no."""

    helpful: StrictBool
    trace_id: TraceId | None = None  # absent when the answer was not traced


class CitationOut(BaseModel):
    marker: str
    unit_id: str
    heading: str
    url: str
    authority: str
    dates: str
    quote: str
    translation: str
    version_id: int | None = None  # the exact wording: what GET /source opens


# Made by the browser (32 random bytes, base64url); the server keeps only its hash.
WithdrawCode = Annotated[
    str, StringConstraints(min_length=32, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
]


class FeedbackIn(BaseModel):
    """What the person saw in the form and chose to save: nothing else is stored."""

    # The explicit "save this" click: JSON true and nothing else (not 1, not "true").
    consent: Literal[True]
    withdraw_code: WithdrawCode
    question: Question
    answer: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXT_CHARS)
    ]
    comment: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=MAX_COMMENT_CHARS)
    ] = ""
    kind: Literal["answered", "follow_up", "not_settled", "out_of_scope"]
    as_of: date
    cited: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=300)]],
        Field(max_length=MAX_CITED),
    ] = []

    @field_validator("consent", mode="before")
    @classmethod
    def _a_real_true(cls, value: object) -> object:
        if value is not True:  # Literal[True] alone accepts the number 1
            raise ValueError("consent must be true")
        return value


class WithdrawIn(BaseModel):
    withdraw_code: WithdrawCode


class ClientGone(Exception):
    """A streamed answer's client disconnected: stop before the next stage."""


class SourceRequest(BaseModel):
    version_id: Annotated[int, Field(ge=1)]


class SourceOut(BaseModel):
    unit_id: str
    heading: str
    authority: str
    url: str
    dates: str
    text: str


class AskResponse(BaseModel):
    kind: str  # answered | follow_up | not_settled | out_of_scope | failed
    text: str
    citations: list[CitationOut]
    credits: list[str]
    as_of: date
    notice: str
    # When the question was made self-contained with the history: how it was understood,
    # and what the user has said about their situation, so they can see and correct it.
    understood_as: str | None = None
    situation: dict[str, str] = {}
    # The answer's trace, when it was traced: what a rating is sent back with.
    trace_id: str | None = None
    # This question was traced with its text (the maintainer's own): the page says so.
    recorded: bool = False


def to_response(answer: Answer, as_of: date) -> AskResponse:
    credits = list(
        dict.fromkeys(CREDITS[c.authority] for c in answer.citations if c.authority in CREDITS)
    )
    return AskResponse(
        kind=answer.kind,
        text=answer.text,
        citations=[
            CitationOut(
                marker=c.marker,
                unit_id=c.unit_id,
                heading=c.heading,
                url=c.url,
                authority=c.authority,
                dates=c.dates,
                quote=c.quote,
                translation=c.translation,
                version_id=c.version_id,
            )
            for c in answer.citations
        ],
        credits=credits,
        as_of=as_of,
        notice=NOTICE,
        understood_as=answer.intake.question if answer.intake and answer.intake.rewritten else None,
        situation=dict(answer.intake.situation) if answer.intake else {},
    )


def _first_part(error: Exception) -> str:
    """'OpenRouter HTTP 402: <provider body>' -> 'OpenRouter HTTP 402': the body can carry
    account or billing details, which do not belong in logs."""
    return str(error).split(":", 1)[0][:80]


WEB_DIR = os.environ.get("PERMITNAV_WEB_DIR", "/app/web")  # the built chat UI (phase 6)


class _RequestSpan:
    """Keeps the HTTP request's own span (FastAPI's, the first row of a trace) where the
    endpoint can reach it: by the time the endpoint runs, the current span is a child."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {})["request_span"] = trace.get_current_span()
        await self.app(scope, receive, send)


def _request_span(http: Request) -> Span | None:
    return getattr(http.state, "request_span", None)


def create_app(
    service: Service | None = None,
    token: str | None = None,
    web_dir: str | None = WEB_DIR,
    maintainer_token: str | None = None,
) -> FastAPI:
    """The app. Tests pass a fake service and a token; production builds both from the
    environment at startup (loading bge-m3 takes a few seconds)."""
    state: dict[str, Any] = {"service": service, "token": token, "maintainer": maintainer_token}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if state["token"] is None:
            state["token"] = os.environ.get("API_TOKEN", "").strip() or None
        if state["token"] is None and os.environ.get("PERMITNAV_ALLOW_NO_AUTH") != "1":
            raise RuntimeError("API_TOKEN is not set (PERMITNAV_ALLOW_NO_AUTH=1 for local use)")
        if state["maintainer"] is None:
            state["maintainer"] = os.environ.get("MAINTAINER_TOKEN", "").strip() or None
        if state["maintainer"] is not None:
            # It decides whose text is recorded, so it must be a secret of its own.
            if state["maintainer"] == state["token"]:
                raise RuntimeError("MAINTAINER_TOKEN must differ from API_TOKEN")
            if len(state["maintainer"]) < MIN_MAINTAINER_TOKEN_CHARS:
                raise RuntimeError(
                    f"MAINTAINER_TOKEN must be at least {MIN_MAINTAINER_TOKEN_CHARS} characters"
                )
        tracing.configure("permitnav-api", content="metadata")  # always: users' questions
        if state["service"] is None:
            from permitnav.service import build_service

            state["service"] = build_service()
        yield
        try:
            close = getattr(state["service"], "close", None)
            if close is not None:
                close()  # the last counts, verdicts and scores, before the traces (bounded)
        finally:
            tracing.flush()  # send the last traces

    app = FastAPI(title="PermitNav", version="0.0.1", lifespan=lifespan)
    app.add_middleware(_RequestSpan)
    quiet_access_log()

    def authorised(authorization: Annotated[str | None, Header()] = None) -> bool:
        """Lets the request through, and says whether it is the maintainer's own (sent with
        MAINTAINER_TOKEN). Without tokens (local use, allowed at startup) nobody is."""
        scheme, _, given = (authorization or "").strip().partition(" ")
        given = given.strip() if scheme.lower() == "bearer" else ""
        maintainer = state["maintainer"]
        # Both comparisons always run, so the time taken does not say which token was near.
        is_maintainer = maintainer is not None and hmac.compare_digest(
            given.encode(), maintainer.encode()
        )
        expected = state["token"]
        is_user = expected is not None and hmac.compare_digest(given.encode(), expected.encode())
        if expected is not None and not (is_user or is_maintainer):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        return is_maintainer

    @app.get("/health")
    def health() -> dict[str, str]:
        if not state["service"].healthy():
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ok"}

    @app.get("/auth-check")
    def auth_check(maintainer: bool = Depends(authorised)) -> dict[str, bool]:
        """Lets a client check a token without asking (and paying for) a question.
        `recorded`: questions sent with this token are traced with their text, which the
        page says where the questions are typed."""
        return {"ok": True, "recorded": maintainer}

    @app.post("/rating", dependencies=[Depends(authorised)])
    def rating(given: RatingIn) -> dict[str, bool]:
        """A person's click under an answer: helpful or not. Counted per day, and put on
        the answer's trace as a score. No text is sent or stored. `saved` is false when
        the click could not be counted: the page then keeps its buttons."""
        try:
            saved = bool(state["service"].rate(given.trace_id, given.helpful))
        except Exception as error:  # a rating is not worth an error page
            log.error("rating not recorded: %s", type(error).__name__)
            saved = False
        return {"saved": saved}

    @app.post("/source", dependencies=[Depends(authorised)])
    def source(request: SourceRequest) -> SourceOut:
        """The full text of the exact wording an answer cited (the citation panel). By
        version id, since an older wording shares its unit with the current one; a POST
        body, so which sources a person opens never appears in a URL or an access log."""
        found = state["service"].source(request.version_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such source")
        return SourceOut(**found)

    @app.post("/feedback", dependencies=[Depends(authorised)])
    def save_feedback(case: FeedbackIn) -> dict[str, bool]:
        """Store a question and answer the person chose to save. Never traced, and no log
        line: one with the moment of the request could be matched to the row."""
        try:
            state["service"].save_feedback(case)
        except FeedbackRefused as refused:
            raise HTTPException(status_code=422, detail=str(refused)) from refused
        return {"saved": True}

    @app.post("/feedback/withdraw", dependencies=[Depends(authorised)])
    def withdraw_feedback(request: WithdrawIn) -> dict[str, bool]:
        """Delete the submission saved under this code. A body, not a URL, so the code
        never reaches an access log."""
        if not state["service"].withdraw_feedback(request.withdraw_code):
            raise HTTPException(status_code=404, detail="no such submission")
        return {"withdrawn": True}

    @app.post("/ask")
    def ask(
        request: AskRequest, http: Request, maintainer: bool = Depends(authorised)
    ) -> AskResponse:
        return _traced_ask(request, maintainer, top=_request_span(http))

    @app.post("/ask/stream")
    async def ask_stream(
        request: AskRequest, http: Request, maintainer: bool = Depends(authorised)
    ) -> StreamingResponse:
        """The same answer as /ask, as server-sent events: `stage` events while it is being
        prepared ("understanding", "searching", "checking", "writing"), then one `answer`
        event with the AskResponse, or one `error` event with a message and the status /ask
        would have returned. Stages, not partial text: citations are checked only once the
        whole reply is in.

        The answer runs in anyio's worker threads (bounded, as /ask's are) and sends its
        stages through a memory stream; if the client goes away, it stops at the next stage
        instead of paying for model calls nobody will read."""
        context = otel_context.get_current()  # the request's trace, carried into the worker
        top = _request_span(http)
        gone = threading.Event()

        async def stream() -> AsyncIterator[str]:
            send, receive = anyio.create_memory_object_stream[tuple[str, dict[str, Any]]](16)

            def progress(stage: str) -> None:
                if gone.is_set():
                    raise ClientGone
                anyio.from_thread.run(send.send, ("stage", {"stage": stage}))

            def work() -> tuple[str, dict[str, Any]] | None:
                token = otel_context.attach(context)
                try:
                    response = _traced_ask(request, maintainer, progress, top)
                    return ("answer", response.model_dump(mode="json"))
                except HTTPException as error:
                    return ("error", {"status": error.status_code, "message": error.detail})
                except (ClientGone, anyio.BrokenResourceError, anyio.ClosedResourceError):
                    return None  # nobody is listening any more
                except Exception:  # never leave the client waiting; the detail stays in the log
                    log.exception("streamed answer failed")
                    return ("error", {"status": 500, "message": "the answer failed"})
                finally:
                    otel_context.detach(token)

            async def run() -> None:
                async with send:
                    outcome = await anyio.to_thread.run_sync(work)
                    if outcome is not None:
                        await send.send(outcome)

            async def watch(tasks: anyio.abc.TaskGroup) -> None:
                # Usually the server cancels this stream when the client leaves, which closes
                # `receive` and makes the worker's next stage fail. Depending on the ASGI
                # version, Starlette may notice only when a write fails, after the next stage
                # has started: so also ask the connection a few times a second.
                while not await http.is_disconnected():
                    await anyio.sleep(0.2)
                gone.set()
                tasks.cancel_scope.cancel()

            try:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(run)
                    tasks.start_soon(watch, tasks)
                    async with receive:
                        async for kind, data in receive:
                            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    tasks.cancel_scope.cancel()  # the answer is out: stop watching
            finally:
                gone.set()  # whatever ended the stream, the worker stops at its next stage

        # no-transform: proxies (Caddy's compression) must pass the events on as they come
        headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    def _traced_ask(
        request: AskRequest,
        maintainer: bool,
        progress: Callable[[str], None] | None = None,
        top: Span | None = None,
    ) -> AskResponse:
        """`top`: the HTTP request's own span, the first row of the trace. It gets the
        conversation and user as the other spans do, and, for the maintainer's own question
        only, the question and the answer, so a list of traces shows them."""
        as_of = request.as_of or date.today()
        # The maintainer's own question is recorded with its text; nobody else's is.
        with (
            tracing.request(
                full=maintainer,
                session=request.conversation,
                user="maintainer" if maintainer else None,
            ),
            tracing.span(
                "ask",
                "span",
                **{
                    "langfuse.trace.name": "ask",
                    "langfuse.environment": os.environ.get("PERMITNAV_ENV", "development"),
                    "permitnav.question_chars": len(request.question),
                    "permitnav.history_turns": len(request.history),  # how many, never what
                    "permitnav.streamed": progress is not None,
                },
            ) as current,
        ):
            if top is not None:
                tracing.label(top)
                tracing.text(top, input=request.question)
            try:
                response = _ask(request, as_of, progress, tracing.trace_id(current))
            except HTTPException as error:
                if top is not None:
                    # Our own fixed wording (never a provider's message): so a question that
                    # got no answer does not look like one with an empty answer, also when
                    # streamed, where the HTTP status stays 200.
                    tracing.text(top, output={"error": error.status_code, "detail": error.detail})
                    if top.is_recording():
                        top.set_attribute("langfuse.observation.level", "ERROR")
                raise
            response.recorded = maintainer
            current.set_attribute("permitnav.kind", response.kind)
            if top is not None:
                tracing.text(top, output=response.text)
            return response

    def _ask(
        request: AskRequest,
        as_of: date,
        progress: Callable[[str], None] | None,
        trace_id: str | None,
    ) -> AskResponse:
        from permitnav.llm.port import LLMError
        from permitnav.spend import SpendCapReached

        try:
            answer = state["service"].answer(request.question, as_of, request.history, progress)
        except SpendCapReached as reached:
            # Usually a leaked access token: an error in the log, a plain message to the person.
            log.error("question declined: %s", reached)
            raise HTTPException(status_code=429, detail=SPEND_CAP_MESSAGE) from reached
        except LLMError as error:
            # The provider's message can carry account or billing details: logged, not returned.
            log.error("model call failed: %s", _first_part(error))
            raise HTTPException(status_code=502, detail="model call failed") from error
        try:  # counts, scores, perhaps the live judge: never worth losing the answer over
            state["service"].record(answer, request.question, request.history, trace_id)
        except Exception as error:
            log.error("answer not recorded for online evals: %s", type(error).__name__)
        if answer.kind == "failed":  # unreadable, cut off or without an answer, even when re-asked
            # The reason, never the reply's text (it can repeat the question).
            log.error("answer failed: %s", answer.problem or "the reply could not be read")
            raise HTTPException(status_code=502, detail="the model's reply could not be read")
        if answer.other_language:  # shown all the same; counted here (no text, no question)
            log.warning("answer in another language than the question")
        if answer.recovered:  # an ordinary answer; counted so the recovery can be watched
            log.warning("answer read from a reply that was not valid JSON")
        response = to_response(answer, as_of)
        response.trace_id = trace_id
        return response

    # The chat UI (phase 6), when built: the page, its icon and /assets only, by GET, so
    # every other path still gets the API's 404 or trailing-slash redirect. The page holds no
    # secret; it asks for the access token and sends it with each request.
    if web_dir and os.path.isfile(os.path.join(web_dir, "index.html")):
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        index = os.path.join(web_dir, "index.html")

        @app.get("/", include_in_schema=False)
        def page() -> FileResponse:
            return FileResponse(index, headers={"Cache-Control": "no-cache"})

        icon = os.path.join(web_dir, "favicon.svg")
        if os.path.isfile(icon):

            @app.get("/favicon.svg", include_in_schema=False)
            def favicon() -> FileResponse:
                return FileResponse(icon)

        assets = os.path.join(web_dir, "assets")
        if os.path.isdir(assets):
            app.mount("/assets", StaticFiles(directory=assets), name="assets")
    return app


def __getattr__(name: str) -> FastAPI:
    """`uvicorn permitnav.api:app` builds the app on first access, not at import (tests
    import this module without the environment)."""
    if name == "app":
        return create_app()
    raise AttributeError(name)
