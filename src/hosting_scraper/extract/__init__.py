"""Extraction pipeline: run enabled extractors and merge their output."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract.dom_cards import extract_card_packages
from hosting_scraper.extract.dom_tables import extract_table_packages
from hosting_scraper.extract.models import ExtractionResult, FetchResult, Package
from hosting_scraper.extract.offers import extract_page_offers
from hosting_scraper.extract.schema_org import extract_schema_packages
from hosting_scraper.extract.xhr_capture import embedded_json_payloads, extract_xhr_packages

logger = logging.getLogger(__name__)


def extract_page(
    fetch: FetchResult,
    *,
    config: ExtractionConfig,
    domain: str,
) -> ExtractionResult:
    """Run all enabled extractors against one fetched page."""
    result = ExtractionResult()
    page_url = fetch.final_url or fetch.url
    html = fetch.html or ""
    extractors: dict[str, Callable[[], list[Package]]] = {
        "schema_org": lambda: extract_schema_packages(html, page_url, domain, config),
        "dom_tables": lambda: extract_table_packages(html, page_url, domain, config),
        "dom_cards": lambda: extract_card_packages(html, page_url, domain, config),
        "xhr_json": lambda: extract_xhr_packages(
            list(fetch.xhr_payloads) + embedded_json_payloads(html, page_url),
            page_url,
            domain,
            config,
        ),
    }
    packages: list[Package] = []
    for name in config.enabled_extractors:
        extractor = extractors.get(name)
        if extractor is None:  # guarded by config validation, kept for safety
            continue
        try:
            found = extractor()
        except Exception as exc:  # noqa: BLE001 - one bad page should not kill the run
            logger.debug("extractor %s failed for %s: %s", name, page_url, exc, exc_info=True)
            result.warnings.append(f"{name} failed: {type(exc).__name__}: {exc}")
            continue
        if found:
            logger.debug("%s found %d packages on %s", name, len(found), page_url)
        packages.extend(found)
    try:
        result.offers = extract_page_offers(html, page_url, config)
    except Exception as exc:  # noqa: BLE001
        result.warnings.append(f"offers failed: {type(exc).__name__}: {exc}")
    merged = (
        merge_packages(packages, limit=config.max_packages_per_page)
        if config.normalize.dedupe
        else sorted(packages, key=lambda item: -item.confidence)[: config.max_packages_per_page]
    )
    result.packages = [p for p in merged if p.confidence >= config.min_confidence_to_accept]
    dropped = len(merged) - len(result.packages)
    if dropped:
        result.warnings.append(f"dropped {dropped} low-confidence package(s)")
    if not result.packages:
        result.warnings.append("no packages extracted")
    return result


def merge_packages(packages: list[Package], *, limit: int = 120) -> list[Package]:
    """Deduplicate across extractors, keeping the most confident record."""
    exact: dict[str, Package] = {}
    fuzzy: dict[str, str] = {}
    for package in packages:
        key = package.dedupe_key()
        fuzzy_key = _fuzzy_key(package)
        existing_key = key if key in exact else fuzzy.get(fuzzy_key)
        if existing_key is None:
            exact[key] = package
            fuzzy[fuzzy_key] = key
            continue
        existing = exact[existing_key]
        if package.confidence > existing.confidence:
            _merge_features(package, existing)
            exact.pop(existing_key, None)
            exact[key] = package
            fuzzy[fuzzy_key] = key
        else:
            _merge_features(existing, package)
    merged = sorted(
        exact.values(),
        key=lambda item: (
            -item.confidence,
            item.monthly_price is None,
            item.monthly_price or 0,
        ),
    )
    return merged[:limit]


def _merge_features(target: Package, source: Package) -> None:
    if len(source.features) > len(target.features):
        target.features = list(source.features)
    if not target.description and source.description:
        target.description = source.description
    for attribute in ("storage", "disk_type", "ram", "bandwidth"):
        if not getattr(target, attribute) and getattr(source, attribute):
            setattr(target, attribute, getattr(source, attribute))


def _fuzzy_key(package: Package) -> str:
    name = re.sub(r"[^a-z0-9]+", "", (package.name or "").lower())
    name = re.sub(r"(plan|package|hosting)$", "", name)
    amount = f"{package.price_amount:.2f}" if package.price_amount is not None else ""
    return "|".join(
        [package.source_domain, name, package.currency or "", amount, package.billing_period]
    )


__all__ = ["extract_page", "merge_packages"]
