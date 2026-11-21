"""A scripted stand-in for tests: returns prepared replies and records every request."""

from __future__ import annotations

from collections.abc import Callable

from permitnav.llm.port import Completion, CompletionRequest


class ScriptedLLM:
    def __init__(self, reply: str | Callable[[CompletionRequest], str]) -> None:
        self._reply = reply
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        text = self._reply(request) if callable(self._reply) else self._reply
        return Completion(
            text=text, model=request.model, input_tokens=100, output_tokens=50, cost_usd=0.0
        )
