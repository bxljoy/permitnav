"""Migrationsverket's web pages (D22: agency practice), found through the site's sitemap.

- Scope (D1): work permits for applicants and employers, extensions, changing or losing a job,
  permanent residence and EU long-term resident status, and processing times. Asylum,
  protection and studies are left out.
- Swedish pages are found in the sitemap; each one's English version comes from its
  `hreflang` link (D6).
- Every URL is checked against the site's robots.txt before fetching. Its `/flikar/` tab pages
  are disallowed; their content is already on the main page.
- Terms: all text on migrationsverket.se may be reused with attribution "Källa: Migrationsverket"
  (docs/research/source-facts.md).
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from permitnav.sources.http import Http
from permitnav.sources.raw import RawFetch, latest_raw, save_raw

SITE = "https://www.migrationsverket.se"
SITEMAP_INDEX = f"{SITE}/sitemapindex.xml"
ROBOTS = f"{SITE}/robots.txt"

IN_SCOPE = [
    re.compile(p)
    for p in (
        r"^/du-vill-ansoka/arbeta(/|\.html$)",
        r"^/du-vill-forlanga/arbeta(/|\.html$)",
        r"^/du-har-tillstand-i-sverige/arbeta(/|\.html$)",
        r"^/du-vantar-pa-beslut/arbeta(/|\.html$)",
        r"^/du-har-fatt-beslut/arbeta(/|\.html$)",
        r"^/arbetsgivare(/|\.html$)",
        r"permanent-uppehallstillstand(/|\.html$)",
        r"varaktigt-bosatt-i-sverige(/|\.html$)",
        r"^/kontakta-oss/vantetider\.html$",
    )
]
# News archives are dated announcements, not current guidance (e.g. last year's salary limit).
OUT_OF_SCOPE = re.compile(r"asyl|skydd|studera|doktorand|nyhetsarkiv|/nyheter/|news-archive|/news/")


def in_scope(url: str) -> bool:
    path = urlsplit(url).path
    return (
        path.endswith(".html")
        and any(rule.search(path) for rule in IN_SCOPE)
        and not OUT_OF_SCOPE.search(path)
    )


class Robots:
    """The `User-agent: *` rules of a robots.txt, with `*` and `$` wildcards (RFC 9309).

    The longest matching rule wins; Allow wins a tie. Python's urllib.robotparser does not
    support wildcards inside paths, which this site uses (`/*/flikar/`).
    """

    def __init__(self, text: str) -> None:
        self._rules: list[tuple[bool, re.Pattern[str], int]] = []
        applies = False
        group_started = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if group_started:  # a new group starts after rules
                    applies = False
                    group_started = False
                applies = applies or value == "*"
            elif field in ("allow", "disallow"):
                group_started = True
                if applies and value:
                    self._rules.append((field == "allow", _pattern(value), len(value)))

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for allow, pattern, length in self._rules:
            if pattern.match(target) and (best is None or (length, allow) > best):
                best = (length, allow)
        return best is None or best[1]


def load_robots(http: Http) -> Robots:
    """The site's robots.txt, following RFC 9309: a 4xx means no rules (all allowed); any
    other failure means the crawl must not go ahead, rather than crawling without rules."""
    response = http.get(ROBOTS)
    if response.status == 200:
        return Robots(response.content.decode("utf-8", errors="replace"))
    if 400 <= response.status < 500:
        return Robots("")
    raise RuntimeError(f"{ROBOTS}: HTTP {response.status}; not crawling without its rules")


class FetchError(RuntimeError):
    def __init__(self, url: str, status: int, size: int) -> None:
        super().__init__(f"{url}: HTTP {status}, {size} bytes")
        self.status = status

    @property
    def gone(self) -> bool:
        """The page no longer exists (as opposed to a temporary failure)."""
        return self.status in (404, 410)


def _pattern(rule: str) -> re.Pattern[str]:
    anchored = rule.endswith("$")
    body = re.escape(rule.rstrip("$")).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def discover(http: Http) -> list[str]:
    """In-scope Swedish page URLs from the sitemap(s), sorted."""
    index = http.get(SITEMAP_INDEX)
    if index.status != 200:
        raise RuntimeError(f"{SITEMAP_INDEX}: HTTP {index.status}")
    urls: set[str] = set()
    for sitemap in _locs(index.content.decode("utf-8")):
        response = http.get(sitemap)
        if response.status != 200:
            raise RuntimeError(f"{sitemap}: HTTP {response.status}")
        body = response.content
        if sitemap.endswith(".gz") or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        urls.update(u for u in _locs(body.decode("utf-8")) if in_scope(u))
    return sorted(urls)


def _locs(xml: str) -> Iterable[str]:
    return (m.strip() for m in re.findall(r"<loc>([^<]+)</loc>", xml))


def page_folder(raw_dir: Path, url: str) -> Path:
    path = urlsplit(url).path.removesuffix(".html").strip("/") or "index"
    return raw_dir / "migrationsverket" / path


def fetch_page(
    http: Http, robots: Robots, url: str, raw_dir: Path, now: datetime | None = None
) -> RawFetch:
    if not url.startswith(SITE + "/"):
        raise ValueError(f"not a Migrationsverket page: {url}")
    if not robots.allowed(url):
        raise PermissionError(f"robots.txt disallows {url}")
    response = http.get(url)
    if response.status != 200 or b"<html" not in response.content[:2000].lower():
        raise FetchError(url, response.status, len(response.content))
    return save_raw(
        page_folder(raw_dir, url),
        "migrationsverket",
        url,
        response.content,
        response.status,
        "html",
        now,
    )


MANIFEST = "_crawl.json"


@dataclass
class Crawl:
    """What the latest crawl found: pages fetched, pages gone (404/410 or no longer listed),
    and pages that failed temporarily. Ingest closes the sections of pages that are gone."""

    finished_at: datetime
    fetched: list[str]
    failed: list[str]


def save_crawl(raw_dir: Path, crawl: Crawl) -> None:
    root = raw_dir / "migrationsverket"
    root.mkdir(parents=True, exist_ok=True)
    data = {
        "finished_at": crawl.finished_at.isoformat(),
        "fetched": crawl.fetched,
        "failed": crawl.failed,
    }
    (root / MANIFEST).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def load_crawl(raw_dir: Path) -> Crawl | None:
    path = raw_dir / "migrationsverket" / MANIFEST
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Crawl(datetime.fromisoformat(data["finished_at"]), data["fetched"], data["failed"])


def finish_crawl(raw_dir: Path, fetched: list[str], failed: list[str]) -> Crawl:
    crawl = Crawl(datetime.now(UTC), sorted(set(fetched)), sorted(set(failed)))
    save_crawl(raw_dir, crawl)
    return crawl


def saved_pages(raw_dir: Path) -> list[RawFetch]:
    """The latest saved download of every page that the latest crawl still found (or, before
    any crawl manifest exists, every saved page), excluding out-of-scope pages. Sorted by URL.
    English pages are fetched as translations of in-scope Swedish pages; only exclusions apply."""
    root = raw_dir / "migrationsverket"
    crawl = load_crawl(raw_dir)
    current = None if crawl is None else set(crawl.fetched) | set(crawl.failed)
    folders = {meta.parent for meta in root.rglob("*.json") if meta.name != MANIFEST}
    fetches = [
        f
        for f in (latest_raw(folder) for folder in folders)
        if f is not None
        and not OUT_OF_SCOPE.search(urlsplit(f.url).path)
        and (current is None or f.url in current)
    ]
    return sorted(fetches, key=lambda f: f.url)
