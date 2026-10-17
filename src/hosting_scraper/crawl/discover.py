"""Pricing-page discovery: seed paths, link ranking and page scoring."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from hosting_scraper.config import DiscoveryConfig
from hosting_scraper.extract.models import Candidate
from hosting_scraper.util.text import clean_text
from hosting_scraper.util.url import (
    absolutize,
    looks_non_pricing,
    origin,
    path_of,
    same_site,
)

PRICE_HINT_RE = re.compile(r"(?:[$€£₹¥₦₽]|USD|EUR|GBP)\s?\d|\d\s?(?:USD|EUR|GBP)", re.I)
OFFER_TYPE_RE = re.compile(r'"@type"\s*:\s*"(?:Offer|Product|AggregateOffer|Service)"', re.I)
BAD_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".svg",
    ".gif",
    ".zip",
    ".mp4",
    ".css",
    ".js",
    ".xml",
)
LOCALE_PREFIXES = {
    "en",
    "en-us",
    "en-gb",
    "de",
    "fr",
    "es",
    "it",
    "nl",
    "pt",
    "pl",
    "ro",
    "ru",
    "tr",
    "sv",
    "da",
    "no",
    "fi",
    "cs",
    "sk",
    "hu",
    "bg",
    "el",
    "uk",
    "ar",
    "he",
    "hi",
    "id",
    "ja",
    "ko",
    "th",
    "vi",
    "zh",
    "zh-cn",
    "zh-tw",
}


@dataclass
class LinkSuggestion:
    url: str
    anchor: str
    score: float


def _path_key(url: str) -> str:
    path = path_of(url).lower()
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if segments and segments[0] in LOCALE_PREFIXES:
        segments = segments[1:]
    return "/" + "/".join(segments)


def url_score(url: str, anchor: str, config: DiscoveryConfig) -> tuple[float, str]:
    path = path_of(url)
    path_lower = path.lower()
    score = 0.0
    reasons: list[str] = []
    if looks_non_pricing(url):
        return -10.0, "non-pricing path"
    for keyword in config.keywords.url:
        needle = keyword.lower()
        if needle in path_lower:
            score += 3.0 if needle in {"pricing", "plans", "plan"} else 1.5
            reasons.append(f"url:{needle}")
    anchor_lower = (anchor or "").lower()
    for keyword in config.keywords.anchor_text:
        if keyword.lower() in anchor_lower:
            score += 1.5
            reasons.append(f"anchor:{keyword}")
    if path_lower in {"/pricing", "/plans", "/pricing/", "/plans/"}:
        score += 2.0
        reasons.append("canonical pricing path")
    segments = [s for s in path_lower.strip("/").split("/") if s]
    if len(segments) > 4:
        score -= 2.0
        reasons.append("deep path")
    if len(segments) == 0:
        score += 0.5
    query = urlsplit(url).query.lower()
    if any(token in query for token in ("search", "filter", "sort", "page=", "tag=")):
        score -= 2.0
        reasons.append("faceted URL")
    return score, ", ".join(reasons)


def extract_links(
    html: str,
    page_url: str,
    domain: str,
    config: DiscoveryConfig,
    *,
    limit: int | None = None,
) -> list[LinkSuggestion]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    suggestions: dict[str, LinkSuggestion] = {}
    for anchor_tag in soup.find_all("a", href=True):
        href = str(anchor_tag.get("href") or "")
        url = absolutize(page_url, href)
        if not url or not same_site(url, domain):
            continue
        path = path_of(url).lower()
        if path.endswith(BAD_EXTENSIONS):
            continue
        anchor = clean_text(anchor_tag.get_text(" "))[:160]
        score, reason = url_score(url, anchor, config)
        if score < 1.0:
            continue
        existing = suggestions.get(url)
        if existing is None or score > existing.score:
            suggestions[url] = LinkSuggestion(url=url, anchor=anchor, score=min(score, 12.0))
    links = sorted(suggestions.values(), key=lambda item: (-item.score, len(item.url)))
    # Collapse locale variants that share the same path suffix.
    collapsed: list[LinkSuggestion] = []
    seen_keys: set[str] = set()
    for link in links:
        key = _path_key(link.url)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        collapsed.append(link)
        if limit and len(collapsed) >= limit:
            break
    return collapsed


def common_path_candidates(
    seed_url: str, final_url: str, config: DiscoveryConfig
) -> list[Candidate]:
    base = origin(final_url or seed_url)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for path in config.common_paths:
        url = absolutize(base + "/", path)
        if not url or url in seen:
            continue
        seen.add(url)
        score, reason = url_score(url, "", config)
        candidates.append(
            Candidate(url=url, score=score, reason=reason or "seed path", from_seed_path=True)
        )
    return candidates


def page_pricing_score(html: str, url: str, config: DiscoveryConfig) -> float:
    """Score how price-like a fetched page is (higher is better)."""
    if not html:
        return 0.0
    soup = BeautifulSoup(html, "lxml")
    title = clean_text(soup.title.get_text(" ")) if soup.title else ""
    headings = " ".join(
        clean_text(tag.get_text(" ")) for tag in soup.find_all(["h1", "h2"], limit=8)
    )
    text = soup.get_text(" ")
    text_lower = text.lower()
    score = 0.0
    if any(word in (title + " " + headings).lower() for word in ("pricing", "plans", "packages")):
        score += 3.0
    if any(word in title.lower() for word in ("hosting", "vps", "cloud", "server")):
        score += 1.0
    prices = len(PRICE_HINT_RE.findall(text[:500_000]))
    score += min(prices, 12) * 0.6
    symbol_hits = sum(text[:500_000].count(symbol) for symbol in config.page_signals.currency_symbols)
    score += min(symbol_hits, 10) * 0.15
    periods = sum(text_lower.count(token.lower()) for token in config.page_signals.period_tokens)
    score += min(periods, 10) * 0.8
    if OFFER_TYPE_RE.search(html):
        score += 3.0
    if "pricing" in path_of(url):
        score += 1.5
    if any(word in text_lower for word in ("404", "page not found", "not found")):
        if len(text) < 3000:
            score -= 4.0
    if any(word in text_lower for word in ("sign in", "log in", "create your account")):
        if len(text) < 2500:
            score -= 2.0
    return round(score, 2)


def parse_json_ld(html: str) -> list[dict]:
    """Return parsed JSON-LD objects from a page (best effort)."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    objects: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Common broken variant: trailing commas or multiple objects.
            try:
                payload = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        if isinstance(payload, list):
            objects.extend(item for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict):
            if "@graph" in payload and isinstance(payload["@graph"], list):
                objects.extend(item for item in payload["@graph"] if isinstance(item, dict))
            objects.append(payload)
    return objects


def select_candidates(
    candidates: list[Candidate],
    *,
    limit: int,
    visited: set[str] | None = None,
    min_score: float = 1.0,
) -> list[Candidate]:
    visited = visited or set()
    ordered = sorted(candidates, key=lambda item: (-item.score, len(item.url)))
    selected: list[Candidate] = []
    seen_keys: set[str] = set()
    for candidate in ordered:
        if candidate.url in visited or candidate.url in seen_keys:
            continue
        if candidate.score < min_score:
            continue
        key = _path_key(candidate.url)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected
