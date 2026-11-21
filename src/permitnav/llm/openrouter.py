"""OpenRouter (OpenAI-compatible chat completions), used during development (D15).

Only the author's questions and the golden set go through it; from the public beta the models
move to a provider that meets D15's criteria. Question text is never logged here.
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable

import certifi

from permitnav.llm.port import Completion, CompletionRequest

URL = "https://openrouter.ai/api/v1/chat/completions"
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


class OpenRouterLLM:
    def __init__(
        self,
        api_key: str,
        url: str = URL,
        timeout_s: float = 120.0,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        self._key = api_key
        self._url = url
        self._timeout = timeout_s
        self._retries = retries
        self._sleep = sleep
        self._tls = ssl.create_default_context(cafile=certifi.where())

    def complete(self, request: CompletionRequest) -> Completion:
        body = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "system", "content": request.system}, *request.messages],
            "usage": {"include": True},  # ask OpenRouter to report the cost
        }
        data = json.dumps(body).encode("utf-8")
        for attempt in range(self._retries + 1):
            req = urllib.request.Request(
                self._url,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                    "X-Title": "PermitNav (development)",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout, context=self._tls) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return _completion(payload, request.model)
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:300]
                if error.code in RETRY_STATUSES and attempt < self._retries:
                    self._sleep(2.0 * (attempt + 1))
                    continue
                raise LLMError(f"OpenRouter HTTP {error.code}: {detail}") from error
            except (OSError, http.client.HTTPException, ValueError) as error:
                # Connection dropped mid-reply, timeouts, unreachable host (OSError covers
                # URLError and TimeoutError), or a 200 that is not JSON (ValueError).
                if attempt < self._retries:
                    self._sleep(2.0 * (attempt + 1))
                    continue
                raise LLMError(
                    f"OpenRouter request failed: {type(error).__name__}: {error}"
                ) from error
        raise LLMError("unreachable")  # the loop always returns or raises


def _completion(payload: dict, requested_model: str) -> Completion:
    if "error" in payload:
        raise LLMError(f"OpenRouter error: {str(payload['error'])[:300]}")
    choices = payload.get("choices") or []
    if not choices:
        raise LLMError("OpenRouter returned no choices")
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    if not isinstance(text, str):  # some providers return content parts
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    usage = payload.get("usage") or {}
    cost = usage.get("cost")
    return Completion(
        text=text,
        model=payload.get("model", requested_model),
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        cost_usd=float(cost) if cost is not None else None,
        extra={"finish_reason": choices[0].get("finish_reason")},
    )
