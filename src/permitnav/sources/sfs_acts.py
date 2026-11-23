"""Amending acts as published (phase 4, slice 3): the authentic PDF of each act.

From 1 April 2018 the official SFS is svenskforfattningssamling.se: one HTML page per act
(`/doc/<year><number>.html`, no zero padding) linking its sealed PDF. Earlier printed SFS
are PDFs at rkrattsdb.gov.se (`/SFSdoc/<yy>/<yy><number:04d>.PDF`). Neither site has a
robots.txt. An act never changes once published, so each is fetched once and kept.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from permitnav.sources.http import Http
from permitnav.sources.raw import RawFetch, latest_raw, save_raw

SFS_SITE = "https://svenskforfattningssamling.se/doc/{year}{number}.html"
PRINTED = "https://rkrattsdb.gov.se/SFSdoc/{yy}/{yy}{number:04d}.PDF"


def act_folder(raw_dir: Path, sfs: str) -> Path:
    return raw_dir / "sfs-acts" / f"sfs-{sfs.replace(':', '-')}"


def latest_act(raw_dir: Path, sfs: str) -> RawFetch | None:
    return latest_raw(act_folder(raw_dir, sfs))


def fetch_act(http: Http, raw_dir: Path, sfs: str, now: datetime | None = None) -> RawFetch:
    """The act's PDF: kept if already downloaded, else from the official site of its time."""
    if (kept := latest_act(raw_dir, sfs)) is not None:
        return kept
    year, number = (int(part) for part in sfs.split(":"))
    url = _from_sfs_site(http, year, number) if year >= 2018 else None
    if url is None:  # printed SFS (up to 2018:159), or not on the new site
        url = PRINTED.format(yy=f"{year % 100:02d}", number=number)
    response = http.get(url)
    if response.status != 200 or not response.content.startswith(b"%PDF"):
        raise RuntimeError(f"SFS {sfs}: {url}: HTTP {response.status}, not a PDF")
    return save_raw(act_folder(raw_dir, sfs), "sfs-act", url, response.content, 200, "pdf", now)


def _from_sfs_site(http: Http, year: int, number: int) -> str | None:
    page_url = SFS_SITE.format(year=year, number=number)
    page = http.get(page_url)
    if page.status != 200:
        return None
    link = re.search(rb'href="([^"]+\.pdf)"', page.content)
    return urljoin(page_url, link.group(1).decode()) if link else None


def acts_on_disk(raw_dir: Path) -> list[str]:
    """The SFS numbers of every act PDF downloaded so far."""
    folder = raw_dir / "sfs-acts"
    if not folder.exists():
        return []
    return sorted(
        p.name.removeprefix("sfs-").replace("-", ":", 1)
        for p in folder.iterdir()
        if p.is_dir() and latest_raw(p) is not None
    )
