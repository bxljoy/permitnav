"""Consolidated statutes from the riksdagen open data API.

`https://data.riksdagen.se/dokument/sfs-2005-716.text` returns the current consolidated text
of SFS 2005:716 (source: Regeringskansliet). Terms: free reuse, credit "Källa: Sveriges
riksdag", and don't imply the Riksdag runs the service (docs/research/source-facts.md).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from permitnav.sources.http import Http

BASE_URL = "https://data.riksdagen.se/dokument"
SFS = re.compile(r"^\d{4}:\d+$")


@dataclass(frozen=True)
class RawFetch:
    """One downloaded document, kept as evidence (docs/DESIGN.md)."""

    source: str
    url: str
    fetched_at: datetime
    content: bytes
    content_hash: str  # sha256 of content
    http_status: int
    storage_path: Path | None = None


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
    fetched_at = (now or datetime.now(UTC)).astimezone(UTC)
    content_hash = hashlib.sha256(response.content).hexdigest()
    folder = raw_dir / "riksdagen" / f"sfs-{sfs.replace(':', '-')}"
    folder.mkdir(parents=True, exist_ok=True)
    stem = fetched_at.strftime("%Y%m%dT%H%M%S.%fZ")  # UTC, unique per download
    path = folder / f"{stem}.txt"
    path.write_bytes(response.content)
    meta = {
        "source": "riksdagen",
        "url": url,
        "fetched_at": fetched_at.isoformat(),
        "content_hash": content_hash,
        "http_status": response.status,
    }
    (folder / f"{stem}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return RawFetch(
        source="riksdagen",
        url=url,
        fetched_at=fetched_at,
        content=response.content,
        content_hash=content_hash,
        http_status=response.status,
        storage_path=path,
    )


def latest_fetch(raw_dir: Path, sfs: str) -> RawFetch | None:
    """The most recent saved download of a statute, read back from disk."""
    folder = raw_dir / "riksdagen" / f"sfs-{sfs.replace(':', '-')}"
    metas = [(json.loads(m.read_text(encoding="utf-8")), m) for m in folder.glob("*.json")]
    if not metas:
        return None
    # The latest by recorded time, not by file name.
    meta, meta_path = max(metas, key=lambda item: datetime.fromisoformat(item[0]["fetched_at"]))
    path = meta_path.with_suffix(".txt")
    return RawFetch(
        source=meta["source"],
        url=meta["url"],
        fetched_at=datetime.fromisoformat(meta["fetched_at"]),
        content=path.read_bytes(),
        content_hash=meta["content_hash"],
        http_status=meta["http_status"],
        storage_path=path,
    )
