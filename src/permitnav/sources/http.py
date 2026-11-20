"""The HTTP seam: live requests in production, saved files in tests (docs/DESIGN.md)."""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import certifi


@dataclass(frozen=True)
class HttpResponse:
    status: int
    content: bytes


class Http(Protocol):
    def get(self, url: str) -> HttpResponse: ...


class LiveHttp:
    """Real HTTP with a per-host minimum interval between requests (crawling rules: 1/s)."""

    def __init__(
        self,
        user_agent: str,
        min_interval_s: float = 1.0,
        timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._user_agent = user_agent
        self._min_interval = min_interval_s
        self._timeout = timeout_s
        self._clock = clock
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        # Mozilla's CA bundle: Python builds don't always find the OS certificate store.
        self._tls = ssl.create_default_context(cafile=certifi.where())

    def get(self, url: str) -> HttpResponse:
        host = urlsplit(url).netloc
        if host in self._last_request:
            wait = self._min_interval - (self._clock() - self._last_request[host])
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()
        request = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout, context=self._tls
            ) as response:
                return HttpResponse(status=response.status, content=response.read())
        except urllib.error.HTTPError as error:
            return HttpResponse(status=error.code, content=error.read())


class FixtureHttp:
    """Serves saved files by URL; unknown URLs are a 404. For tests, never the network."""

    def __init__(self, files: dict[str, Path | bytes]) -> None:
        self._files = files
        self.requested: list[str] = []

    def get(self, url: str) -> HttpResponse:
        self.requested.append(url)
        body = self._files.get(url)
        if body is None:
            return HttpResponse(status=404, content=b"")
        return HttpResponse(
            status=200, content=body if isinstance(body, bytes) else body.read_bytes()
        )
