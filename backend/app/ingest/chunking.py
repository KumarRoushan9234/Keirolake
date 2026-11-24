"""Recursive text splitter: prefer paragraph, then line, then sentence, then
word boundaries, so chunks stay readable for citations.

Sizes are in characters. The default (1500 chars, roughly 375 tokens) stays
inside the 512-token window of e5-large / bge-large, which sit later in the
default embedding fallback chain.
"""
from __future__ import annotations

import re

SEPARATORS = ["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " "]


def _split(text: str, size: int, separators: list[str]) -> list[str]:
    if len(text) <= size:
        return [text]
    for i, sep in enumerate(separators):
        if sep not in text:
            continue
        pieces, current = [], ""
        for part in text.split(sep):
            candidate = f"{current}{sep}{part}" if current else part
            if len(candidate) <= size:
                current = candidate
                continue
            if current:
                pieces.append(current)
            # A single part may still be too big: recurse with finer separators.
            if len(part) > size:
                pieces.extend(_split(part, size, separators[i + 1 :]))
                current = ""
            else:
                current = part
        if current:
            pieces.append(current)
        return pieces
    # No separator left: hard cut.
    return [text[j : j + size] for j in range(0, len(text), size)]


def chunk_text(text: str, size: int = 1500, overlap: int = 200) -> list[str]:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    overlap = max(0, min(overlap, size // 2))
    pieces = _split(text, size - overlap if overlap else size, SEPARATORS)
    if not overlap:
        return [p.strip() for p in pieces if p.strip()]
    chunks = []
    for i, piece in enumerate(pieces):
        if i == 0:
            chunks.append(piece)
            continue
        tail = pieces[i - 1][-overlap:]
        # Start the overlap at a word boundary.
        space = tail.find(" ")
        tail = tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail
        chunks.append(f"{tail} {piece}")
    return [c.strip() for c in chunks if c.strip()]
