"""Read ZIM files: articles as text, built-in search, raw content for the Reader."""
from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass
from functools import lru_cache

import lxml.html
from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.suggestion import SuggestionSearcher

_lock = threading.Lock()
_BLOCKS = ("p", "li", "dd", "dt", "pre", "blockquote", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "br")
_DROP = ("//script|//style|//noscript|//sup[contains(@class,'reference')]|//*[contains(@class,'navbox')]"
         "|//*[contains(@class,'mw-references')]|//ol[contains(@class,'references')]|//*[contains(@class,'reflist')]"
         "|//*[contains(@class,'mw-editsection')]|//*[contains(@class,'metadata')]|//figure|//*[@role='navigation']")
TEXT_MIMES = ("text/html", "application/pdf", "text/plain")


@lru_cache(maxsize=32)
def open_archive(path: str) -> Archive:
    return Archive(path)


@dataclass
class Article:
    path: str
    title: str
    text: str
    mime: str


def html_to_text(html: bytes | str) -> str:
    """Article text with headings marked `## `, references and navigation removed."""
    try:
        doc = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return ""
    for bad in doc.xpath(_DROP):
        bad.drop_tree()
    for el in doc.iter(*_BLOCKS):
        if el.tag in ("h2", "h3", "h4") and (el.text_content() or "").strip():
            el.text = "\n## " + (el.text or "")
        el.tail = "\n" + (el.tail or "")
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in doc.text_content().splitlines())
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n## (\n## )+", "\n## ", text)


def intro(text: str, limit: int = 1200) -> str:
    head = text.split("\n## ", 1)[0]
    return head[:limit]


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:200])
    except Exception:  # noqa: BLE001 - damaged PDFs are skipped
        return ""


def _resolve(archive: Archive, path: str):
    entry = archive.get_entry_by_path(path)
    hops = 0
    while entry.is_redirect and hops < 5:
        entry, hops = entry.get_redirect_entry(), hops + 1
    return entry


@lru_cache(maxsize=512)  # parsed article text; Wikipedia pages are re-read for every question about them
def read_article(path_on_disk: str, entry_path: str) -> Article | None:
    archive = open_archive(path_on_disk)
    try:
        entry = _resolve(archive, entry_path)
    except KeyError:
        return None
    item = entry.get_item()
    mime = item.mimetype.split(";")[0]
    data = bytes(item.content)
    if mime == "text/html":
        text = html_to_text(data)
    elif mime == "application/pdf":
        text = _pdf_text(data)
    elif mime == "text/plain":
        text = data.decode("utf-8", "replace")
    else:
        return None
    return Article(entry.path, entry.title or entry.path, text, mime)


def _as_path(result) -> str:
    """libzim 4 returns result objects; older versions return plain paths."""
    return result if isinstance(result, str) else result.path


def search(path_on_disk: str, query: str, limit: int = 8) -> list[str]:
    """Entry paths matching `query`: full-text index first, then titles."""
    archive = open_archive(path_on_disk)
    paths: list[str] = []
    if archive.has_fulltext_index:
        with _lock:
            result = Searcher(archive).search(Query().set_query(query))
            paths += [_as_path(r) for r in result.get_results(0, limit)]
    if len(paths) < limit and archive.has_title_index:
        with _lock:
            result = SuggestionSearcher(archive).suggest(query)
            paths += [p for p in map(_as_path, result.get_results(0, limit)) if p not in paths]
    return paths[:limit]


def content(path_on_disk: str, entry_path: str) -> tuple[bytes, str, str] | None:
    """Raw bytes, mimetype and the canonical path, for the Reader."""
    archive = open_archive(path_on_disk)
    if not entry_path:
        if not archive.has_main_entry:
            return None
        entry = archive.main_entry
        while entry.is_redirect:
            entry = entry.get_redirect_entry()
        return content(path_on_disk, entry.path)
    try:
        entry = _resolve(archive, entry_path)
    except KeyError:
        return None
    item = entry.get_item()
    return bytes(item.content), item.mimetype, entry.path


def metadata(path_on_disk: str) -> dict:
    archive = open_archive(path_on_disk)

    def meta(key: str) -> str:
        try:
            return bytes(archive.get_metadata(key)).decode("utf-8", "replace")
        except (KeyError, RuntimeError):
            return ""

    main = ""
    if archive.has_main_entry:
        entry = archive.main_entry
        while entry.is_redirect:
            entry = entry.get_redirect_entry()
        main = entry.path
    return {"title": meta("Title"), "description": meta("Description"), "language": meta("Language"),
            "date": meta("Date"), "name": meta("Name"), "license": meta("License"), "creator": meta("Creator"),
            "articles": archive.article_count, "entries": archive.entry_count, "main": main,
            "fulltext": archive.has_fulltext_index, "has_checksum": archive.has_checksum}


def iter_articles(path_on_disk: str, start: int = 0):
    """Yield (entry_id, path, title) of readable articles from entry id `start` (resumable)."""
    archive = open_archive(path_on_disk)
    for i in range(start, archive.entry_count):
        entry = archive.get_entry_by_id(i)
        if entry.is_redirect:
            continue
        try:
            mime = entry.get_item().mimetype.split(";")[0]
        except RuntimeError:
            continue
        if mime in TEXT_MIMES:
            yield i, entry.path, entry.title or entry.path


def check(path_on_disk: str) -> bool:
    archive = Archive(path_on_disk)
    return archive.check() if archive.has_checksum else True
