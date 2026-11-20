"""Consolidated statutes from the riksdagen open data API.

`https://data.riksdagen.se/dokument/sfs-2005-716.text` returns the current consolidated text
of SFS 2005:716 (source: Regeringskansliet). Terms: free reuse, credit "Källa: Sveriges
riksdag", and don't imply the Riksdag runs the service (docs/research/source-facts.md).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from permitnav.sources.http import Http
from permitnav.sources.raw import RawFetch, latest_raw, save_raw

__all__ = ["RawFetch", "fetch_statute", "latest_fetch", "statute_url"]

BASE_URL = "https://data.riksdagen.se/dokument"
SFS = re.compile(r"^\d{4}:\d+$")


def statute_url(sfs: str) -> str:
    if not SFS.match(sfs):
        raise ValueError(f"not an SFS number: {sfs!r}")
    year, number = sfs.split(":")
    return f"{BASE_URL}/sfs-{year}-{number}.text"


def fetch_statute(http: Http, sfs: str, raw_dir: Path, now: datetime | None = None) -> RawFetch:
    """Download one statute and save the raw text with a metadata file next to it.

    Saved as raw_dir/riksdagen/sfs-YYYY-NNN/<UTC timestamp>.txt (+ .json). Raises on a
    non-200 response or an empty body, so a failed download never replaces a good one.
    """
    url = statute_url(sfs)
    response = http.get(url)
    if response.status != 200 or not response.content.strip():
        raise RuntimeError(f"{url}: HTTP {response.status}, {len(response.content)} bytes")
    folder = raw_dir / "riksdagen" / f"sfs-{sfs.replace(':', '-')}"
    return save_raw(folder, "riksdagen", url, response.content, response.status, "txt", now)


def latest_fetch(raw_dir: Path, sfs: str) -> RawFetch | None:
    """The most recent saved download of a statute, read back from disk."""
    return latest_raw(raw_dir / "riksdagen" / f"sfs-{sfs.replace(':', '-')}")
