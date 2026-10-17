"""Optional SQL sink (PostgreSQL by default; any SQLAlchemy URL works)."""

from __future__ import annotations

import logging
import re
from typing import Any

from hosting_scraper.config import PostgresConfig
from hosting_scraper.extract.models import SiteResult

logger = logging.getLogger(__name__)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresSink:
    def __init__(self, config: PostgresConfig, *, run_id: str) -> None:
        self.config = config
        self.run_id = run_id
        try:
            from sqlalchemy import (
                JSON,
                Column,
                Float,
                Integer,
                MetaData,
                Numeric,
                String,
                Table,
                Text,
                create_engine,
            )
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Postgres storage requires SQLAlchemy and a driver: "
                "pip install -e .[postgres]"
            ) from exc
        if not config.dsn:
            raise ValueError("storage.postgres.dsn is required")
        for name in (config.packages_table, config.offers_table):
            if not IDENTIFIER_RE.match(name):
                raise ValueError(f"invalid SQL table name: {name!r}")
        self._engine = create_engine(config.dsn, pool_pre_ping=True, future=True)
        metadata = MetaData()
        self._packages = Table(
            config.packages_table,
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("run_id", String(80), index=True),
            Column("provider", String(255)),
            Column("domain", String(255), index=True),
            Column("page_url", Text),
            Column("name", String(255)),
            Column("price_raw", String(120)),
            Column("price_amount", Numeric(14, 4)),
            Column("currency", String(8)),
            Column("billing_period", String(20)),
            Column("billing_months", Integer),
            Column("monthly_price", Numeric(14, 4)),
            Column("yearly_price", Numeric(14, 4)),
            Column("is_free", Integer),
            Column("category", String(60)),
            Column("description", Text),
            Column("features", JSON),
            Column("storage", String(60)),
            Column("disk_type", String(20)),
            Column("ram", String(60)),
            Column("bandwidth", String(60)),
            Column("extractor", String(40)),
            Column("confidence", Float),
            Column("captured_at", String(40)),
        )
        self._offers = Table(
            config.offers_table,
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("run_id", String(80), index=True),
            Column("provider", String(255)),
            Column("domain", String(255), index=True),
            Column("title", String(300)),
            Column("description", Text),
            Column("url", Text),
            Column("kind", String(40)),
            Column("discount_text", String(120)),
            Column("coupon_code", String(60)),
            Column("expires", String(60)),
            Column("confidence", Float),
            Column("extractor", String(40)),
            Column("evidence", Text),
        )
        metadata.create_all(self._engine)

    def write_site(self, site: SiteResult) -> None:
        package_rows: list[dict[str, Any]] = []
        for package in site.packages:
            package_rows.append(
                {
                    "run_id": self.run_id,
                    "provider": site.provider_name,
                    "domain": site.domain,
                    "page_url": package.page_url,
                    "name": package.name,
                    "price_raw": package.price_raw,
                    "price_amount": package.price_amount,
                    "currency": package.currency,
                    "billing_period": package.billing_period,
                    "billing_months": package.billing_months,
                    "monthly_price": package.monthly_price,
                    "yearly_price": package.yearly_price,
                    "is_free": int(package.is_free),
                    "category": package.category,
                    "description": package.description,
                    "features": package.features,
                    "storage": package.storage,
                    "disk_type": package.disk_type,
                    "ram": package.ram,
                    "bandwidth": package.bandwidth,
                    "extractor": package.extractor,
                    "confidence": package.confidence,
                    "captured_at": package.captured_at,
                }
            )
        offer_rows: list[dict[str, Any]] = []
        for offer in site.offers:
            offer_rows.append(
                {
                    "run_id": self.run_id,
                    "provider": site.provider_name,
                    "domain": site.domain,
                    "title": offer.title,
                    "description": offer.description,
                    "url": offer.url,
                    "kind": offer.kind,
                    "discount_text": offer.discount_text,
                    "coupon_code": offer.coupon_code,
                    "expires": offer.expires,
                    "confidence": offer.confidence,
                    "extractor": offer.extractor,
                    "evidence": offer.evidence,
                }
            )
        with self._engine.begin() as connection:
            if package_rows:
                connection.execute(self._packages.insert(), package_rows)
            if offer_rows:
                connection.execute(self._offers.insert(), offer_rows)
        logger.debug("wrote %d packages / %d offers to SQL for %s", len(package_rows), len(offer_rows), site.domain)

    def close(self) -> None:
        self._engine.dispose()
