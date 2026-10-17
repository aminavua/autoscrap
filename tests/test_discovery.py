from __future__ import annotations

from pathlib import Path

from hosting_scraper.config import DiscoveryConfig
from hosting_scraper.crawl.discover import (
    common_path_candidates,
    extract_links,
    page_pricing_score,
    select_candidates,
)
from hosting_scraper.extract.models import Candidate

FIXTURES = Path(__file__).parent / "fixtures" / "site"


def test_link_ranking_skips_blog_and_auth_pages() -> None:
    html = (FIXTURES / "index.html").read_text(encoding="utf-8")
    config = DiscoveryConfig()
    links = extract_links(html, "https://example.com/", "example.com", config)
    paths = {link.url for link in links}
    assert "https://example.com/pricing" in paths
    assert "https://example.com/vps" in paths
    assert all("/blog" not in url for url in paths)
    assert all("/about" not in url for url in paths)
    assert all("/login" not in url for url in paths)


def test_common_paths_and_selection() -> None:
    config = DiscoveryConfig(common_paths=["pricing", "/plans", "/pricing"])
    candidates = common_path_candidates("https://example.com", "https://example.com/", config)
    urls = [candidate.url for candidate in candidates]
    assert urls == ["https://example.com/pricing", "https://example.com/plans"]
    selected = select_candidates(
        [*candidates, Candidate(url="https://example.com/pricing", score=1.0)],
        limit=1,
        min_score=1.0,
    )
    assert len(selected) == 1


def test_page_pricing_score_prefers_pricing_page() -> None:
    config = DiscoveryConfig()
    pricing = (FIXTURES / "pricing/index.html").read_text(encoding="utf-8")
    about = (FIXTURES / "about.html").read_text(encoding="utf-8")
    assert page_pricing_score(pricing, "https://example.com/pricing", config) > 10
    assert page_pricing_score(about, "https://example.com/about", config) < 3
