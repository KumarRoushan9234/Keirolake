"""Which kind of task is this, and which model slot should do it?

Slots (Settings → Models): everyday (the default), hard, research, coding. Each holds one "provider:model" chosen by the
user from the live catalogue. An empty slot changes nothing: the normal local-first order is used. Vault questions
never use a slot that isn't on this computer.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

SLOTS = ("everyday", "hard", "research", "coding")
SLOT_LABELS = {
    "everyday": "Everyday",
    "hard": "Hard tasks",
    "research": "Research and reports",
    "coding": "Coding",
}
SLOT_HELP = {
    "everyday": "Quick answers, small talk, lookups. A fast local model is usually right.",
    "hard": "Analysis, comparisons, planning and anything that needs careful reasoning.",
    "research": "Deep research, long reports and the daily brief: big context, strong writing.",
    "coding": "Writing and fixing code, scripts and SQL.",
}
ENV = {slot: f"MODEL_SLOT_{slot.upper()}" for slot in SLOTS}


@dataclass
class Task:
    slot: str
    reason: str


_CODE = re.compile(
    r"(```|\b(write|fix|debug|refactor|implement|review)\b.{0,40}\b(code|function|script|class|query|regex|program|bug)\b|"
    r"\b(python|javascript|typescript|sql|bash|powershell|rust|golang|java|c\+\+)\b.{0,30}\b(code|script|function|snippet)\b|"
    r"\btraceback\b|\bstack trace\b|\bsyntax error\b)", re.I | re.S)
_HARD = re.compile(
    r"\b(analy[sz]e|analysis|compare|comparison|versus|vs\.?|evaluate|assess|forecast|predict|strategy|trade-?offs?|"
    r"pros and cons|step[- ]by[- ]step|in detail|detailed|thorough|comprehensive|full report|deep dive|root cause|"
    r"explain why|why (?:did|does|is|are|do)|plan for|recommend|prioriti[sz]e|summari[sz]e (?:all|these|the (?:last|past))|"
    r"implications?|risks? and|reason(?:ing)? about)\b", re.I)
_REPORT = re.compile(r"\b(report|briefing|brief|dossier|white ?paper|market review|outlook)\b", re.I)


def slot_value(slot: str) -> str:
    return os.getenv(ENV.get(slot, ""), "").strip()


def classify(question: str, *, depth: str = "answer", override: str = "") -> Task:
    """Rules first, so it costs nothing and never fails. An explicit override (the user's choice) wins."""
    if override in SLOTS:
        return Task(override, f"You chose {SLOT_LABELS[override]}")
    if depth in ("quick", "standard", "deep"):
        return Task("research", f"{depth.capitalize()} research")
    q = question.strip()
    if _CODE.search(q):
        return Task("coding", "A coding task")
    if _REPORT.search(q) and len(q.split()) >= 5:
        return Task("research", "A report")
    if _HARD.search(q) or len(q.split()) >= 60:
        return Task("hard", "Needs careful reasoning" if _HARD.search(q) else "A long, detailed request")
    return Task("everyday", "A quick question")


def parse(value: str) -> tuple[str, str] | None:
    """"provider:model" (the model may itself contain ':', as Ollama tags do)."""
    if ":" not in value:
        return None
    provider, model = value.split(":", 1)
    return (provider, model) if provider and model else None
