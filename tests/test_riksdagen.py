"""Fetching statutes from the riksdagen API, without the network."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

from permitnav.sources import http as http_module
from permitnav.sources.http import FixtureHttp, LiveHttp
from permitnav.sources.riksdagen import fetch_statute, latest_fetch, statute_url

FIXTURE = Path(__file__).parent / "fixtures" / "utlanningslagen_excerpt.txt"
NOW = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)


def test_statute_url() -> None:
    assert statute_url("2005:716") == "https://data.riksdagen.se/dokument/sfs-2005-716.text"
    with pytest.raises(ValueError):
        statute_url("sfs-2005-716")


def test_fetch_saves_raw_text_and_metadata(tmp_path: Path) -> None:
    http = FixtureHttp({statute_url("2005:716"): FIXTURE})
    fetched = fetch_statute(http, "2005:716", tmp_path, now=NOW)
    assert fetched.content == FIXTURE.read_bytes()
    assert fetched.storage_path == tmp_path / "riksdagen/sfs-2005-716/20261120T093000.000000Z.txt"
    assert len(fetched.content_hash) == 64
    again = latest_fetch(tmp_path, "2005:716")
    assert again is not None
    assert (again.url, again.fetched_at, again.content_hash) == (
        fetched.url,
        NOW,
        fetched.content_hash,
    )


def test_a_failed_download_raises_and_saves_nothing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="HTTP 404"):
        fetch_statute(FixtureHttp({}), "2005:716", tmp_path, now=NOW)
    assert latest_fetch(tmp_path, "2005:716") is None


def test_live_http_waits_between_requests_to_the_same_host(monkeypatch) -> None:
    class FakeResponse(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

    sent = []
    monkeypatch.setattr(
        http_module.urllib.request,
        "urlopen",
        lambda request, timeout, context: sent.append(request) or FakeResponse(b"ok"),
    )
    now = [100.0]
    slept = []
    live = LiveHttp(user_agent="PermitNav-test", clock=lambda: now[0], sleep=slept.append)
    live.get("https://data.riksdagen.se/a")
    now[0] += 0.25
    live.get("https://data.riksdagen.se/b")
    live.get("https://example.org/c")  # another host: no wait
    assert slept == [pytest.approx(0.75)]
    assert sent[0].get_header("User-agent") == "PermitNav-test"


def test_file_names_are_utc_and_the_latest_download_is_chosen_by_time(tmp_path: Path) -> None:
    from datetime import timedelta, timezone

    cet = timezone(timedelta(hours=1))
    http = FixtureHttp({statute_url("2005:716"): b"first"})
    first = fetch_statute(http, "2005:716", tmp_path, now=datetime(2026, 11, 20, 10, 0, tzinfo=cet))
    assert first.storage_path is not None and first.storage_path.name.startswith("20261120T090000")
    http = FixtureHttp({statute_url("2005:716"): b"second"})
    fetch_statute(http, "2005:716", tmp_path, now=datetime(2026, 11, 20, 9, 30, tzinfo=UTC))
    latest = latest_fetch(tmp_path, "2005:716")
    assert latest is not None and latest.content == b"second"
