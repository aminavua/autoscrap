from __future__ import annotations

from decimal import Decimal

import pytest

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract.normalize import build_package, dedupe_features
from hosting_scraper.util.text import (
    parse_billing_period,
    parse_capacity,
    parse_price,
)


@pytest.mark.parametrize(
    ("text", "amount", "currency"),
    [
        ("$3.99/mo", "3.99", "USD"),
        ("from $2.95 per month", "2.95", "USD"),
        ("12,99 €", "12.99", "EUR"),
        ("€12.99", "12.99", "EUR"),
        ("₹299/mo", "299", "INR"),
        ("2.95 USD/mo", "2.95", "USD"),
        ("8 GB RAM | $5.00/mo", "5.00", "USD"),
        ("100 GB SSD $3.50 per month", "3.50", "USD"),
    ],
)
def test_parse_price(text: str, amount: str, currency: str) -> None:
    parsed = parse_price(text)
    assert parsed is not None
    assert parsed.amount == Decimal(amount)
    assert parsed.currency == currency


def test_parse_price_does_not_pick_specs_or_percentages() -> None:
    parsed = parse_price("8 GB RAM | 4 vCPU | 200 GB NVMe | $12.99/mo")
    assert parsed is not None and parsed.amount == Decimal("12.99")
    assert parse_billing_period("billed every 12 months") == ("month", 12)
    assert parse_billing_period("$59/yr")[0] == "year"


def test_parse_capacity() -> None:
    capacity = parse_capacity("8 GB RAM | 4 vCPU | 200 GB NVMe SSD | unlimited bandwidth")
    assert capacity["ram"] == "8 GB"
    assert capacity["disk"] == "200 GB"
    assert capacity["disk_type"] == "NVME"
    assert capacity["bandwidth"] == "unlimited"


def test_build_package_with_feature_specs_and_currency() -> None:
    package = build_package(
        name="Pro",
        price_text="8 GB RAM | $9.99/mo",
        context_text="Pro plan billed monthly",
        page_url="https://example.com/pricing",
        domain="example.com",
        extractor="dom_cards",
        config=ExtractionConfig(),
        features=["8 GB RAM", "200 GB NVMe"],
    )
    assert package is not None
    assert package.price_amount == Decimal("9.99")
    assert package.monthly_price == Decimal("9.99")
    assert package.ram == "8 GB"
    assert package.storage == "200 GB"
    assert package.disk_type == "NVME"
    assert not package.is_free


def test_build_package_free_tier() -> None:
    package = build_package(
        name="Free",
        price_text="Free",
        context_text="Free plan",
        page_url="https://example.com/pricing",
        domain="example.com",
        extractor="dom_cards",
        config=ExtractionConfig(),
        features=["1 website"],
    )
    assert package is not None
    assert package.is_free
    assert package.price_amount == Decimal("0")
    assert package.monthly_price == Decimal("0")


def test_build_package_yearly_to_monthly() -> None:
    package = build_package(
        name="Annual",
        price_text="$59.88/yr",
        context_text="billed annually",
        page_url="https://example.com/pricing",
        domain="example.com",
        extractor="schema_org",
        config=ExtractionConfig(),
    )
    assert package is not None
    assert package.billing_period == "year"
    assert package.monthly_price == Decimal("4.99")
    assert package.yearly_price == Decimal("59.88")


def test_feature_dedupe() -> None:
    assert dedupe_features(["Free SSL", " free ssl ", "10 GB SSD"]) == ["Free SSL", "10 GB SSD"]


def test_custom_billing_tokens() -> None:
    config = ExtractionConfig(
        normalize={
            "default_billing_period": "unknown",
            "monthly_tokens": ["each moon"],
            "yearly_tokens": ["each sun"],
        }
    )
    monthly = build_package(
        name="Moon",
        price_text="$5",
        context_text="billed each moon",
        page_url="https://example.com/pricing",
        domain="example.com",
        extractor="dom_cards",
        config=config,
    )
    yearly = build_package(
        name="Sun",
        price_text="$50",
        context_text="billed each sun",
        page_url="https://example.com/pricing",
        domain="example.com",
        extractor="dom_cards",
        config=config,
    )
    assert monthly is not None and monthly.billing_period == "month"
    assert yearly is not None and yearly.billing_period == "year"
