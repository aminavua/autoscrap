"""Normalization helpers: price parsing, package construction, dedupe keys."""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract.confidence import score_package
from hosting_scraper.extract.models import Package
from hosting_scraper.util.text import (
    clean_text,
    contains_any,
    parse_billing_period,
    parse_capacity,
    parse_price,
    truncate,
)
from hosting_scraper.util.url import host_of, same_site

GENERIC_NAMES = {
    "pricing",
    "price",
    "cost",
    "plans",
    "pricing plans",
    "hosting",
    "web hosting",
    "features",
    "compare plans",
    "choose your plan",
    "choose a plan",
    "get started",
    "buy now",
    "sign up",
    "learn more",
    "most popular",
    "best value",
    "recommended",
    "popular",
    "save",
    "sale",
    "setup",
    "setup fee",
    "one-time setup",
    "one time setup",
    "installation",
    "installation fee",
    "activation fee",
    "domain registration",
    "renewal",
}


def clean_name(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"\b(?:from|starting at|only)\b", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -–—:|•")
    return truncate(value, 120)


def is_generic_name(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", (value or "").lower()).strip(" :•-–—")
    return not normalized or normalized in GENERIC_NAMES or len(normalized) < 2


def dedupe_features(features: Iterable[str], *, limit: int = 20) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for feature in features:
        text = truncate(clean_text(feature), 180)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def appear_monthly(page_url: str, context: str, config: ExtractionConfig) -> tuple[str | None, int | None]:
    period, months = parse_billing_period(context)
    if period:
        return period, months
    if config.normalize.default_billing_period == "unknown":
        return None, None
    return config.normalize.default_billing_period, (
        1 if config.normalize.default_billing_period == "month" else 12
    )


def build_package(
    *,
    name: str,
    price_text: str,
    context_text: str,
    page_url: str,
    domain: str,
    extractor: str,
    config: ExtractionConfig,
    features: Iterable[str] = (),
    description: str = "",
    confidence: float | None = None,
    evidence: str = "",
    strong_container: bool = False,
) -> Package | None:
    """Build a normalized package from a raw name/price snippet.

    Returns ``None`` for snippets that do not contain a usable price.
    """
    name = clean_name(name)
    if is_generic_name(name):
        return None
    context_text = clean_text(context_text)
    price_text = clean_text(price_text)
    combined = f"{price_text} {context_text}".strip()
    if not combined:
        return None
    free_text = bool(re.search(r"\bfree\b", price_text, re.I))
    parsed = parse_price(price_text, default_currency=config.currency_default)
    free = parsed is None and free_text
    if parsed is None and not free:
        parsed = parse_price(combined, default_currency=config.currency_default)
    if parsed is not None and parsed.amount == 0 and free_text:
        free = True
    if parsed is None and not free:
        return None
    if free:
        amount = Decimal("0")
        currency = parsed.currency if parsed else config.currency_default
        raw = price_text or "Free"
    else:
        assert parsed is not None
        amount = parsed.amount
        currency = parsed.currency or config.currency_default
        raw = parsed.raw
    period, months = parse_billing_period(price_text)
    if not period:
        period, months = parse_billing_period(context_text[:400])
    if not period:
        period = _custom_period(
            price_text + " " + context_text[:400],
            config.normalize.monthly_tokens,
            config.normalize.yearly_tokens,
        )
        months = 1 if period == "month" else 12 if period == "year" else None
    if not period and config.normalize.infer_billing_term_months:
        period = config.normalize.default_billing_period
        months = 1 if period == "month" else 12 if period == "year" else None
    if period == "month":
        months = months or 1
    elif period == "year":
        months = months or 12
    features = dedupe_features(features)
    feature_text = " | ".join(features)
    capacities = parse_capacity(feature_text + " " + context_text[:1200])
    is_free = free or amount == Decimal("0")
    if confidence is None:
        confidence = score_package(
            extractor,
            has_name=bool(name),
            has_price=not is_free or amount == 0,
            has_currency=bool(currency),
            has_period=bool(period),
            has_features=bool(features),
            strong_container=strong_container,
            has_description=bool(description),
        )
    package = Package(
        name=name,
        source_domain=domain,
        page_url=page_url,
        price_raw=raw,
        price_amount=amount,
        currency=currency,
        billing_period=period or "unknown",
        billing_months=months,
        is_free=is_free,
        description=truncate(description, 500),
        features=features,
        storage=capacities.get("disk", ""),
        disk_type=capacities.get("disk_type", ""),
        ram=capacities.get("ram", ""),
        bandwidth=capacities.get("bandwidth", ""),
        extractor=extractor,
        confidence=round(confidence, 3),
        evidence=truncate(evidence or combined, 400),
    )
    package.monthly_price = _compute_monthly(package)
    package.yearly_price = _compute_yearly(package)
    return package


def _custom_period(text: str, monthly_tokens: list[str], yearly_tokens: list[str]) -> str | None:
    lowered = text.lower()
    if any(token.lower() in lowered for token in yearly_tokens):
        return "year"
    if any(token.lower() in lowered for token in monthly_tokens):
        return "month"
    return None


def _compute_monthly(package: Package) -> Decimal | None:
    amount = package.price_amount
    if amount is None:
        return None
    if package.is_free:
        return Decimal("0")
    period = package.billing_period
    if period == "month":
        return amount
    if period == "year":
        return (amount / Decimal(12)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if package.billing_months:
        return (amount / Decimal(package.billing_months)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    return None


def _compute_yearly(package: Package) -> Decimal | None:
    amount = package.price_amount
    if amount is None:
        return None
    if package.is_free:
        return Decimal("0")
    if package.billing_period == "year":
        return amount
    monthly = package.monthly_price
    if monthly is not None:
        return (monthly * Decimal(12)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return None


def normal_currency(value: str | None, default: str = "USD") -> str:
    if not value:
        return default
    from hosting_scraper.util.text import CURRENCY_BY_SYMBOL

    value = clean_text(value)
    code = value.upper()
    if len(code) == 3 and code.isalpha():
        return code
    return CURRENCY_BY_SYMBOL.get(value, value if len(value) == 3 else default)


def domain_of(url: str) -> str:
    return host_of(url)


def annotate_category(package: Package, page_url: str) -> Package:
    """Best-effort category from the URL path (vps, dedicated, reseller, ...)."""
    path = page_url.lower()
    for category, tokens in {
        "vps": ("vps", "virtual-private", "cloud-vps"),
        "dedicated": ("dedicated", "bare-metal", "baremetal"),
        "reseller": ("reseller",),
        "wordpress": ("wordpress", "wp-hosting"),
        "cloud": ("cloud",),
        "shared": ("shared", "web-hosting", "webhosting"),
        "email": ("email-hosting",),
        "domain": ("domain",),
    }.items():
        if any(token in path for token in tokens):
            package.category = category
            break
    return package


def is_same_site_url(url: str, domain: str) -> bool:
    return same_site(url, domain)


def contains_price_signal(text: str) -> bool:
    if not text:
        return False
    if re.search(r"(?:[$€£₹¥₦₽]|USD|EUR|GBP)\s*\d", text, re.I):
        return True
    return bool(contains_any(text, ["/mo", "per month", "monthly", "/yr", "per year"]))
