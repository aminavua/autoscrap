"""Page-level promotion, coupon and free-quota heuristics."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract.models import DirectOffer
from hosting_scraper.util.text import clean_text, truncate

DISCOUNT_RE = re.compile(
    r"(?:up to\s+)?\d{1,3}\s?%\s?(?:off|discount)"
    r"|\bsave\s+(?:up to\s+)?\d{1,3}\s?%"
    r"|\bblack friday\b|\bcyber monday\b|\bhalf[- ]price\b",
    re.I,
)
FREE_RE = re.compile(
    r"\bfree\s+(?:domain|ssl|certificate|migration|migrations|setup|cdn|email|backup|"
    r"backups|website|site|trial|month|hosting)\b",
    re.I,
)
GUARANTEE_RE = re.compile(r"\b\d{1,2}[- ]day\s+money[- ]back\b|\bmoney[- ]back guarantee\b", re.I)
COUPON_RE = re.compile(
    r"\b(?:promo\s*code|coupon\s*code|discount\s*code|with\s+code)\s*[:=-]?\s*"
    r"([A-Z0-9][A-Z0-9_-]{3,24})\b",
    re.I,
)
MAX_OFFERS = 8


def extract_page_offers(
    html: str,
    page_url: str,
    config: ExtractionConfig,
) -> list[DirectOffer]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    blocks = _visible_blocks(soup)
    if not blocks:
        return []
    sentences: list[str] = []
    seen_sentences: set[str] = set()
    for block in blocks:
        for sentence in _sentences(block):
            key = sentence.lower()
            if key not in seen_sentences:
                seen_sentences.add(key)
                sentences.append(sentence)
    offers: list[DirectOffer] = []
    seen: set[str] = set()
    for sentence in sentences:
        if len(sentence) > 320:
            sentence = truncate(sentence, 320)
        match_type = ""
        match_text = ""
        match = DISCOUNT_RE.search(sentence)
        if match:
            match_type = "promotion"
            match_text = match.group(0)
        else:
            match = FREE_RE.search(sentence)
            if match:
                match_type = "free_quota"
                match_text = match.group(0)
            else:
                match = GUARANTEE_RE.search(sentence)
                if match:
                    match_type = "money_back"
                    match_text = match.group(0)
        if not match_type:
            continue
        key = f"{match_type}:{match_text.lower()}"
        if key in seen:
            continue
        seen.add(key)
        coupon = ""
        coupon_match = COUPON_RE.search(sentence)
        if coupon_match:
            coupon = coupon_match.group(1)
            if not match_type:
                match_type = "coupon"
        title = sentence
        if len(title) > 120:
            title = truncate(title, 120)
        confidence = 0.5 if match_type == "promotion" else 0.4
        if coupon:
            confidence += 0.1
        offers.append(
            DirectOffer(
                title=title,
                description=truncate(sentence, 280),
                url=page_url,
                kind=match_type,
                discount_text=match_text,
                coupon_code=coupon,
                confidence=round(min(confidence, 0.85), 3),
                extractor="page_heuristics",
                evidence=sentence[:300],
            )
        )
        if len(offers) >= MAX_OFFERS:
            break
    return offers


def _visible_blocks(soup: BeautifulSoup) -> list[str]:
    block_tags = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "span", "strong", "em", "a", "button", "label", "div"}
    blocks: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all(block_tags):
        if tag.find(block_tags - {"a"}):
            continue
        text = clean_text(tag.get_text(" "))
        if not text or len(text) < 8:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        blocks.append(text)
    return blocks


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\s{2,}|\n+|\s+\|\s+", text)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        part = clean_text(part)
        if not part or len(part) < 8:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return out
