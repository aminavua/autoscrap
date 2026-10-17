"""Run summary/report generation."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from hosting_scraper.config import AppSettings
from hosting_scraper.extract.models import SiteResult, jsonable
from hosting_scraper.providers.certbot_registry import ProviderLoad
from hosting_scraper.storage.artifacts import RunArtifacts


def build_summary(
    *,
    artifacts: RunArtifacts,
    settings: AppSettings,
    provider_load: ProviderLoad,
    sites: list[SiteResult],
    started_at: str,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    finished_at = finished_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    status_counts = Counter(site.status for site in sites)
    packages = [package for site in sites for package in site.packages]
    offers = [offer for site in sites for offer in site.offers]
    proxy_counts = Counter(site.used_proxy for site in sites if site.used_proxy)
    extractor_counts = Counter(package.extractor for package in packages)
    free_packages = sum(1 for package in packages if package.is_free)
    pages_fetched = sum(len(site.pages) for site in sites)
    outputs: dict[str, str] = {
        "summary": "summary.json",
        "providers": "providers.json",
        "artifacts": "artifacts/",
        "run_config": "run-config.json",
        "log": "run.log",
    }
    for key, filename in (
        ("sites", "sites.jsonl"),
        ("packages", "results.jsonl"),
        ("offers", "offers.jsonl"),
        ("packages_csv", "packages.csv"),
    ):
        if (artifacts.root / filename).exists():
            outputs[key] = filename
    summary = {
        "run_id": artifacts.run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": (
            round(duration_seconds, 2)
            if duration_seconds is not None
            else _duration(started_at, finished_at)
        ),
        "config": {
            "app": settings.app.model_dump(),
            "crawl": settings.crawl.model_dump(),
            "fetch": settings.fetch.model_dump(),
            "proxy": {
                "enabled": settings.proxy.enabled,
                "mode": settings.proxy.policy.mode,
                "resources": [resource.name for resource in settings.proxy.resources],
            },
            "storage": {"output_dir": str(artifacts.root)},
        },
        "providers": {
            "source": provider_load.source,
            "fetched_at": provider_load.fetched_at,
            "from_cache": provider_load.from_cache,
            "loaded": len(provider_load.providers),
            "skipped": len(provider_load.skipped),
            "skipped_details": provider_load.skipped,
            "warnings": provider_load.warnings,
        },
        "sites": {
            "total": len(sites),
            "ok": status_counts.get("ok", 0),
            "partial": status_counts.get("partial", 0),
            "error": status_counts.get("error", 0),
            "skipped": status_counts.get("skipped", 0),
        },
        "totals": {
            "packages": len(packages),
            "free_packages": free_packages,
            "offers": len(offers),
            "pages_fetched": pages_fetched,
            "sites_with_packages": sum(1 for site in sites if site.packages),
            "sites_with_errors": status_counts.get("error", 0),
        },
        "breakdown": {
            "status": dict(status_counts),
            "extractors": dict(extractor_counts),
            "proxy_usage": dict(proxy_counts),
        },
        "warnings": list(warnings or []) + list(provider_load.warnings),
        "errors": list(errors or []),
        "outputs": outputs,
        "site_results": [
            {
                "provider": site.provider_name,
                "domain": site.domain,
                "status": site.status,
                "packages": len(site.packages),
                "offers": len(site.offers),
                "pages": len(site.pages),
                "used_proxy": site.used_proxy,
                "error": site.error,
            }
            for site in sites
        ],
    }
    return jsonable(summary)


def _duration(started_at: str, finished_at: str) -> float:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return round((end - start).total_seconds(), 2)


def write_summary(artifacts: RunArtifacts, summary: dict[str, Any]) -> str:
    return artifacts.rel(artifacts.write_json("summary.json", summary))
