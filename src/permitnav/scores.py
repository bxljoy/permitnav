"""Scores on production traces, sent to Langfuse (online evals).

A score is a name and a value attached to a trace: the answer's kind, whether a second model
call was needed, the live judge's verdict, a person's rating. It carries no text, so a
trace recorded as metadata only can still be found by what happened to it.

Sending never delays or fails an answer: scores wait in a bounded queue and one background
thread posts them; when Langfuse is slow or away, scores are dropped and counted in the log.
Each score counts against the Langfuse plan's monthly units, as a span does, so only a few
are sent per question (permitnav.online).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import certifi

log = logging.getLogger("permitnav.scores")

MAX_WAITING = 500
TIMEOUT_S = 10
Value = bool | str | float


def body(trace_id: str, name: str, value: Value, environment: str) -> dict[str, Any]:
    """Langfuse's score object. The id is fixed per trace and name, so sending the same
    score twice (a retry, a second click) replaces it instead of adding one."""
    if isinstance(value, bool):
        data_type, sent = "BOOLEAN", 1 if value else 0
    elif isinstance(value, str):
        data_type, sent = "CATEGORICAL", value
    else:
        data_type, sent = "NUMERIC", value
    return {
        "id": f"{trace_id}-{name}",
        "traceId": trace_id,
        "name": name,
        "value": sent,
        "dataType": data_type,
        "environment": environment,
    }


class Scores:
    def __init__(
        self,
        host: str,
        public: str,
        secret: str,
        environment: str,
        post: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._url = f"{host.rstrip('/')}/api/public/scores"
        auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
        self._headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
        self._environment = environment
        self._post = post or self._send
        self._tls = ssl.create_default_context(cafile=certifi.where())
        self._waiting: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_WAITING)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.dropped = 0

    def add(self, trace_id: str | None, name: str, value: Value) -> None:
        """Queue a score for a trace; nothing happens for a trace that was not recorded."""
        if not trace_id:
            return
        try:
            self._waiting.put_nowait(body(trace_id, name, value, self._environment))
        except queue.Full:
            self._drop("the queue is full")
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="scores", daemon=True)
                self._thread.start()

    def flush(self, timeout_s: float = 5.0) -> bool:
        """Wait until the queued scores are sent (shutdown, tests). False on a timeout."""
        deadline = time.monotonic() + timeout_s
        while self._waiting.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _run(self) -> None:
        while True:
            score = self._waiting.get()
            try:
                self._post(score)
            except Exception as error:  # a score is never worth a crash; no text is logged
                self._drop(type(error).__name__)
            finally:
                self._waiting.task_done()

    def _drop(self, why: str) -> None:
        self.dropped += 1
        if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
            log.warning("score not sent to Langfuse (%s); %d dropped so far", why, self.dropped)

    def _send(self, score: dict[str, Any]) -> None:
        request = urllib.request.Request(  # noqa: S310  (an https URL from configuration)
            self._url, data=json.dumps(score).encode(), headers=self._headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=self._tls) as reply:  # noqa: S310
            reply.read()


def from_environment() -> Scores | None:
    """The sender for the Langfuse project the traces go to; None without its keys."""
    from permitnav.tracing import LANGFUSE_EU

    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not (public and secret):
        return None
    host = os.environ.get("LANGFUSE_HOST", "").strip() or LANGFUSE_EU
    return Scores(host, public, secret, os.environ.get("PERMITNAV_ENV", "development"))
