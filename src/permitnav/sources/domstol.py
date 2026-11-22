"""Rulings from the Swedish courts' open data API, "Sök rättspraxis" (PUH API, CC0).

`GET /api/v1/publiceringar?domstolkod=MIOD&page=N&pagesize=100` lists the Migration Court
of Appeal's publications with full text, cited law sections and cited rulings
(docs/research/source-facts.md). Pages are capped at 100 records; the total is in the
`x-total-count` header, which the Http port does not expose, so paging stops at the first
short page. All pages of one fetch are saved together as one raw download (one JSON array,
stored with a .txt suffix: raw.py keeps each download's metadata in <time>.json).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from permitnav.sources.http import Http
from permitnav.sources.raw import RawFetch, latest_raw, save_raw

API = "https://rattspraxis.etjanst.domstol.se/api/v1/publiceringar"
PAGE_SIZE = 100
MAX_PAGES = 50  # a safety stop: the court has ~535 publications


def list_url(court: str) -> str:
    return f"{API}?domstolkod={court}"


def fetch_rulings(
    http: Http, raw_dir: Path, court: str = "MIOD", now: datetime | None = None
) -> RawFetch:
    """Download every publication of a court and save them as one JSON array.

    Raises on a failed page or a malformed response, so a partial list never replaces a
    complete one (rulings missing from a fetch would be closed by ingest)."""
    records: list[dict] = []
    for page in range(MAX_PAGES):
        url = f"{list_url(court)}&page={page}&pagesize={PAGE_SIZE}"
        response = http.get(url)
        if response.status != 200:
            raise RuntimeError(f"{url}: HTTP {response.status}")
        batch = json.loads(response.content.decode("utf-8"))
        if not isinstance(batch, list):
            raise RuntimeError(f"{url}: expected a JSON array")
        records += batch
        if len(batch) < PAGE_SIZE:
            break
    else:
        raise RuntimeError(f"{list_url(court)}: more than {MAX_PAGES} pages; check the API")
    if not records:
        raise RuntimeError(f"{list_url(court)}: no publications")
    content = json.dumps(records, ensure_ascii=False).encode("utf-8")
    return save_raw(_folder(raw_dir, court), "domstol", list_url(court), content, 200, "txt", now)


def latest_rulings(raw_dir: Path, court: str = "MIOD") -> RawFetch | None:
    return latest_raw(_folder(raw_dir, court))


def _folder(raw_dir: Path, court: str) -> Path:
    return raw_dir / "domstol" / court.lower()
