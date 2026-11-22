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
from pydantic import BaseModel, StringConstraints

from permitnav.answering.answerer import Answer

NOTICE = "Not legal advice; check with Migrationsverket. You are talking to an AI."
CREDITS = {
    "law": "Källa: Sveriges riksdag",
    "ordinance": "Källa: Sveriges riksdag",
    "web_page": "Källa: Migrationsverket",
    "ruling": "Källa: Sveriges Domstolar",
}
MAX_QUESTION_CHARS = 2000
log = logging.getLogger("permitnav.api")


class Service(Protocol):
    def answer(self, question: str, as_of: date) -> Answer: ...
    def healthy(self) -> bool: ...


class AskRequest(BaseModel):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
    ]
    as_of: date | None = None  # the date the question is asked about (D8); default today


class CitationOut(BaseModel):
    marker: str
    unit_id: str
    heading: str
    url: str
    authority: str
    dates: str
    quote: str
    translation: str


class AskResponse(BaseModel):
    kind: str  # answered | follow_up | not_settled | out_of_scope | failed
    text: str
    citations: list[CitationOut]
    credits: list[str]
    as_of: date
    notice: str


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
            )
            for c in answer.citations
        ],
        credits=credits,
        as_of=as_of,
        notice=NOTICE,
    )


def create_app(service: Service | None = None, token: str | None = None) -> FastAPI:
    """The app. Tests pass a fake service and a token; production builds both from the
    environment at startup (loading bge-m3 takes a few seconds)."""
    state: dict[str, Any] = {"service": service, "token": token}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if state["token"] is None:
            state["token"] = os.environ.get("API_TOKEN", "").strip() or None
        if state["token"] is None and os.environ.get("PERMITNAV_ALLOW_NO_AUTH") != "1":
            raise RuntimeError("API_TOKEN is not set (PERMITNAV_ALLOW_NO_AUTH=1 for local use)")
        if state["service"] is None:
            from permitnav.service import build_service

            state["service"] = build_service()
        yield

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

    @app.post("/ask", dependencies=[Depends(authorised)])
    def ask(request: AskRequest) -> AskResponse:
        from permitnav.llm.port import LLMError

        as_of = request.as_of or date.today()
        try:
            answer = state["service"].answer(request.question, as_of)
        except LLMError as error:
            # The provider's message can carry account or billing details: logged, not returned.
            log.error("model call failed: %s", error)
            raise HTTPException(status_code=502, detail="model call failed") from error
        if answer.kind == "failed":  # unreadable or cut-off reply, even after asking again
            log.error("answer failed: the model's reply could not be read")
            raise HTTPException(status_code=502, detail="the model's reply could not be read")
        return to_response(answer, as_of)

    return app


def __getattr__(name: str) -> FastAPI:
    """`uvicorn permitnav.api:app` builds the app on first access, not at import (tests
    import this module without the environment)."""
    if name == "app":
        return create_app()
    raise AttributeError(name)
