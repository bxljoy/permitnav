"""Position papers from Lifos (slice 5): only pages robots.txt allows.

- `curation/position_papers.csv` (the review file, in the repo) says which papers are in
  scope: a person's decision, made from saved search results (the search is closed to
  crawlers).
- Each paper version is a document page (`/dokument?documentSummaryId=<n>`) and a PDF
  (`/dokument?documentAttachmentId=<m>`); both are fetched once and kept (a version never
  changes; a new version is a new document).
- The legal department's RSS feed announces new versions. A newer version of an in-scope
  paper is followed automatically and remembered in the raw volume (`lifos/latest.json`), so
  it survives restarts; a paper not in the review file is only reported, for a person to
  decide on.
"""

from __future__ import annotations

import csv
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from permitnav.parsing.lifos import DOCUMENT_URL, RS, VERSION, _normal_version
from permitnav.sources.http import Http
from permitnav.sources.raw import RawFetch, latest_raw, save_raw

FEED = (
    "https://lifos.migrationsverket.se/2.339945412c72922c2280000/12.39a9cd9514a346077211abc"
    ".portlet?state=rss&sv.contenttype=text/xml;charset=UTF-8"
)
ATTACHMENT = "https://lifos.migrationsverket.se/dokument?documentAttachmentId={id}"
REVIEW_FILE = Path("curation/position_papers.csv")


@dataclass(frozen=True)
class Known:
    """The latest version of an in-scope paper that PermitNav knows of."""

    rs: str
    version: str
    document_id: int


def in_scope(review_file: Path = REVIEW_FILE) -> dict[str, Known]:
    """RS number -> the version the review file names, for rows decided `include`."""
    if not review_file.exists():
        return {}
    with review_file.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["decision"] == "include" and r["rs"]]
    return {r["rs"]: Known(r["rs"], r["version"], int(r["document_id"])) for r in rows}


def latest_known(raw_dir: Path, review_file: Path = REVIEW_FILE) -> dict[str, Known]:
    """The review file's versions, moved forward by what the feed has announced since."""
    known = in_scope(review_file)
    state = _state_path(raw_dir)
    if state.exists():
        for rs, entry in json.loads(state.read_text(encoding="utf-8")).items():
            if rs in known and _order(entry["version"]) > _order(known[rs].version):
                known[rs] = Known(rs, entry["version"], int(entry["document_id"]))
    return known


def remember(raw_dir: Path, paper: Known) -> None:
    state = _state_path(raw_dir)
    data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    data[paper.rs] = {"version": paper.version, "document_id": paper.document_id}
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def fetch_paper(
    http: Http, raw_dir: Path, document_id: int, now: datetime | None = None
) -> tuple[RawFetch, RawFetch]:
    """The paper's document page and its main PDF, each fetched once and kept."""
    folder = paper_folder(raw_dir, document_id)
    page = latest_raw(folder / "page")
    if page is None:
        url = DOCUMENT_URL.format(id=document_id)
        response = http.get(url)
        if response.status != 200 or b"documentAttachmentId" not in response.content:
            raise RuntimeError(f"{url}: HTTP {response.status}, not a Lifos document page")
        page = save_raw(folder / "page", "lifos", url, response.content, 200, "html", now)
    pdf = latest_raw(folder / "pdf")
    if pdf is None:
        main = page.content.split(b"Huvuddokument", 1)  # the main document, not an appendix
        attachment = re.search(rb"documentAttachmentId=(\d+)", main[-1])
        if attachment is None:
            raise RuntimeError(f"{page.url}: no main document (PDF) linked")
        url = ATTACHMENT.format(id=attachment.group(1).decode())
        response = http.get(url)
        if response.status != 200 or not response.content.startswith(b"%PDF"):
            raise RuntimeError(f"{url}: HTTP {response.status}, not a PDF")
        pdf = save_raw(folder / "pdf", "lifos", url, response.content, 200, "pdf", now)
    return page, pdf


@dataclass(frozen=True)
class FeedItem:
    rs: str
    version: str
    title: str
    link: str


def fetch_feed(http: Http) -> list[FeedItem]:
    """The legal department's latest announcements (about a month's worth)."""
    response = http.get(FEED)
    if response.status != 200 or b"<rss" not in response.content[:500]:
        raise RuntimeError(f"Lifos feed: HTTP {response.status}, not an RSS feed")
    items = []
    feed = response.content.decode("utf-8", errors="replace")
    for item in re.findall(r"<item>(.*?)</item>", feed, re.S):
        title = re.search(r"<title>(.*?)</title>", item, re.S)
        link = re.search(r"<link>(.*?)</link>", item, re.S)
        if not title or not link:
            continue
        text = re.sub(r"\s+", " ", _xml_text(title.group(1))).strip()
        rs, version = RS.search(text), VERSION.search(text)
        if rs:
            items.append(
                FeedItem(
                    rs.group(0),
                    _normal_version(version.group(1)) if version else "1.0",
                    text,
                    _xml_text(link.group(1)).strip().replace("http://", "https://", 1),
                )
            )
    return items


def document_id_of(http: Http, announcement_url: str, rs: str, version: str) -> int:
    """The document behind a feed announcement: of the documents its page links, the one
    whose own page names this RS number and version (a page can link others too)."""
    response = http.get(announcement_url)
    if response.status != 200:
        raise RuntimeError(f"{announcement_url}: HTTP {response.status}")
    candidates = dict.fromkeys(
        int(n) for n in re.findall(rb"documentSummaryId=(\d+)", response.content)
    )
    for document_id in candidates:
        page = http.get(DOCUMENT_URL.format(id=document_id))
        title = _page_title(page.content.decode("utf-8", errors="replace"))
        found = VERSION.search(title)
        if rs in title and found and _normal_version(found.group(1)) == version:
            return document_id
    raise RuntimeError(f"{announcement_url}: no linked document is {rs} version {version}")


def _page_title(page: str) -> str:
    found = re.search(r"Rättsligt ställningstagande\.[^<|]*?RS/\d{3}/\d{4}[^<|]*", page)
    return html.unescape(found.group(0)) if found else ""


def _xml_text(value: str) -> str:
    """Element text as RSS carries it: maybe in CDATA, with entities ("&amp;")."""
    cdata = re.fullmatch(r"\s*<!\[CDATA\[(.*?)\]\]>\s*", value, re.S)
    return cdata.group(1) if cdata else html.unescape(value)


def paper_folder(raw_dir: Path, document_id: int) -> Path:
    return raw_dir / "lifos" / f"doc-{document_id}"


def _state_path(raw_dir: Path) -> Path:
    return raw_dir / "lifos" / "latest.json"


def _order(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))
