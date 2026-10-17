"""Crawl orchestration: discovery, fetching, extraction, storage and reporting."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hosting_scraper.config import AppSettings, SiteSettings, normalize_domain
from hosting_scraper.crawl.discover import (
    common_path_candidates,
    extract_links,
    page_pricing_score,
)
from hosting_scraper.crawl.fetch import SiteFetcher, looks_like_js_shell
from hosting_scraper.crawl.rate_limit import DomainRateLimiter
from hosting_scraper.crawl.robots import RobotsCache
from hosting_scraper.extract import extract_page, merge_packages
from hosting_scraper.extract.models import (
    Candidate,
    ExtractionResult,
    FetchResult,
    PageResult,
    SiteResult,
    utcnow,
)
from hosting_scraper.providers.certbot_registry import Provider, ProviderLoad
from hosting_scraper.proxy.manager import ProxyManager
from hosting_scraper.reporting import build_summary, write_summary
from hosting_scraper.storage.artifacts import RunArtifacts
from hosting_scraper.storage.sink_jsonl import JsonlSink
from hosting_scraper.storage.sink_postgres import PostgresSink
from hosting_scraper.util.hashing import slugify
from hosting_scraper.util.url import canonicalize, same_site

logger = logging.getLogger(__name__)


@dataclass
class RunOptions:
    max_sites: int | None = None
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    output_dir: Path | None = None
    run_id: str | None = None


@dataclass
class RunOutcome:
    summary: dict
    sites: list[SiteResult]
    artifacts: RunArtifacts


@dataclass
class FetchedPage:
    url: str
    fetch: FetchResult
    candidate: Candidate
    page_score: float = 0.0


class ScraperEngine:
    def __init__(
        self,
        settings: AppSettings,
        provider_load: ProviderLoad,
        options: RunOptions | None = None,
    ) -> None:
        self.settings = settings
        self.provider_load = provider_load
        self.options = options or RunOptions()
        self.artifacts = RunArtifacts.create(
            settings,
            run_id=self.options.run_id,
            output_dir=self.options.output_dir,
        )
        self.proxy_manager = ProxyManager(settings.proxy)
        self.robots = RobotsCache(
            user_agent=settings.app.user_agent,
            timeout=settings.crawl.timeout_seconds,
            headers=settings.crawl.headers,
        )
        self.limiter = DomainRateLimiter()
        self.site_semaphore = asyncio.Semaphore(settings.crawl.concurrency)
        self.started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self._monotonic_start = time.monotonic()
        self.completed_sites: list[SiteResult] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self._next_index = 0
        self._site_total = 0
        self._sink: JsonlSink | None = None
        self._postgres: PostgresSink | None = None
        self._log_handler = self._attach_file_log()

    # ------------------------------------------------------------------ #
    # setup / filtering
    # ------------------------------------------------------------------ #
    def _select_providers(self) -> list[Provider]:
        include = (
            self.options.include_domains
            if self.options.include_domains is not None
            else self.settings.crawl.include_domains
        )
        exclude = list(self.settings.crawl.exclude_domains) + list(
            self.options.exclude_domains or []
        )
        include = [normalize_domain(value) for value in include if value]
        exclude = [normalize_domain(value) for value in exclude if value]
        selected: list[Provider] = []
        filtered = 0
        for provider in self.provider_load.providers:
            if include and not _matches_domain(provider.domain, include):
                filtered += 1
                continue
            if exclude and _matches_domain(provider.domain, exclude):
                filtered += 1
                continue
            selected.append(provider)
        selected.sort(key=lambda provider: provider.name.lower())
        limit = self.options.max_sites
        if limit is None:
            limit = self.settings.crawl.max_sites
        if limit and limit > 0:
            selected = selected[:limit]
        logger.info(
            "selected %d provider(s) from %d loaded (%d filtered, max_sites=%s)",
            len(selected),
            len(self.provider_load.providers),
            filtered,
            limit or "no limit",
        )
        return selected

    def _attach_file_log(self) -> logging.Handler:
        from hosting_scraper.logging_utils import JsonFormatter

        handler = logging.FileHandler(self.artifacts.root / "run.log", encoding="utf-8")
        formatter: logging.Formatter = (
            JsonFormatter()
            if self.settings.logging.json_logs
            else logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)
        return handler

    def _open_sinks(self) -> None:
        self._sink = JsonlSink(self.artifacts)
        if self.settings.storage.postgres.enabled:
            try:
                self._postgres = PostgresSink(
                    self.settings.storage.postgres, run_id=self.artifacts.run_id
                )
            except Exception as exc:  # noqa: BLE001 - optional sink
                self.warnings.append(f"postgres sink disabled: {type(exc).__name__}: {exc}")
                logger.warning("postgres sink disabled: %s", exc)
                self._postgres = None

    def _check_playwright(self) -> None:
        from hosting_scraper.crawl.playwright_fetch import INSTALL_COMMAND_HINT, is_available

        needs_playwright = (
            self.settings.fetch.mode != "http" and self.settings.fetch.playwright.enabled
        )
        if needs_playwright and not is_available():
            self.warnings.append(
                "Playwright is enabled but not installed; JS-heavy and blocked pages will fail. "
                + INSTALL_COMMAND_HINT
            )
            logger.warning("Playwright is enabled in config but the package is not installed")

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #
    async def run(self) -> RunOutcome:
        providers = self._select_providers()
        self.artifacts.write_json("providers.json", self.provider_load.to_dict())
        self._open_sinks()
        self._check_playwright()
        self._site_total = len(providers)
        tasks = [
            asyncio.create_task(self._run_site_limited(provider))
            for provider in providers
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            sites = sorted(self.completed_sites, key=lambda site: site.provider_name.lower())
            for site in sites:
                if not self.settings.storage.save_extracted_json:
                    break
                try:
                    self.artifacts.save_site_extraction(site)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("could not write extracted.json for %s: %s", site.domain, exc)
            if self._sink is not None:
                try:
                    self._sink.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._postgres is not None:
                try:
                    await asyncio.to_thread(self._postgres.close)
                except Exception:  # noqa: BLE001
                    pass
            summary = build_summary(
                artifacts=self.artifacts,
                settings=self.settings,
                provider_load=self.provider_load,
                sites=sites,
                started_at=self.started_at,
                duration_seconds=round(time.monotonic() - self._monotonic_start, 2),
                warnings=self.warnings,
                errors=self.errors,
            )
            write_summary(self.artifacts, summary)
            self.last_summary = summary
            try:
                logging.getLogger().removeHandler(self._log_handler)
                self._log_handler.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("run complete: %s", self.artifacts.rel(self.artifacts.root / "summary.json"))
        return RunOutcome(summary=summary, sites=sites, artifacts=self.artifacts)

    async def _run_site_limited(self, provider: Provider) -> None:
        async with self.site_semaphore:
            index = self._next_index
            self._next_index += 1
            logger.info(
                "[%d/%d] crawling %s (%s)", index + 1, self._site_total, provider.name, provider.domain
            )
            site_started = time.monotonic()
            crawler: SiteCrawler | None = None
            try:
                crawler = SiteCrawler(
                    provider=provider,
                    settings=self.settings,
                    artifacts=self.artifacts,
                    proxy_manager=self.proxy_manager,
                    robots=self.robots,
                    limiter=self.limiter,
                )
                site = await crawler.crawl()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate provider failures
                logger.exception("site crawl failed for %s", provider.domain)
                site = SiteResult(
                    provider_name=provider.name,
                    domain=provider.domain,
                    seed_url=provider.seed_url,
                    status="error",
                    finished_at=utcnow(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                if crawler is not None:
                    try:
                        await crawler.aclose()
                    except Exception:  # noqa: BLE001
                        pass
            elapsed = time.monotonic() - site_started
            site.finished_at = site.finished_at or utcnow()
            self.completed_sites.append(site)
            if self._sink is not None:
                try:
                    self._sink.write_site(site)
                except Exception as exc:  # noqa: BLE001
                    message = f"JSONL sink failed for {site.domain}: {exc}"
                    self.errors.append(message)
                    logger.warning(message)
            if self._postgres is not None:
                try:
                    await asyncio.to_thread(self._postgres.write_site, site)
                except Exception as exc:  # noqa: BLE001
                    message = f"postgres sink failed for {site.domain}: {exc}"
                    self.warnings.append(message)
                    logger.warning(message)
                    self._postgres = None
            logger.info(
                "[%d/%d] %s: %s - %d package(s), %d offer(s) in %.1fs",
                index + 1,
                self._site_total,
                provider.domain,
                site.status,
                len(site.packages),
                len(site.offers),
                elapsed,
            )


class SiteCrawler:
    def __init__(
        self,
        *,
        provider: Provider,
        settings: AppSettings,
        artifacts: RunArtifacts,
        proxy_manager: ProxyManager,
        robots: RobotsCache,
        limiter: DomainRateLimiter,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.artifacts = artifacts
        self.site: SiteSettings = settings.for_domain(provider.domain)
        self.domain = provider.domain
        self.robots = robots
        self.limiter = limiter
        self.fetcher = SiteFetcher(
            settings,
            self.site,
            provider.domain,
            proxy_manager,
            screenshot_dir=(
                artifacts.screenshots_for(provider.domain)
                if settings.storage.save_screenshots
                else None
            ),
        )
        self.domain_semaphore = asyncio.Semaphore(self.site.crawl.per_domain_concurrency)
        self.attempted = 0
        self.failed_requests = 0
        self.successes: dict[str, FetchedPage] = {}
        self.final_urls: set[str] = set()
        self.requested: set[str] = set()
        self.pages: list[PageResult] = []
        self.escalations = 0
        self._max_pages = self.site.discovery.max_candidate_pages_per_site

    async def aclose(self) -> None:
        await self.fetcher.aclose()

    async def crawl(self) -> SiteResult:
        started = utcnow()
        seed_candidate = Candidate(url=self.provider.seed_url, score=99, reason="registry seed")
        seed = await self._fetch(self.provider.seed_url, candidate=seed_candidate, primary=True)
        if seed is None:
            return SiteResult(
                provider_name=self.provider.name,
                domain=self.domain,
                seed_url=self.provider.seed_url,
                status="skipped",
                started_at=started,
                finished_at=utcnow(),
                error="seed URL disallowed by robots.txt",
            )
        if not seed.fetch.ok:
            reason = seed.fetch.block_reason or seed.fetch.error or f"HTTP {seed.fetch.status}"
            page = PageResult(
                url=self.provider.seed_url,
                status="blocked" if seed.fetch.blocked else "error",
                fetch=seed.fetch,
                error=reason,
                duration_ms=seed.fetch.elapsed_ms,
            )
            return SiteResult(
                provider_name=self.provider.name,
                domain=self.domain,
                seed_url=self.provider.seed_url,
                status="error",
                pages=[page],
                used_proxy=seed.fetch.from_proxy,
                started_at=started,
                finished_at=utcnow(),
                error=f"seed fetch failed: {reason}",
            )
        self._remember(seed)
        seed_score = page_pricing_score(seed.fetch.html, seed.fetch.final_url or seed.url, self.site.discovery)
        seed.page_score = seed_score

        extras = [
            Candidate(url=url, score=25, reason="registry extra URL")
            for url in self.provider.seed_urls[1:]
            if same_site(url, self.domain) and url not in self.requested
        ]
        common = common_path_candidates(
            self.provider.seed_url, seed.fetch.final_url or seed.url, self.site.discovery
        )
        initial_budget = min(len(common), max(1, self._max_pages // 2))
        initial = [*extras, *common[:initial_budget]][: self._max_pages]
        await self._fetch_batch(initial)

        links: list[Candidate] = []
        for page in list(self.successes.values()):
            for suggestion in extract_links(
                page.fetch.html,
                page.fetch.final_url or page.url,
                self.domain,
                self.site.discovery,
                limit=self.site.discovery.max_links_to_scan,
            ):
                links.append(
                    Candidate(url=suggestion.url, score=suggestion.score, reason=suggestion.anchor or "link")
                )
        leftovers = common[initial_budget:]
        queue: dict[str, Candidate] = {}
        for candidate in [*extras, *links, *leftovers]:
            if not same_site(candidate.url, self.domain):
                continue
            if candidate.url in self.requested:
                continue
            existing = queue.get(candidate.url)
            if existing is None or candidate.score > existing.score:
                queue[candidate.url] = candidate
        remaining = max(0, self._max_pages - self.attempted)
        if remaining and queue:
            ordered = sorted(
                queue.values(),
                key=lambda item: (-item.score, len(item.url)),
            )[:remaining]
            await self._fetch_batch(ordered)

        selected = self._select_pages(seed, seed_score)
        seed_candidate.page_score = seed_score
        for page in selected:
            extraction, fetch = await self._extract_with_escalation(page)
            page.fetch = fetch
            self.pages.append(
                PageResult(
                    url=page.url,
                    status="ok",
                    fetch=fetch,
                    candidates=[seed_candidate] if page.url == seed.url else [page.candidate],
                    extraction=extraction,
                    duration_ms=fetch.elapsed_ms,
                )
            )
            if page.url == seed.url and seed_score < 2.5 and not extraction.packages:
                # The landing page had no pricing signal; drop it from the report
                # but keep it in artifacts/manifest.
                self.pages.pop()

        packages = merge_packages(
            [package for page in self.pages if page.extraction for package in page.extraction.packages],
            limit=max(self.site.extraction.max_packages_per_page * 3, 50),
        )
        offers = _dedupe_offers(
            [offer for page in self.pages if page.extraction for offer in page.extraction.offers]
        )
        proxy_labels = {
            page.fetch.from_proxy for page in self.pages if page.fetch and page.fetch.from_proxy
        }
        status = "ok" if packages else "partial" if self.successes else "error"
        error = ""
        if status == "error":
            error = "no page could be fetched"
        elif status == "partial" and not packages:
            error = "pages fetched but no packages extracted"
        site = SiteResult(
            provider_name=self.provider.name,
            domain=self.domain,
            seed_url=self.provider.seed_url,
            status=status,
            pages=self.pages,
            used_proxy=", ".join(sorted(proxy_labels)),
            started_at=started,
            finished_at=utcnow(),
            error=error,
            deduped_packages=packages,
            deduped_offers=offers,
        )
        logger.debug(
            "%s: seed score %.1f, attempted %d pages, %d successes, %d escalations",
            self.domain,
            seed_score,
            self.attempted,
            len(self.successes),
            self.escalations,
        )
        return site

    # ------------------------------------------------------------------ #
    # fetching
    # ------------------------------------------------------------------ #
    async def _fetch_batch(self, candidates: list[Candidate]) -> None:
        if not candidates:
            return
        budget = max(0, self._max_pages - self.attempted)
        candidates = [candidate for candidate in candidates if candidate.url not in self.requested][:budget]
        if not candidates:
            return
        tasks = [
            asyncio.create_task(self._fetch(candidate.url, candidate=candidate))
            for candidate in candidates
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch(
        self,
        url: str,
        *,
        candidate: Candidate,
        primary: bool = False,
        force_playwright: bool = False,
        bypass_duplicate: bool = False,
    ) -> FetchedPage | None:
        if not bypass_duplicate and url in self.requested:
            return None
        if not bypass_duplicate:
            self.requested.add(url)
        if not same_site(url, self.domain):
            return None
        if self.site.crawl.respect_robots_txt and not await self.robots.can_fetch(url):
            logger.info("skipping %s (robots.txt)", url)
            return None
        delay = self.site.crawl.delay_seconds
        if self.site.crawl.respect_robots_txt:
            delay = max(delay, await self.robots.crawl_delay(url))
        async with self.domain_semaphore:
            await self.limiter.acquire(self.domain, delay)
            if not bypass_duplicate:
                self.attempted += 1
            try:
                fetch = await self.fetcher.fetch(
                    url,
                    force_playwright=force_playwright,
                    screenshot_name=slugify(url, max_length=60),
                )
            except Exception as exc:  # noqa: BLE001 - unexpected fetch failure
                logger.warning("fetch raised for %s: %s", url, exc)
                fetch = FetchResult(
                    url=url,
                    mode="playwright" if force_playwright else "http",
                    error=f"{type(exc).__name__}: {exc}",
                )
        if not fetch.ok:
            self.failed_requests += 1
            logger.debug(
                "fetch failed for %s: status=%s blocked=%s error=%s",
                url,
                fetch.status,
                fetch.blocked,
                fetch.error,
            )
            return FetchedPage(
                url=url,
                fetch=fetch,
                candidate=candidate,
                page_score=0.0,
            )
        if self.settings.storage.save_raw_html and fetch.html:
            try:
                fetch.html_path = self.artifacts.save_html(
                    self.domain, fetch.final_url or url, fetch.html, primary=primary
                )
            except OSError as exc:
                logger.debug("could not save HTML for %s: %s", url, exc)
        page = FetchedPage(
            url=url,
            fetch=fetch,
            candidate=candidate,
            page_score=page_pricing_score(
                fetch.html, fetch.final_url or url, self.site.discovery
            ),
        )
        self._remember(page)
        return page

    def _remember(self, page: FetchedPage) -> None:
        final_key = canonicalize(page.fetch.final_url or page.url)
        if final_key and final_key in self.final_urls:
            return
        if final_key:
            self.final_urls.add(final_key)
        self.successes[page.url] = page

    def _select_pages(self, seed: FetchedPage, seed_score: float) -> list[FetchedPage]:
        selected: list[FetchedPage] = []
        if seed_score >= 2.5 or self.provider.seed_urls[1:]:
            selected.append(seed)
        candidates = [page for page in self.successes.values() if page.url != seed.url]
        candidates.sort(key=lambda page: -page.page_score)
        for page in candidates:
            if page.page_score < 0.5 and "pric" not in page.url.lower():
                continue
            selected.append(page)
        if not selected and seed.fetch.html:
            selected = [seed]
        return selected

    async def _extract_with_escalation(
        self, page: FetchedPage
    ) -> tuple[ExtractionResult, FetchResult]:
        extraction = extract_page(
            page.fetch,
            config=self.site.extraction,
            domain=self.domain,
        )
        from hosting_scraper.crawl.playwright_fetch import is_available

        can_escalate = (
            is_available()
            and not extraction.packages
            and self.site.fetch.playwright.enabled
            and self.site.fetch.mode != "http"
            and page.fetch.mode != "playwright"
            and self.escalations < 2
            and looks_like_js_shell(page.fetch.html)
        )
        if not can_escalate:
            return extraction, page.fetch
        self.escalations += 1
        logger.info("re-rendering %s with Playwright (no packages in static HTML)", page.url)
        rendered = await self._fetch(
            page.url,
            candidate=page.candidate,
            force_playwright=True,
            bypass_duplicate=True,
        )
        if rendered is None or not rendered.fetch.ok:
            return extraction, page.fetch
        rendered_extraction = extract_page(
            rendered.fetch,
            config=self.site.extraction,
            domain=self.domain,
        )
        if len(rendered_extraction.packages) >= len(extraction.packages):
            return rendered_extraction, rendered.fetch
        return extraction, page.fetch


def _matches_domain(domain: str, patterns: Iterable[str]) -> bool:
    domain = normalize_domain(domain)
    for pattern in patterns:
        pattern = normalize_domain(pattern)
        if domain == pattern or domain.endswith("." + pattern):
            return True
    return False


def _dedupe_offers(offers):
    best = {}
    for offer in offers:
        key = f"{offer.kind}:{offer.title.lower()}"
        current = best.get(key)
        if current is None or offer.confidence > current.confidence:
            best[key] = offer
    return sorted(best.values(), key=lambda item: (-item.confidence, item.title))[:20]
