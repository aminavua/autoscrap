from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract import extract_page, merge_packages
from hosting_scraper.extract.dom_cards import extract_card_packages
from hosting_scraper.extract.dom_tables import extract_table_packages
from hosting_scraper.extract.models import FetchResult
from hosting_scraper.extract.offers import extract_page_offers
from hosting_scraper.extract.schema_org import extract_schema_packages
from hosting_scraper.extract.xhr_capture import extract_xhr_packages

FIXTURES = Path(__file__).parent / "fixtures" / "site"


def _fetch(name: str, url: str = "https://example.com/pricing") -> FetchResult:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return FetchResult(url=url, final_url=url, status=200, html=html)


def test_full_pricing_page() -> None:
    result = extract_page(
        _fetch("pricing/index.html"),
        config=ExtractionConfig(),
        domain="example.com",
    )
    by_name = {package.name: package for package in result.packages}
    assert {"Free", "Starter", "Pro", "Business"} <= set(by_name)
    starter = by_name["Starter"]
    assert starter.price_amount == Decimal("3.99")
    assert starter.monthly_price == Decimal("3.99")
    assert starter.currency == "USD"
    assert any("Free SSL" in feature for feature in starter.features)
    assert by_name["Free"].is_free
    assert by_name["Free"].price_amount == Decimal("0")
    assert by_name["Business"].price_amount == Decimal("14.99")
    assert result.warnings == [] or all("low-confidence" not in warning for warning in result.warnings)
    promo = next(offer for offer in result.offers if offer.kind == "promotion")
    assert promo.coupon_code == "SAVE20"


def test_transposed_vps_table() -> None:
    packages = extract_table_packages(
        (FIXTURES / "vps/index.html").read_text(encoding="utf-8"),
        "https://example.com/vps",
        "example.com",
        ExtractionConfig(),
    )
    by_name = {package.name: package for package in packages}
    assert set(by_name) == {"VPS-1", "VPS-2"}
    assert by_name["VPS-1"].price_amount == Decimal("5.99")
    assert by_name["VPS-2"].price_amount == Decimal("11.99")
    assert any("RAM: 2 GB" in feature for feature in by_name["VPS-1"].features)


def test_normal_table_and_feature_table_false_positive_guard() -> None:
    html = """
    <table><tr><th>Plan</th><th>Price</th><th>Storage</th></tr>
    <tr><td>Basic</td><td>$4.50/mo</td><td>20 GB</td></tr></table>
    <table><tr><th>Feature</th><th>Basic</th><th>Pro</th></tr>
    <tr><td>RAM</td><td>2 GB</td><td>4 GB</td></tr></table>
    """
    packages = extract_table_packages(html, "https://example.com/pricing", "example.com", ExtractionConfig())
    assert [package.name for package in packages] == ["Basic"]
    assert packages[0].price_amount == Decimal("4.50")


def test_cards_do_not_treat_free_ssl_as_free_price() -> None:
    html = """
    <div class="pricing-card"><h3>Starter</h3>
      <div class="price">$3.99/mo</div>
      <ul><li>10 GB SSD</li><li>Free SSL</li></ul>
    </div>
    """
    packages = extract_card_packages(html, "https://example.com/pricing", "example.com", ExtractionConfig())
    assert len(packages) == 1
    assert packages[0].price_amount == Decimal("3.99")
    assert not packages[0].is_free


def test_schema_org_currency_and_period() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"Product","name":"Cloud","offers":{"@type":"Offer","price":19.5,
     "priceCurrency":"EUR","priceSpecification":{"billingDuration":"P1Y"}}}
    </script>
    """
    packages = extract_schema_packages(html, "https://example.com/cloud", "example.com", ExtractionConfig())
    assert len(packages) == 1
    package = packages[0]
    assert package.currency == "EUR"
    assert package.billing_period == "year"
    assert package.monthly_price == Decimal("1.63")


def test_embedded_json_payload_extraction() -> None:
    fetch = _fetch("js-pricing/index.html", "https://example.com/deals")
    result = extract_page(fetch, config=ExtractionConfig(), domain="example.com")
    names = {package.name: package for package in result.packages}
    assert {"Nano", "Mega"} <= set(names)
    assert names["Mega"].price_amount == Decimal("19.5")
    assert names["Mega"].storage == "200 GB"


def test_xhr_nested_prices() -> None:
    payloads = [
        {
            "url": "https://example.com/api/plans",
            "body": {
                "plans": [
                    {"name": "Small", "prices": {"monthly": 2.95}, "currency": "USD"},
                    {"name": "Big", "price": {"amount": 10.5, "currency": "EUR"}, "billingPeriod": "month"},
                ]
            },
        }
    ]
    packages = extract_xhr_packages(payloads, "https://example.com/pricing", "example.com", ExtractionConfig())
    by_name = {package.name: package for package in packages}
    assert by_name["Small"].price_amount == Decimal("2.95")
    assert by_name["Big"].currency == "EUR"
    assert by_name["Big"].monthly_price == Decimal("10.5")


def test_merge_prefers_confidence_and_keeps_features() -> None:
    result = extract_page(
        _fetch("pricing/index.html"),
        config=ExtractionConfig(),
        domain="example.com",
    )
    merged = merge_packages(result.packages)
    starter = next(package for package in merged if package.name == "Starter")
    assert starter.confidence >= 0.78
    assert any("Free SSL" in feature for feature in starter.features)


def test_offers_coupon_and_free_quota() -> None:
    html = """
    <html><body>
      <p>Save 20% with code SAVE20.</p>
      <p>Every plan includes a free domain for the first year.</p>
      <p>30-day money-back guarantee.</p>
    </body></html>
    """
    offers = extract_page_offers(html, "https://example.com/pricing", ExtractionConfig())
    kinds = {offer.kind for offer in offers}
    assert {"promotion", "free_quota", "money_back"} <= kinds
    coupon = next(offer for offer in offers if offer.kind == "promotion")
    assert coupon.coupon_code == "SAVE20"
