"""HTTP API (phase 3): `POST /ask` and `GET /health`.

Run with `uvicorn permitnav.api:app` (the `api` extra). Every request except `/health`
needs `Authorization: Bearer <API_TOKEN>`: until the public beta (phase 7) the service is for
its author only, so other people's questions never reach the development LLM route (D15).
Without API_TOKEN the app refuses to start, unless PERMITNAV_ALLOW_NO_AUTH=1 (local only).
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from permitnav import tracing
from permitnav.agent.intake import MAX_HISTORY_ACCEPTED, MAX_QUESTION_CHARS
from permitnav.answering.answerer import Answer

NOTICE = "Not legal advice; check with Migrationsverket. You are talking to an AI."
CREDITS = {
    "law": "Källa: Sveriges riksdag",
    "ordinance": "Källa: Sveriges riksdag",
    "web_page": "Källa: Migrationsverket",
    "ruling": "Källa: Sveriges Domstolar",
    "position_paper": "Källa: Migrationsverket",
}
log = logging.getLogger("permitnav.api")


class Service(Protocol):
    def answer(self, question: str, as_of: date, history: list[str]) -> Answer: ...
    def source(self, version_id: int) -> dict[str, str] | None: ...
    def healthy(self) -> bool: ...


Question = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
]


class AskRequest(BaseModel):
    question: Question
    as_of: date | None = None  # the date the question is asked about (D8); default today
    # The user's earlier questions in this conversation, oldest first (phase 5 memory). The
    # client keeps them; the server uses the last few for this answer only, never stores or
    # traces their text. A whole conversation may be sent; the list is only bounded.
    history: Annotated[list[Question], Field(max_length=MAX_HISTORY_ACCEPTED)] = []


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


def create_app(
    service: Service | None = None, token: str | None = None, web_dir: str | None = WEB_DIR
) -> FastAPI:
    """The app. Tests pass a fake service and a token; production builds both from the
    environment at startup (loading bge-m3 takes a few seconds)."""
    state: dict[str, Any] = {"service": service, "token": token}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if state["token"] is None:
            state["token"] = os.environ.get("API_TOKEN", "").strip() or None
        if state["token"] is None and os.environ.get("PERMITNAV_ALLOW_NO_AUTH") != "1":
            raise RuntimeError("API_TOKEN is not set (PERMITNAV_ALLOW_NO_AUTH=1 for local use)")
        tracing.configure("permitnav-api", content="metadata")  # always: users' questions
        if state["service"] is None:
            from permitnav.service import build_service

            state["service"] = build_service()
        yield
        tracing.flush()  # send the last traces

    app = FastAPI(title="PermitNav", version="0.0.1", lifespan=lifespan)

    def authorised(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = state["token"]
        if expected is None:
            return  # local use without a token, explicitly allowed at startup
        scheme, _, given = (authorization or "").strip().partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode(), expected.encode()
        ):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    @app.get("/health")
    def health() -> dict[str, str]:
        if not state["service"].healthy():
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ok"}

    @app.get("/auth-check", dependencies=[Depends(authorised)])
    def auth_check() -> dict[str, bool]:
        """Lets a client check a token without asking (and paying for) a question."""
        return {"ok": True}

    @app.post("/source", dependencies=[Depends(authorised)])
    def source(request: SourceRequest) -> SourceOut:
        """The full text of the exact wording an answer cited (the citation panel). By
        version id, since an older wording shares its unit with the current one; a POST
        body, so which sources a person opens never appears in a URL or an access log."""
        found = state["service"].source(request.version_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such source")
        return SourceOut(**found)

    @app.post("/ask", dependencies=[Depends(authorised)])
    def ask(request: AskRequest) -> AskResponse:
        as_of = request.as_of or date.today()
        with tracing.span(
            "ask",
            "span",
            **{
                "langfuse.trace.name": "ask",
                "langfuse.environment": os.environ.get("PERMITNAV_ENV", "development"),
                "permitnav.question_chars": len(request.question),
                "permitnav.history_turns": len(request.history),  # how many, never what
            },
        ) as current:
            response = _ask(request.question, as_of, request.history)
            current.set_attribute("permitnav.kind", response.kind)
            return response

    def _ask(question: str, as_of: date, history: list[str]) -> AskResponse:
        from permitnav.llm.port import LLMError

        try:
            answer = state["service"].answer(question, as_of, history)
        except LLMError as error:
            # The provider's message can carry account or billing details: logged, not returned.
            log.error("model call failed: %s", _first_part(error))
            raise HTTPException(status_code=502, detail="model call failed") from error
        if answer.kind == "failed":  # unreadable or cut-off reply, even after asking again
            log.error("answer failed: the model's reply could not be read")
            raise HTTPException(status_code=502, detail="the model's reply could not be read")
        return to_response(answer, as_of)

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
