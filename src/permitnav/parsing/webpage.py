"""Parser for Migrationsverket's web pages (D2, D5, D6, D22).

Each page becomes one unit per `h2` section of the main content, with the heading id as the
anchor: `web:sv/du-vill-ansoka/arbeta/.../anstalld#kravforattfaarbetstillstand`. Text before
the first `h2` becomes an `#intro` unit. Sub-headings (h3, h4) stay inside their section's
text. Pages are dated by when the crawler saw them (`time_basis = "observed"`).

The page's `hreflang` links pair the Swedish and English versions (D6). Pure function, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from permitnav.parsing.types import ParsedUnit

SITE = "https://www.migrationsverket.se"
MAIN = ".mv-main-content"
SKIP_SECTIONS = {"nyheter", "news"}  # news listings, not guidance
# Elements that hold text of their own. A string belongs to its nearest such ancestor, so text
# is collected once, including text in a plain <div> or before a nested list.
BLOCK_TAGS = frozenset(
    {
        "p",
        "li",
        "h3",
        "h4",
        "h5",
        "h6",
        "td",
        "th",
        "dt",
        "dd",
        "div",
        "blockquote",
        "section",
        "article",
        "figcaption",
        "caption",
        "pre",
        "ul",
        "ol",
        "dl",
        "table",
        "tr",
    }
)
HEADINGS = frozenset({"h3", "h4", "h5", "h6"})
WHITESPACE = re.compile(r"\s+")


@dataclass
class Page:
    url: str
    language: str  # sv | en
    title: str
    translations: dict[str, str] = field(default_factory=dict)  # language -> url
    units: list[ParsedUnit] = field(default_factory=list)


def page_path(url: str) -> tuple[str, str]:
    """(language, path) for a page URL: /en/... is English, everything else Swedish."""
    path = urlsplit(url).path.removesuffix(".html").strip("/")
    if path.startswith("en/"):
        return "en", path.removeprefix("en/")
    return "sv", path


def parse_page(html: str, url: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    language, path = page_path(url)
    title = _text(soup.title) if soup.title else path
    title = re.sub(r"\s+[–-]\s+(Migrationsverket|Swedish Migration Agency)$", "", title)
    page = Page(url=url, language=language, title=title, translations=_translations(soup))

    main = soup.select_one(MAIN)
    if main is None:
        raise ValueError(f"{url}: no main content ({MAIN})")
    for tag in main.select("script, style, noscript, nav, form, button"):
        tag.decompose()

    sections: list[tuple[str, str, str, list[str]]] = []  # (anchor, fragment, heading, text)
    current: tuple[str, str, str, list[str]] = ("intro", "", title, [])
    used: set[str] = set()
    for element in main.find_all(["h2", *BLOCK_TAGS]):
        if not isinstance(element, Tag):
            continue
        if element.name == "h2":
            sections.append(current)
            heading = _text(element)
            slug, fragment = _anchor(element, heading)
            anchor, n = slug, 1
            while anchor in used:  # repeated headings (tab copies): -2, -3, ... never reused
                n += 1
                anchor = f"{slug}-{n}"
            used.add(anchor)
            current = (anchor, fragment, heading, [])
            continue
        text = _own_text(element)
        if not text:
            continue
        if element.name in HEADINGS:
            text = f"## {text}"
        elif element.name == "li" or element.find_parent("li") is not None:
            text = f"- {text}"  # a list item, or a paragraph inside one
        paragraphs = current[3]
        if not paragraphs or paragraphs[-1] != text:  # tab copies sometimes repeat a line
            paragraphs.append(text)
    sections.append(current)

    for anchor, fragment, heading, paragraphs in sections:
        if not paragraphs or heading.lower() in SKIP_SECTIONS:
            continue
        page.units.append(
            ParsedUnit(
                unit_id=f"web:{language}/{path}#{anchor}",
                source_type="web_page",
                authority="web_page",
                language=language,
                document=url,
                heading=title if anchor == "intro" else f"{title} › {heading}",
                # The page's own element id, so the link jumps to the section.
                url=f"{url}#{fragment}" if fragment else url,
                text="\n".join(paragraphs),
                time_basis="observed",
            )
        )
    return page


def _own_text(element: Tag) -> str:
    """The text that belongs to this element and not to a nested block inside it."""
    parts = []
    for string in element.find_all(string=True):
        if not isinstance(string, NavigableString) or type(string) is not NavigableString:
            continue  # comments, CDATA and the like
        owner = next((a for a in string.parents if a.name in BLOCK_TAGS or a.name == "h2"), None)
        if owner is element:
            parts.append(str(string))
    return WHITESPACE.sub(" ", " ".join(parts)).strip()


def _text(element: Tag | NavigableString | None) -> str:
    if element is None:
        return ""
    return WHITESPACE.sub(" ", element.get_text(" ", strip=True)).strip()


def _anchor(h2: Tag, heading: str) -> tuple[str, str]:
    """(slug for the unit id, the page's own element id for the link fragment)."""
    element_id = str(h2.get("id") or "")
    raw = element_id.removeprefix("h-") or heading
    slug = re.sub(r"[^a-z0-9]+", "-", _ascii(raw.lower())).strip("-")
    return slug or "section", element_id


def _ascii(text: str) -> str:
    return text.translate(str.maketrans("åäöéü", "aaoeu"))


def _translations(soup: BeautifulSoup) -> dict[str, str]:
    found: dict[str, str] = {}
    for link in soup.find_all("link", hreflang=True):
        if not isinstance(link, Tag):
            continue
        lang = str(link.get("hreflang", "")).lower()
        href = str(link.get("href", ""))
        if lang.startswith("sv"):
            found["sv"] = href
        elif lang.startswith("en"):
            found["en"] = href
    return found
