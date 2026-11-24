"""Read JSON from a model's reply, forgiving the usual slips of small models.

Handles code fences, text around the object, trailing commas, single-quoted strings, comments,
Python's True/False/None, and answers cut off before the closing braces. Returns None when
nothing usable can be recovered, so callers can retry instead of failing the whole run.
"""
from __future__ import annotations

import json
import re

FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _first_balanced(text: str, opener: str) -> str | None:
    """From the first `opener` on: the balanced object/array, or the rest of the text if it is cut off."""
    closer = "}" if opener == "{" else "]"
    start = text.find(opener)
    if start < 0:
        return None
    depth, in_str, quote, esc = 0, False, "", False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str, quote = True, ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def _close_truncated(text: str) -> str:
    """Close open brackets of a cut-off answer. A value that was cut mid-way is dropped, never kept half-written."""
    stack, in_str, quote, esc = [], False, "", False
    last_sep = -1  # index of the last "," or opening bracket outside any string
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str, quote = True, ch
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
            last_sep = i
        elif ch in "}]" and stack:
            stack.pop()
        elif ch == ",":
            last_sep = i
    body = text
    if in_str:  # cut inside a string: drop that whole key/value
        body = text[:last_sep + 1] if text[last_sep:last_sep + 1] in "{[" else text[:last_sep]
    body = re.sub(r"[,:]\s*$", "", body.rstrip())
    body = re.sub(r",?\s*\"[^\"]*\"\s*$", "", body) if body.rstrip().endswith('"') and re.search(r'[{,]\s*"[^"]*"\s*$', body) else body
    return body + "".join(reversed(stack))


def _tidy(text: str) -> str:
    text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S) if not re.search(r"https?://", text) else text
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b", "null", text)
    return text


def _single_to_double(text: str) -> str:
    """'key': 'value' -> "key": "value" (only when there are no double-quoted strings to confuse)."""
    if '"' in text:
        return text
    return re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: json.dumps(m.group(1).replace("\\'", "'")), text)


def loads_lenient(text: str, want: type = dict):
    """The first JSON object (or array when want=list) in `text`, repaired if needed; None if hopeless."""
    if not text:
        return None
    candidates = [text] + [m.group(1) for m in FENCE.finditer(text)]
    opener = "{" if want is dict else "["
    for cand in candidates:
        piece = _first_balanced(cand, opener)
        if piece is None:
            continue
        for attempt in (piece, _tidy(piece), _single_to_double(_tidy(piece)), _close_truncated(_single_to_double(_tidy(piece)))):
            try:
                value = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, want):
                return value
    return None
