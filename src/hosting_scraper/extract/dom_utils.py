"""Shared helpers for DOM-based extractors."""

from __future__ import annotations

import re

from bs4 import Tag

from hosting_scraper.util.text import clean_text

PRICE_HINT_RE = re.compile(
    r"(?:US\$|C\$|A\$|R\$|[$€£₹¥₦₽₺₩]|USD|EUR|GBP|CHF)\s?\d"
    r"|\d[\d.,]*\s?(?:USD|EUR|GBP|CHF)\b"
    r"|\d[\d.,]*\s*(?:/|per\s+)(?:mo|month|yr|year)\b",
    re.I,
)
FREE_RE = re.compile(r"^\s*free(?:\s+(?:plan|tier|trial|forever))?\s*$", re.I)
STRUCK_CLASS_RE = re.compile(
    r"(?:^|[-_ ])(?:old|was|strike|strikethrough|crossed|regular|compare-at)(?:$|[-_ ])",
    re.I,
)
GENERIC_HEADING_RE = re.compile(
    r"^(?:pricing|plans?|pricing plans?|hosting|web hosting|features|compare(?: plans)?|"
    r"choose (?:your|a) plan|get started|most popular|best value|recommended)$",
    re.I,
)


def is_struck(node: Tag) -> bool:
    current: Tag | None = node
    while isinstance(current, Tag):
        if current.name in {"del", "s", "strike"}:
            return True
        classes = " ".join(current.get("class") or [])
        if classes and STRUCK_CLASS_RE.search(classes):
            return True
        if current.name in {"body", "html"}:
            break
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False


def strip_struck(scope: Tag) -> None:
    for tag in scope.find_all(["del", "s", "strike"]):
        tag.decompose()
    for tag in scope.find_all(True):
        classes = " ".join(tag.get("class") or [])
        if classes and STRUCK_CLASS_RE.search(classes):
            tag.decompose()


def has_price_signal(text: str) -> bool:
    return bool(PRICE_HINT_RE.search(text or "") or FREE_RE.match(text or ""))


def cell_text(cell: Tag) -> str:
    """Text of a table cell, ignoring struck-through (old) prices."""
    strip_struck(cell)
    return clean_text(cell.get_text(" "))


def heading_text(container: Tag, *, limit: int = 3) -> str:
    for tag in container.find_all(["h1", "h2", "h3", "h4", "h5"], limit=limit):
        text = clean_text(tag.get_text(" "))
        if text and not GENERIC_HEADING_RE.match(text):
            return text
    for selector in (
        "[class*='plan-name']",
        "[class*='planName']",
        "[class*='plan-title']",
        "[class*='planTitle']",
        "[class*='product-name']",
        "[class*='productName']",
        "[class*='package-name']",
        "[class*='packageName']",
        "[class*='title']",
    ):
        tag = container.select_one(selector)
        if tag:
            text = clean_text(tag.get_text(" "))
            if text and len(text) < 90 and not GENERIC_HEADING_RE.match(text):
                return text
    return ""


def feature_list(container: Tag, *, limit: int = 15) -> list[str]:
    features: list[str] = []
    for tag in container.find_all("li", limit=limit * 2):
        text = clean_text(tag.get_text(" "))
        if 2 <= len(text) <= 180:
            features.append(text)
    if not features:
        for selector in ("[class*='feature']", "[class*='benefit']"):
            for tag in container.select(selector)[:limit]:
                text = clean_text(tag.get_text(" "))
                if 2 <= len(text) <= 180:
                    features.append(text)
    seen: set[str] = set()
    out: list[str] = []
    for feature in features:
        if feature.lower() in seen:
            continue
        seen.add(feature.lower())
        out.append(feature)
        if len(out) >= limit:
            break
    return out
