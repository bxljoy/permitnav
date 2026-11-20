"""Saving and reading raw downloads, kept as evidence (docs/DESIGN.md: the download table)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class RawFetch:
    """One downloaded document."""

    source: str
    url: str
    fetched_at: datetime
    content: bytes
    content_hash: str  # sha256 of content
    http_status: int
    storage_path: Path | None = None


def save_raw(
    folder: Path,
    source: str,
    url: str,
    content: bytes,
    status: int,
    suffix: str,
    now: datetime | None,
) -> RawFetch:
    """Write content and a metadata file to folder/<UTC timestamp>.<suffix> (+ .json)."""
    fetched_at = (now or datetime.now(UTC)).astimezone(UTC)
    content_hash = hashlib.sha256(content).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    stem = fetched_at.strftime("%Y%m%dT%H%M%S.%fZ")  # UTC, unique per download
    path = folder / f"{stem}.{suffix}"
    path.write_bytes(content)
    meta = {
        "source": source,
        "url": url,
        "fetched_at": fetched_at.isoformat(),
        "content_hash": content_hash,
        "http_status": status,
    }
    (folder / f"{stem}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return RawFetch(source, url, fetched_at, content, content_hash, status, path)


def latest_raw(folder: Path) -> RawFetch | None:
    """The most recent download saved in a folder, by recorded time (not file name)."""
    metas = [(json.loads(m.read_text(encoding="utf-8")), m) for m in folder.glob("*.json")]
    if not metas:
        return None
    meta, meta_path = max(metas, key=lambda item: datetime.fromisoformat(item[0]["fetched_at"]))
    content_path = next(
        p for p in meta_path.parent.glob(meta_path.stem + ".*") if p.suffix != ".json"
    )
    return RawFetch(
        source=meta["source"],
        url=meta["url"],
        fetched_at=datetime.fromisoformat(meta["fetched_at"]),
        content=content_path.read_bytes(),
        content_hash=meta["content_hash"],
        http_status=meta["http_status"],
        storage_path=content_path,
    )
