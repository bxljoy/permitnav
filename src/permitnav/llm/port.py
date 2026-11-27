"""The LLM interface: one request in, one completion out."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class LLMError(RuntimeError):
    """A model call failed (after any retries the adapter makes)."""


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    system: str
    messages: list[dict[str, str]]  # [{"role": "user" | "assistant", "content": ...}]
    max_tokens: int = 4000
    # For reasoning models ("none" | "minimal" | "low" | "medium" | "high"): how much hidden
    # thinking they may do before answering; it counts against max_tokens. None sends nothing
    # (the model's default); "none" asks for no thinking.
    reasoning_effort: str | None = None
    # A JSON Schema the reply must follow (structured output): the provider then returns one
    # JSON object of that shape and nothing else. None: free text. Only for models whose
    # endpoint supports it; others reject the request.
    response_schema: dict | None = None
    # No sampling parameters: current Claude models reject temperature/top_p.


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None  # as reported by the provider, if it reports it
    extra: dict[str, object] = field(default_factory=dict)


class LLM(Protocol):
    def complete(self, request: CompletionRequest) -> Completion: ...
