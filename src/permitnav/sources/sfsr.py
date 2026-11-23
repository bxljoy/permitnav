"""The amendment register (SFSR, rkrattsbaser.gov.se) and the SFS publication feed
(svenskforfattningssamling.se): three requests a day in the refresh. No robots.txt on either
site (docs/research/source-facts.md); fetched at 1 request/s like every source."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from permitnav.sources.http import Http
from permitnav.sources.raw import RawFetch, latest_raw, save_raw

REGISTER = "https://rkrattsbaser.gov.se/sfsr?bet={sfs}"
FEED = "https://svenskforfattningssamling.se/regulations/rss.xml/rss.xml"


def fetch_register(http: Http, raw_dir: Path, sfs: str, now: datetime | None = None) -> RawFetch:
    url = REGISTER.format(sfs=sfs)
    response = http.get(url)
    if response.status != 200 or b"result-inner-sub-box" not in response.content:
        raise RuntimeError(f"{url}: HTTP {response.status}, not a register page")
    return save_raw(_register_folder(raw_dir, sfs), "sfsr", url, response.content, 200, "html", now)


def fetch_feed(http: Http, raw_dir: Path, now: datetime | None = None) -> RawFetch:
    response = http.get(FEED)
    if response.status != 200 or b"<rss" not in response.content[:500]:
        raise RuntimeError(f"{FEED}: HTTP {response.status}, not an RSS feed")
    return save_raw(raw_dir / "sfs-feed", "sfs-feed", FEED, response.content, 200, "xml", now)


def latest_register(raw_dir: Path, sfs: str) -> RawFetch | None:
    return latest_raw(_register_folder(raw_dir, sfs))


def latest_feed(raw_dir: Path) -> RawFetch | None:
    return latest_raw(raw_dir / "sfs-feed")


def _register_folder(raw_dir: Path, sfs: str) -> Path:
    return raw_dir / "sfsr" / f"sfs-{sfs.replace(':', '-')}"
