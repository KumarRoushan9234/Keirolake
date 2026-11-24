"""Turn raw bytes into clean text."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path


class UnsupportedFile(Exception):
    pass


@dataclass
class ParsedDoc:
    title: str
    text: str
    mime: str
    meta: dict = field(default_factory=dict)


TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log", ".py", ".js", ".ts", ".sql", ".yaml", ".yml"}
SUPPORTED_EXTS = TEXT_EXTS | {".pdf", ".docx", ".html", ".htm", ".json", ".jsonl"}


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf(data: bytes) -> tuple[str, dict]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    # Page markers keep citations useful ("see page 4").
    text = "\n\n".join(f"[page {i + 1}]\n{p}" for i, p in enumerate(pages) if p)
    meta = {"pages": len(pages)}
    if reader.metadata and reader.metadata.title:
        meta["pdf_title"] = str(reader.metadata.title)
    return text, meta


def _docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n\n".join(parts)


def html_to_text(html: str) -> str:
    import trafilatura

    extracted = trafilatura.extract(html, output_format="markdown", include_tables=True, include_links=False)
    return extracted or ""


def _json(data: bytes, jsonl: bool) -> str:
    raw = _decode(data)
    if jsonl:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return "\n\n".join(json.dumps(r, ensure_ascii=False, indent=1) for r in rows)
    return json.dumps(json.loads(raw), ensure_ascii=False, indent=1)


def parse_bytes(filename: str, data: bytes) -> ParsedDoc:
    ext = Path(filename).suffix.lower()
    title = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or filename
    meta: dict = {"filename": filename, "bytes": len(data)}
    if ext == ".pdf":
        text, extra = _pdf(data)
        meta.update(extra)
        title = extra.get("pdf_title") or title
        mime = "application/pdf"
    elif ext == ".docx":
        text, mime = _docx(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif ext in {".html", ".htm"}:
        text, mime = html_to_text(_decode(data)), "text/html"
    elif ext in {".json", ".jsonl"}:
        text, mime = _json(data, ext == ".jsonl"), "application/json"
    elif ext in TEXT_EXTS:
        text, mime = _decode(data), "text/markdown" if ext in {".md", ".markdown"} else "text/plain"
    else:
        raise UnsupportedFile(f"Unsupported file type {ext or '(none)'}; supported: {', '.join(sorted(SUPPORTED_EXTS))}")
    return ParsedDoc(title=title, text=text.strip(), mime=mime, meta=meta)


def pdf_tables(data: bytes, max_tables: int = 20) -> list[list[list[str]]]:
    """Tables found on a PDF's pages (ruled or clearly aligned), each as rows of cells. Header row first."""
    import pdfplumber

    found: list[list[list[str]]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:200]:
            for table in page.extract_tables():
                rows = [[(c or "").replace("\n", " ").strip() for c in row] for row in table if row and any((c or "").strip() for c in row)]
                if len(rows) >= 3 and len(rows[0]) >= 2:  # a header plus at least two rows
                    found.append(rows)
                if len(found) >= max_tables:
                    return found
    return found
