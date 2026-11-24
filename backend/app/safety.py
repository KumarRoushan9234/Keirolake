"""Safety notices for questions where a wrong answer can hurt someone.

A question is tagged medical / chemical / electrical / fire by keywords. Tagged
questions get a visible notice, and the model is told to use only sourced
numbers and to say plainly when the sources don't cover a dose, ratio or rating.
"""
from __future__ import annotations

import re

CATEGORIES: dict[str, re.Pattern] = {
    "medical": re.compile(
        r"\b(dose|dosage|mg|ml|overdose|poison\w*|antibiotic\w*|medicine|medication|drug\w*|infection|wound|bleed\w*|"
        r"tourniquet|cpr|fracture|burn\w*|fever|allerg\w*|insulin|diabet\w*|pregnan\w*|symptom\w*|first aid|"
        r"heart attack|stroke|choking|snake ?bite|hypotherm\w*|heat ?stroke|dehydrat\w*|sepsis|seizure)\b", re.I),
    "chemical": re.compile(
        r"\b(bleach|chlorine|ammonia|acid|solvent|pesticide\w*|toxic|fumes?|carbon monoxide|mix(?:ing)? chemicals?|"
        r"disinfect\w*|iodine|purif\w+ water|water purification|ppm|concentration)\b", re.I),
    "electrical": re.compile(
        r"\b(wiring|voltage|amps?|circuit breaker|generator|electrocut\w*|mains|live wire|solar panel|battery bank|"
        r"inverter|short circuit|extension cord)\b", re.I),
    "fire": re.compile(r"\b(gas leak|propane|house fire|wildfire|fire extinguisher|natural gas|explosi\w+|flammable)\b", re.I),
}

NOTICES = {
    "medical": "Medical question: use this as first-aid guidance only. Doses and treatments must come from the cited sources; call emergency services when in doubt.",
    "chemical": "Chemical safety: ratios and concentrations matter. Only trust amounts shown in a cited source, and never mix cleaning chemicals.",
    "electrical": "Electrical safety: mistakes can be fatal. Check ratings in a cited source and use a qualified electrician for mains work.",
    "fire": "Fire and gas safety: get to safety and call emergency services first; follow cited instructions only.",
}

MODEL_RULE = (
    "This is a safety-critical question ({category}). Give only numbers (doses, ratios, times, temperatures, ratings) that "
    "appear in the sources, each with its tag. If the sources don't state a number, say so instead of estimating. "
    "Lead with the most urgent action, and mention when to get professional or emergency help."
)


def classify(question: str) -> str | None:
    """The first matching safety category, or None."""
    for category, pattern in CATEGORIES.items():
        if pattern.search(question):
            return category
    return None


def notice(category: str) -> str:
    return NOTICES[category]


def model_rule(category: str) -> str:
    return MODEL_RULE.format(category=category)
