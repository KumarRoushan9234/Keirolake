"""Catch answers that make things up: placeholder companies, "simulated" reports and numbers no source contains.

Small local models sometimes ignore "don't invent facts" and write a sample report instead of saying the data isn't
there. This runs on the finished answer, so the user sees a clear warning and can re-run it on a stronger model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_PLACEHOLDER = re.compile(
    r"\b(?:(?:XYZ|ABC|PQR|LMN|DEF|ACME|Foo|Bar)\s+(?:Ltd|Limited|Inc|Corp|Corporation|Company|Industries|Holdings|"
    r"Enterprises|Group|Bank|Technologies)\b|Company\s+[A-Z]\b|Ticker(?:\s+Symbol)?:\s*(?:XYZ|ABC|PQR|LMN|DEF)\b)")
_MAKE_BELIEVE = re.compile(
    r"\b(simulat(?:e|ed|ing)\s+(?:a\s+)?(?:report|data|results?)|hypothetical\s+(?:data|figures|example|companies)|"
    r"for\s+illustration(?:\s+purposes)?|sample\s+(?:data|figures|report)|fictional|placeholder|"
    r"(?:i\s+)?don'?t\s+have\s+(?:direct\s+)?access\s+to\s+real[- ]time)\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])(?:[$₹€£]\s?)?\d[\d,]*(?:\.\d+)?\s?(?:%|crore|lakh|billion|million|bn|mn|cr)?", re.I)
_SOURCE_TAG = re.compile(r"\[(?:\d+|[WZPS]\d+)\]")


@dataclass
class Verdict:
    tripped: bool
    reasons: list[str] = field(default_factory=list)
    unsourced_numbers: list[str] = field(default_factory=list)

    def message(self) -> str:
        return "This answer may contain made-up information: " + "; ".join(self.reasons) + "."


def _numbers(text: str) -> set[str]:
    out = set()
    for m in _NUMBER.findall(text):
        n = re.sub(r"[^\d.]", "", m).strip(".")
        if n and len(n.replace(".", "")) >= 2 and not re.fullmatch(r"(19|20)\d\d", n):  # skip years and tiny numbers
            out.add(n.lstrip("0") or "0")
    return out


def check(answer: str, sources_text: str) -> Verdict:
    reasons: list[str] = []
    placeholders = sorted({m.group(0) for m in _PLACEHOLDER.finditer(answer)})
    if placeholders:
        reasons.append("placeholder names such as " + ", ".join(placeholders[:3]))
    if _MAKE_BELIEVE.search(answer):
        reasons.append("the model says the data is simulated or illustrative")
    unsourced: list[str] = []
    have = _numbers(sources_text)
    # Only figures in sentences without a citation count: cited ones are checked against their sources elsewhere.
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer):
        if _SOURCE_TAG.search(sentence):
            continue
        unsourced += [n for n in _numbers(sentence) if n not in have]
    unsourced = list(dict.fromkeys(unsourced))
    if len(unsourced) >= 6:
        reasons.append(f"{len(unsourced)} figures that appear in none of the sources")
    return Verdict(bool(reasons), reasons, unsourced[:20])


def relevance(text: str, query_terms: set[str]) -> float:
    """Share of the query's words that appear in the text (0 to 1)."""
    from app.engine.evidence import terms

    if not query_terms:
        return 1.0
    return len(query_terms & set(terms(text))) / len(query_terms)
