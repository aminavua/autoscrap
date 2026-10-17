"""Price-card extractor for modern pricing grids.

The heuristic walks up from every visible price snippet to the nearest element
that looks like a plan card, then pulls the heading, features and cadence from
that card. It intentionally skips tables (handled by ``dom_tables``).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from bs4.element import Tag

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract.dom_utils import (
    feature_list,
    has_price_signal,
    heading_text,
    is_struck,
)
from hosting_scraper.extract.models import Package
from hosting_scraper.extract.normalize import annotate_category, build_package
from hosting_scraper.util.text import clean_text, parse_billing_period

CARD_CLASS_RE = re.compile(
    r"(?:^|[-_ ])(?:card|pricing|price|plan|package|tier|product)(?:$|[-_ ])", re.I
)
SETUP_RE = re.compile(r"setup|activation|installation|domain|renewal", re.I)
SIZE_LIMIT = 2800


def extract_card_packages(
    html: str,
    page_url: str,
    domain: str,
    config: ExtractionConfig,
) -> list[Package]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    page_h1 = ""
    h1 = soup.find("h1")
    if h1:
        page_h1 = clean_text(h1.get_text(" "))

    price_nodes: list[tuple[Tag, str]] = []
    for tag in soup.find_all(["span", "div", "strong", "b", "p", "h3", "h4", "h5", "a", "button", "em"]):
        if tag.find(["script", "style"]):
            continue
        if is_struck(tag):
            continue
        text = clean_text(tag.get_text(" "))
        if not text or len(text) > 90:
            continue
        if not has_price_signal(text):
            continue
        # Keep the innermost node; parents would duplicate the same price or
        # produce a card-wide "price" that also contains features.
        if any(
            has_price_signal(clean_text(descendant.get_text(" ")))
            for descendant in tag.find_all(True)
        ):
            continue
        price_nodes.append((tag, text))

    grouped: dict[int, tuple[Tag, list[tuple[Tag, str]]]] = {}
    for tag, text in price_nodes:
        container = _choose_container(tag, page_h1)
        if container is None:
            continue
        key = id(container)
        if key not in grouped:
            grouped[key] = (container, [])
        grouped[key][1].append((tag, text))

    packages: list[Package] = []
    for container, nodes in grouped.values():
        name = heading_text(container)
        if not name:
            continue
        if name == page_h1 and len(clean_text(container.get_text(" "))) > 1600:
            continue
        price_text = _choose_price(nodes, container)
        if not price_text:
            continue
        row_text = clean_text(container.get_text(" "))
        features = feature_list(container)
        description = _description(container, name)
        strong = bool(_card_classes(container))
        package = build_package(
            name=name,
            price_text=price_text,
            context_text=row_text[:1200],
            page_url=page_url,
            domain=domain,
            extractor="dom_cards",
            config=config,
            features=features,
            description=description,
            evidence=row_text[:400],
            strong_container=strong,
        )
        if package:
            packages.append(annotate_category(package, page_url))
    return _dedupe(packages, config.max_packages_per_page)


def _choose_container(tag: Tag, page_h1: str) -> Tag | None:
    current: Tag | None = tag
    for _depth in range(7):
        if not isinstance(current, Tag) or current.name in {"body", "html", "[document]"}:
            return None
        if current.name == "table" or current.find_parent("table"):
            current = current.parent if isinstance(current.parent, Tag) else None
            continue
        text = clean_text(current.get_text(" "))
        if len(text) < 10:
            current = current.parent if isinstance(current.parent, Tag) else None
            continue
        if len(text) > SIZE_LIMIT:
            return None
        if current.name in {"a", "button"}:
            current = current.parent if isinstance(current.parent, Tag) else None
            continue
        heading = heading_text(current)
        class_hint = bool(_card_classes(current))
        price_count = _price_count(current)
        if heading and heading != page_h1:
            if price_count <= 2:
                return current
        elif class_hint and price_count == 1 and len(text) <= 1800:
            return current
        if heading and heading != page_h1 and price_count > 2:
            # Grid wrapper: continue climbing would be worse, but this is not a card.
            return None
        current = current.parent if isinstance(current.parent, Tag) else None
    return None


def _price_count(container: Tag) -> int:
    count = 0
    for tag in container.find_all(["span", "div", "strong", "b", "p", "h3", "h4", "h5", "a", "button"]):
        if is_struck(tag):
            continue
        text = clean_text(tag.get_text(" "))
        if text and len(text) <= 90 and has_price_signal(text):
            count += 1
    return count


def _card_classes(container: Tag) -> str:
    classes = " ".join(container.get("class") or [])
    if CARD_CLASS_RE.search(classes):
        return classes
    parent = container.parent
    if isinstance(parent, Tag):
        parent_classes = " ".join(parent.get("class") or [])
        if CARD_CLASS_RE.search(parent_classes):
            return parent_classes
    return ""


def _choose_price(nodes: list[tuple[Tag, str]], container: Tag) -> str:
    scored: list[tuple[float, str]] = []
    for tag, text in nodes:
        context = clean_text(tag.parent.get_text(" ")) if isinstance(tag.parent, Tag) else ""
        if SETUP_RE.search(context[:60]):
            continue
        if any(is_struck(ancestor) for ancestor in list(tag.parents)[:3] if isinstance(ancestor, Tag)):
            continue
        period, _months = parse_billing_period(text)
        score = 0.0
        if period == "month":
            score += 3.0
        elif period == "year":
            score += 1.0
        if "$" in text or "€" in text or "£" in text or "USD" in text.upper():
            score += 1.5
        if re.search(r"\d[.,]\d{2}", text):
            score += 0.5
        if re.search(r"old|was|regular", text, re.I):
            score -= 3.0
        scored.append((score, text))
    if not scored:
        return ""
    scored.sort(key=lambda item: -item[0])
    return scored[0][1]


def _description(container: Tag, name: str) -> str:
    for tag in container.find_all(["p", "div"], limit=12):
        if tag.find(["p", "div", "ul", "li"]):
            continue
        text = clean_text(tag.get_text(" "))
        if 15 <= len(text) <= 240 and text != name and not has_price_signal(text):
            return text
    return ""


def _dedupe(packages: list[Package], limit: int) -> list[Package]:
    best: dict[str, Package] = {}
    for package in packages:
        key = package.dedupe_key()
        current = best.get(key)
        if current is None or package.confidence > current.confidence:
            best[key] = package
    return list(best.values())[:limit]
