"""schema.org JSON-LD extractor (Product/Service offers, AggregateOffer)."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import BeautifulSoup

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract.models import Package
from hosting_scraper.extract.normalize import annotate_category, build_package
from hosting_scraper.util.text import clean_text, parse_billing_period, truncate

PRODUCT_TYPES = {
    "product",
    "service",
    "softwareapplication",
    "offer",
    "aggregateoffer",
    "webhosting",
}
ISO_DURATION_RE = re.compile(r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<days>\d+)D)?$", re.I)


def parse_json_ld(html: str) -> list[dict[str, Any]]:
    """Return all JSON-LD objects from a page, expanding ``@graph``."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    objects: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            try:
                payload = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        objects.extend(_flatten(payload))
    return objects


def _flatten(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        out: list[dict[str, Any]] = []
        for item in payload:
            out.extend(_flatten(item))
        return out
    if not isinstance(payload, dict):
        return []
    out = [payload]
    graph = payload.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            if isinstance(item, dict):
                out.extend(_flatten(item))
    return out


def _types(obj: dict[str, Any]) -> set[str]:
    raw = obj.get("@type") or obj.get("type") or ""
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [str(item) for item in raw]
    else:
        values = []
    return {value.rsplit("/", 1)[-1].lower() for value in values}


def _duration_months(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        value = value.get("duration") or value.get("@value") or value.get("name")
        return _duration_months(value)
    text = clean_text(str(value))
    match = ISO_DURATION_RE.match(text.upper())
    if match:
        years = int(match.group("years") or 0)
        months = int(match.group("months") or 0)
        days = int(match.group("days") or 0)
        total = years * 12 + months
        if not total and days:
            total = max(1, round(days / 30))
        return total or None
    return None


def _period_from_offer(offer: dict[str, Any], fallback_text: str) -> tuple[str | None, int | None]:
    specs = offer.get("priceSpecification")
    spec_list = specs if isinstance(specs, list) else [specs] if isinstance(specs, dict) else []
    for spec in spec_list:
        if not isinstance(spec, dict):
            continue
        for key in ("billingDuration", "duration", "billingIncrement"):
            months = _duration_months(spec.get(key))
            if months:
                return ("year" if months % 12 == 0 and months >= 12 else "month", months)
        unit = clean_text(
            str(spec.get("unitText") or spec.get("unitCode") or spec.get("name") or "")
        )
        period, months = parse_billing_period(unit)
        if period:
            return period, months
    for key in ("billingPeriod", "billingDuration", "billingIncrement", "duration"):
        months = _duration_months(offer.get(key))
        if months:
            return ("year" if months % 12 == 0 and months >= 12 else "month", months)
    period, months = parse_billing_period(fallback_text)
    return period, months


def _offer_amount(offer: dict[str, Any]) -> tuple[str, str] | None:
    price = offer.get("price")
    if isinstance(price, dict):
        price = price.get("value") or price.get("@value") or price.get("price")
    if price is None and isinstance(offer.get("priceSpecification"), dict):
        price = offer["priceSpecification"].get("price")
    if price is None:
        return None
    currency = clean_text(
        str(offer.get("priceCurrency") or offer.get("currency") or "")
    )
    return clean_text(str(price)), currency


def _feature_list(obj: dict[str, Any]) -> list[str]:
    features: list[str] = []
    props = obj.get("additionalProperty")
    if isinstance(props, list):
        for prop in props:
            if isinstance(prop, dict):
                name = clean_text(str(prop.get("name") or ""))
                value = clean_text(str(prop.get("value") or ""))
                if name and value:
                    features.append(f"{name}: {value}")
                elif value:
                    features.append(value)
            elif isinstance(prop, str):
                features.append(prop)
    for key in ("featureList", "features"):
        value = obj.get(key)
        if isinstance(value, list):
            features.extend(clean_text(str(item)) for item in value if item)
        elif isinstance(value, str):
            features.extend(part.strip() for part in re.split(r"[,;|]", value) if part.strip())
    return features


def _iter_offers(product: dict[str, Any]) -> list[dict[str, Any]]:
    offers = product.get("offers")
    if isinstance(offers, list):
        return [item for item in offers if isinstance(item, dict)]
    if isinstance(offers, dict):
        return [offers]
    return []


def extract_schema_packages(
    html: str,
    page_url: str,
    domain: str,
    config: ExtractionConfig,
) -> list[Package]:
    packages: list[Package] = []
    for obj in parse_json_ld(html):
        types = _types(obj)
        if not types & PRODUCT_TYPES:
            continue
        name = clean_text(str(obj.get("name") or obj.get("headline") or ""))
        description = truncate(clean_text(str(obj.get("description") or "")), 500)
        features = _feature_list(obj)
        offers = _iter_offers(obj)
        if not offers and types & {"offer", "aggregateoffer"}:
            offers = [obj]
        if not offers:
            continue
        for offer in offers:
            offer_types = _types(offer)
            if "aggregateoffer" in offer_types:
                low = offer.get("lowPrice")
                if low is None:
                    continue
                currency = clean_text(str(offer.get("priceCurrency") or ""))
                price_text = f"{low} {currency}".strip()
                period_text = " ".join(
                    [
                        name,
                        description,
                        clean_text(str(offer.get("name") or "")),
                        clean_text(str(offer.get("description") or "")),
                    ]
                )
                package = build_package(
                    name=f"{name} (from)" if name else "",
                    price_text=price_text,
                    context_text=period_text,
                    page_url=page_url,
                    domain=domain,
                    extractor="schema_org",
                    config=config,
                    features=features,
                    description=description,
                    confidence=0.72,
                    evidence=json.dumps(offer, ensure_ascii=False)[:400],
                )
                if package:
                    packages.append(annotate_category(package, page_url))
                continue
            amount = _offer_amount(offer)
            if amount is None:
                continue
            raw_amount, currency = amount
            if not currency:
                currency = clean_text(
                    str(obj.get("priceCurrency") or obj.get("currency") or "")
                )
            offer_name = clean_text(str(offer.get("name") or ""))
            offer_desc = clean_text(str(offer.get("description") or ""))
            context = " ".join([offer_name, offer_desc, name, description])
            period, months = _period_from_offer(offer, context)
            price_text = f"{raw_amount} {currency}".strip()
            confidence = 0.9 if (currency and period) else 0.82 if currency else 0.72
            package = build_package(
                name=name or offer_name,
                price_text=price_text,
                context_text=context,
                page_url=page_url,
                domain=domain,
                extractor="schema_org",
                config=config,
                features=features,
                description=description or offer_desc,
                confidence=confidence,
                evidence=json.dumps({"product": name, "offer": offer}, ensure_ascii=False)[:400],
                strong_container=True,
            )
            if package:
                if period:
                    package.billing_period = period
                    package.billing_months = months
                    _recompute(package)
                packages.append(annotate_category(package, page_url))
    return _dedupe(packages, config.max_packages_per_page)


def _recompute(package: Package) -> None:
    from hosting_scraper.extract.normalize import _compute_monthly, _compute_yearly

    package.monthly_price = _compute_monthly(package)
    package.yearly_price = _compute_yearly(package)


def _dedupe(packages: list[Package], limit: int) -> list[Package]:
    best: dict[str, Package] = {}
    for package in packages:
        key = package.dedupe_key()
        current = best.get(key)
        if current is None or package.confidence > current.confidence:
            best[key] = package
    return list(best.values())[:limit]


def _price_number(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
