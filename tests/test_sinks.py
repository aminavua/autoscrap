from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from hosting_scraper.config import AppSettings, PostgresConfig
from hosting_scraper.extract.models import (
    DirectOffer,
    ExtractionResult,
    Package,
    PageResult,
    SiteResult,
)
from hosting_scraper.reporting import build_summary, write_summary
from hosting_scraper.storage.artifacts import RunArtifacts
from hosting_scraper.storage.sink_jsonl import JsonlSink
from hosting_scraper.storage.sink_postgres import PostgresSink


def _settings(tmp_path: Path, **storage_overrides) -> AppSettings:
    storage = {"output_dir": str(tmp_path / "runs")}
    storage.update(storage_overrides)
    return AppSettings(storage=storage)


def _site() -> SiteResult:
    package = Package(
        name="Starter",
        source_domain="example.com",
        page_url="https://example.com/pricing",
        price_raw="$3.99/mo",
        price_amount=Decimal("3.99"),
        currency="USD",
        billing_period="month",
        billing_months=1,
        monthly_price=Decimal("3.99"),
        features=["10 GB SSD"],
        extractor="dom_cards",
        confidence=0.8,
    )
    offer = DirectOffer(
        title="Save 20% with code SAVE20",
        kind="promotion",
        coupon_code="SAVE20",
        confidence=0.6,
    )
    extraction = ExtractionResult(packages=[package], offers=[offer])
    page = PageResult(
        url="https://example.com/pricing",
        status="ok",
        extraction=extraction,
    )
    return SiteResult(
        provider_name="Example Host",
        domain="example.com",
        seed_url="https://example.com/",
        status="ok",
        pages=[page],
        deduped_packages=[package],
        deduped_offers=[offer],
    )


def test_jsonl_and_csv_sink(tmp_path: Path) -> None:
    settings = _settings(tmp_path, formats={"jsonl": True, "csv": True})
    artifacts = RunArtifacts.create(settings, run_id="test-run")
    sink = JsonlSink(artifacts)
    sink.write_site(_site())
    sink.close()

    results = (artifacts.root / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(results) == 1
    record = json.loads(results[0])
    assert record["name"] == "Starter"
    assert record["provider"] == "Example Host"
    assert record["run_id"] == "test-run"
    offers = (artifacts.root / "offers.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(offers[0])["coupon_code"] == "SAVE20"
    sites = (artifacts.root / "sites.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(sites[0])["status"] == "ok"
    csv_text = (artifacts.root / "packages.csv").read_text(encoding="utf-8")
    assert csv_text.startswith("run_id,provider,domain")
    assert "Starter" in csv_text


def test_jsonl_can_be_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, formats={"jsonl": False, "csv": True})
    artifacts = RunArtifacts.create(settings, run_id="test-run")
    sink = JsonlSink(artifacts)
    sink.write_site(_site())
    sink.close()
    assert not (artifacts.root / "results.jsonl").exists()
    assert (artifacts.root / "packages.csv").exists()


def test_postgres_sink_with_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "data.db"
    config = PostgresConfig(
        enabled=True,
        dsn=f"sqlite:///{database}",
        packages_table="test_packages",
        offers_table="test_offers",
    )
    sink = PostgresSink(config, run_id="run-1")
    try:
        sink.write_site(_site())
    finally:
        sink.close()
    connection = sqlite3.connect(database)
    try:
        packages = connection.execute("select name, monthly_price, provider from test_packages").fetchall()
        offers = connection.execute("select coupon_code from test_offers").fetchall()
    finally:
        connection.close()
    assert packages == [("Starter", 3.99, "Example Host")]
    assert offers == [("SAVE20",)]


def test_summary_lists_existing_outputs(tmp_path: Path) -> None:
    settings = _settings(tmp_path, formats={"jsonl": True, "csv": False})
    artifacts = RunArtifacts.create(settings, run_id="test-run")
    sink = JsonlSink(artifacts)
    sink.write_site(_site())
    sink.close()
    from hosting_scraper.providers.certbot_registry import ProviderLoad

    load = ProviderLoad(providers=[], skipped=[], source="test", fetched_at="2026-01-01T00:00:00+00:00")
    summary = build_summary(
        artifacts=artifacts,
        settings=settings,
        provider_load=load,
        sites=[_site()],
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:02+00:00",
    )
    assert summary["duration_seconds"] == 2.0
    assert summary["outputs"]["packages"] == "results.jsonl"
    assert summary["outputs"]["sites"] == "sites.jsonl"
    assert "packages_csv" not in summary["outputs"]
    write_summary(artifacts, summary)
    assert (artifacts.root / "summary.json").exists()
