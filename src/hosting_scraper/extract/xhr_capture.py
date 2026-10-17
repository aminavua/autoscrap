"""Extract packages from JSON responses captured while rendering with Playwright.

Many modern pricing pages ship plan data as XHR JSON. The backend capture keeps
response bodies; this module walks them looking for records that pair a plan
name with a numeric price.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import BeautifulSoup

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract.models import Package
from hosting_scraper.extract.normalize import annotate_category, build_package
from hosting_scraper.util.text import clean_text, truncate

NAME_KEYS = (
    "planname",
    "plan_title",
    "plantitle",
    "package_name",
    "packagename",
    "product_name",
    "productname",
    "product",
    "title",
    "name",
    "label",
)
PRICE_KEYS = (
    "monthlyprice",
    "monthly_price",
    "pricepermonth",
    "price_per_month",
    "price_monthly",
    "monthlycost",
    "monthly_cost",
    "saleprice",
    "sale_price",
    "baseprice",
    "base_price",
    "startingprice",
    "starting_price",
    "amount",
    "price",
    "cost",
)
CURRENCY_KEYS = ("currency", "currencycode", "currency_code", "pricecurrency")
PERIOD_KEYS = ("billingperiod", "billing_period", "billingcycle", "billing_cycle", "interval", "period", "term")
FEATURE_KEYS = ("features", "featurelist", "feature_list", "highlights", "includes", "benefits")
DESCRIPTION_KEYS = ("description", "shortdescription", "summary", "tagline")
CATEGORY_KEYS = ("category", "plantype", "plan_type", "group", "producttype", "type")
MAX_RECORDS = 4000


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _lookup(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    normalized = {_norm_key(key): value for key, value in record.items()}
    for key in keys:
        if _norm_key(key) in normalized:
            return normalized[_norm_key(key)]
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return ""


def _as_amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = clean_text(str(value))
    match = re.search(r"-?\d+(?:[.,]\d{1,2})?", text)
    if not match:
        return None
    raw = match.group(0)
    if "," in raw and "." not in raw:
        left, right = raw.rsplit(",", 1)
        raw = f"{left}.{right}" if len(right) in (1, 2) else raw.replace(",", "")
    else:
        raw = raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _extract_price(record: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(amount_text, currency, period_hint)`` for one record."""
    value: Any = None
    key_used = ""
    normalized = {_norm_key(key): (key, item) for key, item in record.items()}
    for key in PRICE_KEYS:
        if _norm_key(key) in normalized:
            key_used, value = normalized[_norm_key(key)]
            break
    if value is None:
        # {"prices": {"monthly": 2.95, "yearly": 29.5}}
        nested = _lookup(record, ("prices", "priceoptions", "price_options", "billing"))
        if isinstance(nested, dict):
            for preferred in ("monthly", "month", "monthlyprice", "yearly", "year", "annual"):
                for key, item in nested.items():
                    if _norm_key(key) == _norm_key(preferred):
                        value = item
                        key_used = preferred
                        break
                if value is not None:
                    break
            if value is None and len(nested) == 1:
                key_used, value = next(iter(nested.items()))
    if value is None:
        return None
    currency = _as_text(_lookup(record, CURRENCY_KEYS))
    period_hint = _as_text(_lookup(record, PERIOD_KEYS)) or _period_from_key(key_used)
    if isinstance(value, dict):
        nested_currency = _as_text(_lookup(value, CURRENCY_KEYS))
        currency = nested_currency or currency
        nested_amount = None
        for key in ("amount", "value", "price", "monthly", "month"):
            nested_amount = value.get(key)
            if nested_amount is not None:
                break
        value = nested_amount
    if value is None:
        return None
    amount = _as_amount(value)
    if amount is None:
        return None
    if isinstance(value, str) and re.search(r"[$€£₹¥₦₽]", value) and not currency:
        currency = value
    return str(amount), currency, period_hint


def _period_from_key(key: str) -> str:
    normalized = _norm_key(key)
    if "month" in normalized or normalized == "mo":
        return "month"
    if "year" in normalized or "annual" in normalized:
        return "year"
    return ""


def _features(record: dict[str, Any]) -> list[str]:
    value = _lookup(record, FEATURE_KEYS)
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                label = _as_text(_lookup(item, ("name", "label", "title", "text")))
                amount = _as_text(_lookup(item, ("value", "description")))
                if label and amount:
                    out.append(f"{label}: {amount}")
                elif label or amount:
                    out.append(label or amount)
    elif isinstance(value, str):
        out.extend(part.strip() for part in re.split(r"[,;|]", value) if part.strip())
    return out


def _walk(value: Any, depth: int = 0, count: list[int] | None = None):
    count = count if count is not None else [0]
    if count[0] >= MAX_RECORDS or depth > 9:
        return
    if isinstance(value, dict):
        count[0] += 1
        yield value
        for item in value.values():
            yield from _walk(item, depth + 1, count)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, depth + 1, count)


def extract_xhr_packages(
    payloads: list[Any],
    page_url: str,
    domain: str,
    config: ExtractionConfig,
) -> list[Package]:
    packages: list[Package] = []
    for payload in payloads or []:
        body = payload.get("body") if isinstance(payload, dict) else payload
        source_url = payload.get("url", page_url) if isinstance(payload, dict) else page_url
        for record in _walk(body):
            name = _as_text(_lookup(record, NAME_KEYS))
            if not name or len(name) < 2 or len(name) > 120:
                continue
            price = _extract_price(record)
            if not price:
                continue
            amount_text, currency, period_hint = price
            if currency:
                price_text = f"{amount_text} {currency}"
            else:
                price_text = amount_text
            description = truncate(_as_text(_lookup(record, DESCRIPTION_KEYS)), 300)
            category = truncate(_as_text(_lookup(record, CATEGORY_KEYS)), 40)
            context = " ".join(value for value in (period_hint, description, name) if value)
            confidence = 0.52
            if currency:
                confidence += 0.08
            if period_hint:
                confidence += 0.06
            if _features(record):
                confidence += 0.04
            if description:
                confidence += 0.02
            package = build_package(
                name=name,
                price_text=price_text,
                context_text=context,
                page_url=source_url,
                domain=domain,
                extractor="xhr_json",
                config=config,
                features=_features(record),
                description=description,
                confidence=confidence,
                evidence=json.dumps(record, ensure_ascii=False)[:400],
            )
            if package:
                if category:
                    package.category = category.lower()[:40]
                packages.append(annotate_category(package, page_url))
    return _dedupe(packages, config.max_packages_per_page)


def embedded_json_payloads(html: str, page_url: str) -> list[dict[str, Any]]:
    """Find serialized state blobs (Next.js/Nuxt style) in server HTML."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    payloads: list[dict[str, Any]] = []
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        if "json" not in script_type:
            continue
        raw = (script.string or script.get_text() or "").strip()
        if len(raw) < 200 or len(raw) > 3_000_000:
            continue
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payloads.append({"url": page_url, "status": 200, "content_type": "application/json", "body": body})
    return payloads


def _dedupe(packages: list[Package], limit: int) -> list[Package]:
    best: dict[str, Package] = {}
    for package in packages:
        key = package.dedupe_key()
        current = best.get(key)
        if current is None or package.confidence > current.confidence:
            best[key] = package
    return list(best.values())[:limit]
