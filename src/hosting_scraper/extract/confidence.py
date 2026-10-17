"""Confidence scoring shared by extractors."""

from __future__ import annotations

EXTRACTOR_BASE = {
    "schema_org": 0.78,
    "dom_tables": 0.62,
    "dom_cards": 0.55,
    "xhr_json": 0.5,
}


def clamp(value: float, low: float = 0.0, high: float = 0.99) -> float:
    return max(low, min(high, value))


def score_package(
    extractor: str,
    *,
    has_name: bool = True,
    has_price: bool = True,
    has_currency: bool = False,
    has_period: bool = False,
    has_features: bool = False,
    strong_container: bool = False,
    has_description: bool = False,
) -> float:
    """Heuristic confidence that a DOM/JSON record is a real hosting package."""
    score = EXTRACTOR_BASE.get(extractor, 0.45)
    if not has_name:
        score -= 0.25
    if not has_price:
        score -= 0.4
    score += 0.06 if has_currency else -0.06
    score += 0.05 if has_period else -0.04
    score += 0.08 if has_features else 0.0
    score += 0.06 if strong_container else 0.0
    score += 0.03 if has_description else 0.0
    return round(clamp(score), 3)
