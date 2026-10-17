from __future__ import annotations

import json
from pathlib import Path

from hosting_scraper.config import AppSettings
from hosting_scraper.crawl.fetch import SiteFetcher, detect_block, looks_like_js_shell
from hosting_scraper.engine import RunOptions, ScraperEngine
from hosting_scraper.providers.certbot_registry import load_providers
from hosting_scraper.proxy.manager import ProxyManager


def test_detect_block() -> None:
    blocked, reason = detect_block(
        status=403,
        html="",
        headers={},
        statuses=[403, 429],
        markers=["captcha"],
    )
    assert blocked and "403" in reason
    blocked, _ = detect_block(
        status=200,
        html="<html><title>Just a moment...</title></html>",
        headers={},
        statuses=[403],
        markers=["just a moment"],
    )
    assert blocked
    blocked, _ = detect_block(
        status=200,
        html="a" * 260_000 + " captcha",
        headers={},
        statuses=[403],
        markers=["captcha"],
    )
    assert not blocked


def test_js_shell_detection() -> None:
    assert looks_like_js_shell('<html><body><div id="root"></div></body></html>')
    full = (
        "<html><body><h1>Pricing</h1><p>"
        + "Real content. " * 80
        + "</p></body></html>"
    )
    assert not looks_like_js_shell(full)


async def test_http_fetch_against_local_site(local_settings: AppSettings, site_server: str) -> None:
    site = local_settings.for_domain("127.0.0.1")
    fetcher = SiteFetcher(
        local_settings,
        site,
        "127.0.0.1",
        ProxyManager(local_settings.proxy),
    )
    try:
        result = await fetcher.fetch(site_server + "/pricing")
        assert result.ok
        assert result.status == 200
        assert "Hosting plans" in result.html
        assert result.error == ""
        missing = await fetcher.fetch(site_server + "/does-not-exist")
        assert not missing.ok
        assert missing.status == 404
    finally:
        await fetcher.aclose()


async def test_engine_end_to_end(
    local_settings: AppSettings,
    local_provider_file: Path,
    site_server: str,
    tmp_path: Path,
) -> None:
    load = load_providers(
        local_settings.providers_source,
        cache_path=tmp_path / "cache.json",
        user_agent="test",
        base_dir=tmp_path,
    )
    engine = ScraperEngine(local_settings, load, RunOptions())
    outcome = await engine.run()
    summary = outcome.summary
    assert summary["sites"]["total"] == 1
    assert summary["sites"]["ok"] == 1
    assert summary["totals"]["packages"] >= 5
    assert summary["totals"]["offers"] >= 1

    site = outcome.sites[0]
    names = {package.name for package in site.packages}
    assert {"Starter", "Pro", "Free", "Business", "VPS-1", "VPS-2"} <= names
    assert not any("/blog" in page.url for page in site.pages)
    assert site.used_proxy == ""

    run_dir = outcome.artifacts.root
    for filename in ("summary.json", "results.jsonl", "offers.jsonl", "sites.jsonl", "providers.json", "run-config.json", "run.log"):
        assert (run_dir / filename).exists(), filename
    domain_dir = run_dir / "artifacts" / "127.0.0.1"
    assert (domain_dir / "raw.html").exists()
    assert (domain_dir / "extracted.json").exists()
    extracted = json.loads((domain_dir / "extracted.json").read_text(encoding="utf-8"))
    assert len(extracted["packages"]) >= 5

    records = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == len(site.packages)
    assert {record["provider"] for record in records} == {"Local Hosting"}
    assert (run_dir / "summary.json").read_text(encoding="utf-8").count("Local Hosting") >= 1

    offers = [
        json.loads(line)
        for line in (run_dir / "offers.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(offer.get("coupon_code") == "SAVE20" for offer in offers)
