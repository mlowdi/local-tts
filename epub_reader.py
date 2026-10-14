"""Extract readable EPUB spine sections without rendering or executing HTML."""

from dataclasses import dataclass
from pathlib import Path
import posixpath
import re
import textwrap
from urllib.parse import unquote, urldefrag

from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub


@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    href: str
    text: str


@dataclass(frozen=True)
class Book:
    title: str
    author: str
    language: str
    chapters: list[Chapter]


def _path(href: str) -> str:
    return posixpath.normpath(unquote(urldefrag(href)[0]))


def _toc_titles(entries) -> dict[str, str]:
    titles = {}
    for entry in entries:
        if isinstance(entry, tuple):
            section, children = entry
            if getattr(section, "href", None):
                titles.setdefault(_path(section.href), section.title)
            for href, title in _toc_titles(children).items():
                titles.setdefault(href, title)
        elif getattr(entry, "href", None):
            titles.setdefault(_path(entry.href), entry.title)
    return titles


def html_text(content: bytes) -> tuple[str, str]:
    soup = BeautifulSoup(content, "html.parser")
    for node in soup.select('head, script, style, nav, noscript, svg, [hidden], [aria-hidden="true"]'):
        node.decompose()
    for node in reversed(soup.find_all(style=True)):
        if re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", node["style"], re.I):
            node.decompose()
    heading = soup.find(["h1", "h2", "h3"])
    title = heading.get_text(" ", strip=True) if heading else ""
    for node in soup.find_all("br"):
        node.replace_with("\n")
    for node in soup.find_all(["p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "pre"]):
        node.append("\n\n")
    # Do not put spaces around inline tags: <em>word</em>, must stay "word,".
    text = soup.get_text().replace("\u00ad", "").replace("\u200b", "")
    paragraphs = [" ".join(part.split()) for part in re.split(r"\n\s*\n", text)]
    return title, "\n\n".join(part for part in paragraphs if part)


def read_book(path: Path) -> Book:
    book = epub.read_epub(path, options={"ignore_ncx": False})
    titles = _toc_titles(book.toc)
    navigation = {_path(item["href"]) for item in book.guide if item.get("type") == "toc"}
    seen = set()
    chapters = []
    for uid, linear in book.spine:
        if linear == "no" or uid in seen:
            continue
        seen.add(uid)
        item = book.get_item_with_id(uid)
        if item is None:
            raise ValueError(f"EPUB spine references a missing item: {uid}")
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        href = _path(item.get_name())
        if "nav" in item.properties or href in navigation:
            continue
        heading, text = html_text(item.get_body_content())
        if not any(character.isalnum() for character in text):
            continue
        number = len(chapters) + 1
        title = titles.get(href) or heading or f"Section {number}"
        chapters.append(Chapter(number, title, href, text))
    if not chapters:
        raise ValueError("No readable text in the EPUB; use a DRM-free, text-based EPUB")

    def metadata(name, default=""):
        values = book.get_metadata("DC", name)
        return "; ".join(str(value) for value, _ in values) if values else default

    return Book(metadata("title", path.stem), metadata("creator"), metadata("language"), chapters)


def split_text(text: str, limit: int = 900) -> list[str]:
    """Prefer sentence/paragraph boundaries; hard-wrap only oversized sentences."""
    chunks = []
    pending = ""
    for sentence in re.split(r"(?<=[.!?…])\s+|\n\s*\n", text):
        sentence = " ".join(sentence.split())
        for piece in textwrap.wrap(sentence, width=limit, break_on_hyphens=False):
            if pending and len(pending) + len(piece) + 1 > limit:
                chunks.append(pending)
                pending = ""
            pending = f"{pending} {piece}" if pending else piece
    if pending:
        chunks.append(pending)
    return chunks


def select_chapters(chapters: list[Chapter], selection: str | None) -> list[Chapter]:
    if selection is None:
        return chapters
    numbers = set()
    for part in selection.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:-(\d+))?\s*", part)
        if not match:
            raise ValueError("--chapters must contain indices/ranges, for example 1,3-5")
        first = int(match[1])
        last = int(match[2] or first)
        if not 1 <= first <= last <= len(chapters):
            raise ValueError(f"Chapter indices must be between 1 and {len(chapters)}")
        numbers.update(range(first, last + 1))
    return [chapter for chapter in chapters if chapter.number in numbers]
