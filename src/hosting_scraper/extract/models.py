"""Data models shared by extractors, sinks and reports."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

BillingPeriod = str  # "month" | "year" | "week" | "day" | "one_time" | "unknown"


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/Decimals/datetimes into JSON-safe values."""
    if dataclasses.is_dataclass(value):
        return {key: jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return value


def dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, indent=indent, sort_keys=indent is not None)


@dataclass
class Price:
    amount: Decimal
    currency: str | None = None
    period: BillingPeriod = "unknown"
    months: int | None = None
    raw: str = ""

    @property
    def monthly_amount(self) -> Decimal | None:
        if self.months and self.months > 0:
            return (self.amount / Decimal(self.months)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if self.period in {"month", "one_time"}:
            return self.amount
        return None

    @property
    def yearly_amount(self) -> Decimal | None:
        if self.months and self.months > 0:
            return (self.amount * Decimal(12) / Decimal(self.months)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if self.period == "year":
            return self.amount
        if self.period == "month":
            return (self.amount * 12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return None


@dataclass
class DirectOffer:
    """A promotion / discount / free-quota claim found on a page."""

    title: str
    description: str = ""
    url: str = ""
    kind: str = "promotion"  # promotion | coupon | free_quota | money_back
    discount_text: str = ""
    coupon_code: str = ""
    expires: str = ""
    confidence: float = 0.0
    extractor: str = ""
    evidence: str = ""


@dataclass
class Package:
    name: str
    source_domain: str
    page_url: str
    price_raw: str = ""
    price_amount: Decimal | None = None
    currency: str | None = None
    billing_period: BillingPeriod = "unknown"
    billing_months: int | None = None
    monthly_price: Decimal | None = None
    yearly_price: Decimal | None = None
    is_free: bool = False
    category: str = "hosting"
    description: str = ""
    features: list[str] = field(default_factory=list)
    storage: str = ""
    disk_type: str = ""
    ram: str = ""
    bandwidth: str = ""
    extractor: str = ""
    confidence: float = 0.0
    evidence: str = ""
    captured_at: str = field(default_factory=utcnow)

    def dedupe_key(self) -> str:
        amount = str(self.price_amount) if self.price_amount is not None else ""
        return "|".join(
            [
                self.source_domain,
                _norm(self.name),
                self.currency or "",
                amount,
                self.billing_period or "",
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass
class ExtractionResult:
    packages: list[Package] = field(default_factory=list)
    offers: list[DirectOffer] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int | None = None
    mode: str = "http"
    html: str = ""
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0
    attempts: int = 1
    blocked: bool = False
    block_reason: str = ""
    error: str = ""
    screenshot_path: str = ""
    html_path: str = ""
    xhr_payloads: list[Any] = field(default_factory=list)
    from_proxy: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400 and not self.error

    @property
    def content_type(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value
        return ""

    def to_dict(self) -> dict[str, Any]:
        data = jsonable(self)
        data.pop("content", None)
        data.pop("html", None)
        data.pop("xhr_payloads", None)
        data["html_length"] = len(self.html or "")
        data["xhr_payload_count"] = len(self.xhr_payloads)
        return data


@dataclass
class Candidate:
    url: str
    score: float = 0.0
    reason: str = ""
    from_seed_path: bool = False
    page_score: float = 0.0


@dataclass
class PageResult:
    url: str
    status: str  # "ok" | "error" | "skipped" | "blocked"
    fetch: FetchResult | None = None
    candidates: list[Candidate] = field(default_factory=list)
    extraction: ExtractionResult | None = None
    error: str = ""
    duration_ms: int = 0


@dataclass
class SiteResult:
    provider_name: str
    domain: str
    seed_url: str
    status: str = "ok"  # ok | partial | error | skipped
    pages: list[PageResult] = field(default_factory=list)
    used_proxy: str = ""
    started_at: str = field(default_factory=utcnow)
    finished_at: str = ""
    error: str = ""
    deduped_packages: list[Package] | None = None
    deduped_offers: list[DirectOffer] | None = None

    @property
    def packages(self) -> list[Package]:
        if self.deduped_packages is not None:
            return self.deduped_packages
        out: list[Package] = []
        for page in self.pages:
            if page.extraction:
                out.extend(page.extraction.packages)
        return out

    @property
    def offers(self) -> list[DirectOffer]:
        if self.deduped_offers is not None:
            return self.deduped_offers
        out: list[DirectOffer] = []
        for page in self.pages:
            if page.extraction:
                out.extend(page.extraction.offers)
        return out


def _norm(value: str) -> str:
    import re

    return re.sub(r"\s+", " ", (value or "").strip().lower())
