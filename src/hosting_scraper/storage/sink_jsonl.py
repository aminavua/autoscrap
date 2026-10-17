"""JSONL/CSV sinks for incremental results."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from hosting_scraper.extract.models import DirectOffer, Package, SiteResult, jsonable
from hosting_scraper.storage.artifacts import RunArtifacts

logger = logging.getLogger(__name__)

PACKAGE_FIELDS = [
    "run_id",
    "provider",
    "domain",
    "page_url",
    "name",
    "price_raw",
    "price_amount",
    "currency",
    "billing_period",
    "billing_months",
    "monthly_price",
    "yearly_price",
    "is_free",
    "category",
    "description",
    "features",
    "storage",
    "disk_type",
    "ram",
    "bandwidth",
    "extractor",
    "confidence",
    "captured_at",
]


class JsonlSink:
    def __init__(self, artifacts: RunArtifacts) -> None:
        self.artifacts = artifacts
        self.config = artifacts.settings.storage
        self.results_path = artifacts.root / "results.jsonl"
        self.offers_path = artifacts.root / "offers.jsonl"
        self.sites_path = artifacts.root / "sites.jsonl"
        self.csv_path = artifacts.root / "packages.csv"
        self.jsonl_enabled = self.config.formats.jsonl
        self._results = self._open(self.results_path) if self.jsonl_enabled else None
        self._offers = self._open(self.offers_path) if self.jsonl_enabled else None
        self._sites = self._open(self.sites_path) if self.jsonl_enabled else None
        self._csv = None
        self._csv_writer = None
        if self.config.formats.csv:
            self._csv = self.csv_path.open("w", encoding="utf-8", newline="")
            self._csv_writer = csv.DictWriter(self._csv, fieldnames=PACKAGE_FIELDS, extrasaction="ignore")
            self._csv_writer.writeheader()

    @staticmethod
    def _open(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("w", encoding="utf-8")

    def write_site(self, site: SiteResult) -> None:
        run_id = self.artifacts.run_id
        for package in site.packages:
            self._write_package(package, site, run_id)
        for offer in site.offers:
            self._write_offer(offer, site, run_id)
        page_records = []
        for page in site.pages:
            page_records.append(
                {
                    "url": page.url,
                    "status": page.status,
                    "duration_ms": page.duration_ms,
                    "error": page.error,
                    "fetch": page.fetch.to_dict() if page.fetch else None,
                    "candidate_count": len(page.candidates),
                    "package_count": len(page.extraction.packages) if page.extraction else 0,
                    "offer_count": len(page.extraction.offers) if page.extraction else 0,
                    "warnings": page.extraction.warnings if page.extraction else [],
                }
            )
        record = {
            "run_id": run_id,
            "provider": site.provider_name,
            "domain": site.domain,
            "seed_url": site.seed_url,
            "status": site.status,
            "used_proxy": site.used_proxy,
            "started_at": site.started_at,
            "finished_at": site.finished_at,
            "error": site.error,
            "package_count": len(site.packages),
            "offer_count": len(site.offers),
            "pages": page_records,
        }
        if self._sites is not None:
            self._sites.write(json.dumps(jsonable(record), ensure_ascii=False) + "\n")
            self._sites.flush()

    def _write_package(self, package: Package, site: SiteResult, run_id: str) -> None:
        record = jsonable(package)
        record.update(
            {"run_id": run_id, "provider": site.provider_name, "domain": site.domain}
        )
        record["features"] = package.features
        if self._results is not None:
            self._results.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._results.flush()
        if self._csv_writer is not None:
            row = dict(record)
            row["features"] = "; ".join(package.features)
            self._csv_writer.writerow(row)
            assert self._csv is not None
            self._csv.flush()

    def _write_offer(self, offer: DirectOffer, site: SiteResult, run_id: str) -> None:
        record = jsonable(offer)
        record.update(
            {"run_id": run_id, "provider": site.provider_name, "domain": site.domain}
        )
        if self._offers is not None:
            self._offers.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._offers.flush()

    def add_packages(self, packages: list[Package]) -> None:
        """Hook used by the Postgres sink; JSONL writes happen per site."""
        del packages

    def close(self) -> None:
        for handle in (self._results, self._offers, self._sites, self._csv):
            if handle is not None and not handle.closed:
                handle.close()
