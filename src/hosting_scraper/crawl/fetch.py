"""HTTP fetching with proxy policy, block detection and Playwright fallback."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from hosting_scraper.config import AppSettings, SiteSettings
from hosting_scraper.extract.models import FetchResult
from hosting_scraper.proxy.base import ProxyError, ProxyRoute
from hosting_scraper.proxy.manager import ProxyManager

logger = logging.getLogger(__name__)

JS_SHELL_MARKERS = (
    "id=\"root\"",
    "id=\"app\"",
    "id=\"__next\"",
    "__next_data__",
    "ng-app",
    "data-reactroot",
    "enable javascript",
    "javascript is disabled",
    "please enable js",
)


def detect_block(
    *,
    status: int | None,
    html: str,
    headers: dict[str, str],
    statuses: list[int],
    markers: list[str],
    visible_text: str | None = None,
) -> tuple[bool, str]:
    """Best-effort challenge/block detection.

    Body markers only count for short pages so that a SaaS feature page which
    happens to mention "captcha" is not misclassified.
    """
    if status is not None and status in statuses:
        return True, f"HTTP {status}"
    lowered_headers = {key.lower(): value.lower() for key, value in headers.items()}
    if "challenge" in lowered_headers.get("cf-mitigated", ""):
        return True, "Cloudflare challenge header"
    body = (visible_text if visible_text is not None else html or "").lower()
    if not body:
        return False, ""
    haystack_limit = 250_000 if visible_text is None else 20_000
    if len(body) < haystack_limit:
        for marker in markers:
            if marker.lower() in body:
                return True, f"challenge marker: {marker!r}"
    return False, ""


def looks_like_js_shell(html: str) -> bool:
    """True when a page is likely an empty SPA shell that needs a browser."""
    if not html:
        return True
    lowered = html.lower()
    text = re.sub(r"<script[^>]*>.*?</script>", " ", lowered, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 400:
        return True
    return any(marker in lowered for marker in JS_SHELL_MARKERS) and len(text) < 1500


class SiteFetcher:
    """Fetches pages for one domain, sharing a Playwright browser when needed."""

    def __init__(
        self,
        settings: AppSettings,
        site: SiteSettings,
        domain: str,
        proxy_manager: ProxyManager,
        *,
        screenshot_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.site = site
        self.domain = domain
        self.proxy_manager = proxy_manager
        self.screenshot_dir = screenshot_dir
        self._playwright: Any | None = None
        self._browsers: dict[str, Any] = {}
        self._pw_lock = asyncio.Lock()
        self._warned_playwright = False

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    async def fetch(
        self,
        url: str,
        *,
        force_playwright: bool = False,
        screenshot_name: str = "",
    ) -> FetchResult:
        mode = self.site.fetch.mode
        if force_playwright:
            mode = "playwright"
        use_proxy = self.proxy_manager.should_use(self.domain)
        route = self.proxy_manager.route_for(self.domain) if use_proxy else None

        if mode == "playwright":
            result = await self._fetch_with_playwright_managed(
                url, route=route, screenshot_name=screenshot_name
            )
            if self._should_try_proxy_retry(result) and route is None:
                route = self.proxy_manager.route_for(self.domain, retry=True)
                if route is not None:
                    retry = await self._fetch_with_playwright_managed(
                        url, route=route, screenshot_name=screenshot_name
                    )
                    if retry.ok and not retry.blocked:
                        return retry
            return result

        result = await self._fetch_http_managed(url, route=route)
        if result.ok and not result.blocked:
            return result

        # Fallback policy: retry through a proxy after a direct block.
        if route is None and self._should_try_proxy_retry(result):
            proxy_route = self.proxy_manager.route_for(self.domain, retry=True)
            if proxy_route is not None:
                logger.info("retrying %s through proxy %s", url, proxy_route.label())
                proxied = await self._fetch_http_managed(url, route=proxy_route)
                if proxied.ok and not proxied.blocked:
                    return proxied
                result = proxied
                route = proxy_route

        # Playwright fallback for JS shells, challenges and hard blocks.
        if self._should_escalate_to_playwright(result):
            if route is not None and route.is_smart:
                logger.warning(
                    "cannot use Playwright through smart proxy %s; retrying without it",
                    route.name,
                )
                route = None
            logger.info("escalating %s to Playwright", url)
            rendered = await self._fetch_with_playwright_managed(
                url, route=route, screenshot_name=screenshot_name
            )
            if rendered.ok and not rendered.blocked:
                return rendered
            if not result.ok or result.blocked:
                return rendered

        return result

    async def aclose(self) -> None:
        for browser in self._browsers.values():
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
        self._browsers.clear()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    # ------------------------------------------------------------------ #
    # policy helpers
    # ------------------------------------------------------------------ #
    def _should_try_proxy_retry(self, result: FetchResult) -> bool:
        if not result.ok or result.blocked:
            return self.proxy_manager.should_use(self.domain, retry=True)
        return False

    def _should_escalate_to_playwright(self, result: FetchResult) -> bool:
        from hosting_scraper.crawl.playwright_fetch import is_available

        if not is_available():
            return False
        if not self.site.fetch.playwright.enabled:
            return False
        if self.site.fetch.mode == "http":
            return False
        if result.blocked:
            return bool(self.settings.proxy.policy.retry_with_playwright)
        if not result.error and result.ok and looks_like_js_shell(result.html):
            return True
        return False

    def _httpx_kwargs(self, route: ProxyRoute | None) -> dict[str, Any]:
        timeout = httpx.Timeout(self.site.crawl.timeout_seconds)
        return {
            "timeout": timeout,
            "follow_redirects": self.site.fetch.http.follow_redirects,
            "verify": self.site.fetch.http.verify_tls,
            "max_redirects": self.site.crawl.max_redirects,
        }

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.settings.app.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            **self.site.crawl.headers,
        }
        return headers

    async def _fetch_http_managed(
        self, url: str, *, route: ProxyRoute | None
    ) -> FetchResult:
        attempts = self.site.crawl.retries + 1
        last: FetchResult | None = None
        for attempt in range(1, attempts + 1):
            result = await self._fetch_http(url, route=route, attempt=attempt)
            last = result
            if result.ok and not result.blocked:
                return result
            if result.blocked:
                return result
            if result.status is not None and 400 <= result.status < 500:
                return result
            if attempt < attempts:
                await asyncio.sleep(self.site.crawl.retry_backoff_seconds * attempt)
        assert last is not None
        return last

    async def _fetch_http(
        self, url: str, *, route: ProxyRoute | None, attempt: int
    ) -> FetchResult:
        started = time.monotonic()
        fetch = FetchResult(url=url, mode="http", attempts=attempt)
        request_url = url
        headers = self._base_headers()
        proxy: str | None = None
        if route is not None:
            try:
                request_url, extra_headers, proxy = route.request_plan(url)
                headers.update(extra_headers)
                fetch.from_proxy = route.label()
            except ProxyError as exc:
                fetch.error = str(exc)
                fetch.elapsed_ms = int((time.monotonic() - started) * 1000)
                return fetch
        try:
            async with httpx.AsyncClient(**self._httpx_kwargs(route), proxy=proxy) as client:
                response = await client.get(request_url, headers=headers)
            fetch.status = response.status_code
            fetch.headers = {key.lower(): value for key, value in response.headers.items()}
            fetch.final_url = url if (route is not None and route.is_smart) else str(response.url)
            content_type = response.headers.get("content-type", "")
            if any(token in content_type.lower() for token in ("application/json", "text/plain")):
                fetch.html = response.text
            elif "html" in content_type.lower() or not content_type:
                fetch.html = response.text
            else:
                fetch.error = f"unsupported content type: {content_type}"
                fetch.html = ""
            fetch.content = response.content
            if route is None:
                fetch.from_proxy = ""
            blocked, reason = detect_block(
                status=fetch.status,
                html=fetch.html,
                headers=fetch.headers,
                statuses=self.settings.proxy.policy.blocked_statuses,
                markers=self.settings.proxy.policy.challenge_markers,
            )
            fetch.blocked = blocked
            fetch.block_reason = reason
            if fetch.status >= 400 and not fetch.error:
                fetch.error = f"HTTP {fetch.status}"
        except httpx.HTTPError as exc:
            fetch.error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - malformed proxy URL etc.
            fetch.error = f"{type(exc).__name__}: {exc}"
        fetch.elapsed_ms = int((time.monotonic() - started) * 1000)
        return fetch

    # ------------------------------------------------------------------ #
    # Playwright
    # ------------------------------------------------------------------ #
    async def _ensure_playwright(self) -> Any:
        if self._playwright is not None:
            return self._playwright
        async with self._pw_lock:
            if self._playwright is not None:
                return self._playwright
            from hosting_scraper.crawl.playwright_fetch import start_playwright

            self._playwright = await start_playwright()
            return self._playwright

    async def _get_browser(self, route: ProxyRoute | None) -> Any:
        proxy_settings = route.playwright_proxy() if route else None
        key = "direct" if not proxy_settings else f"{proxy_settings.get('server')}|{proxy_settings.get('username','')}"
        if key in self._browsers:
            return self._browsers[key]
        playwright = await self._ensure_playwright()
        browser_type = getattr(playwright, self.site.fetch.playwright.browser)
        launch_kwargs: dict[str, Any] = {
            "headless": self.site.fetch.playwright.headless,
            "args": list(self.site.fetch.playwright.browser_args),
        }
        if proxy_settings:
            launch_kwargs["proxy"] = proxy_settings
        browser = await browser_type.launch(**launch_kwargs)
        self._browsers[key] = browser
        return browser

    async def _fetch_with_playwright_managed(
        self,
        url: str,
        *,
        route: ProxyRoute | None,
        screenshot_name: str = "",
    ) -> FetchResult:
        attempts = self.site.crawl.retries + 1
        last: FetchResult | None = None
        for attempt in range(1, attempts + 1):
            result = await self._fetch_with_playwright(
                url, route=route, attempt=attempt, screenshot_name=screenshot_name
            )
            last = result
            if result.ok and not result.blocked:
                return result
            if result.blocked or (result.status and result.status < 500):
                return result
            if "Executable doesn't exist" in (result.error or ""):
                return result
            if attempt < attempts:
                await asyncio.sleep(self.site.crawl.retry_backoff_seconds * attempt)
        assert last is not None
        return last

    async def _fetch_with_playwright(
        self,
        url: str,
        *,
        route: ProxyRoute | None,
        attempt: int,
        screenshot_name: str = "",
    ) -> FetchResult:
        started = time.monotonic()
        result = FetchResult(url=url, mode="playwright", attempts=attempt)
        if route is not None:
            if route.is_smart:
                result.error = (
                    f"smart proxy {route.name!r} cannot be used by the Playwright fetcher"
                )
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                return result
            result.from_proxy = route.label()
        try:
            browser = await self._get_browser(route)
            context = await browser.new_context(
                user_agent=self.settings.app.user_agent,
                extra_http_headers={
                    **{k: v for k, v in self._base_headers().items() if k.lower() != "user-agent"}
                },
                ignore_https_errors=not self.site.fetch.http.verify_tls,
                viewport={"width": 1440, "height": 900},
            )
            try:
                page = await context.new_page()
                await self._install_resource_blocking(page)
                captures: list[dict[str, Any]] = []
                capture_tasks: list[asyncio.Task[Any]] = []

                def on_response(response: Any) -> None:
                    capture_tasks.append(
                        asyncio.create_task(self._capture_xhr(response, captures))
                    )

                page.on("response", on_response)
                try:
                    response = await page.goto(
                        url,
                        wait_until=self.site.fetch.playwright.wait_until,
                        timeout=self.site.fetch.playwright.navigation_timeout_seconds * 1000,
                    )
                except Exception as exc:  # noqa: BLE001
                    if "Timeout" not in type(exc).__name__:
                        raise
                    logger.warning(
                        "Playwright navigation timed out for %s; using the DOM rendered so far",
                        url,
                    )
                    response = None
                result.status = response.status if response else None
                if result.status is None:
                    result.status = 200
                if self.site.fetch.playwright.wait_after_load_ms:
                    await page.wait_for_timeout(self.site.fetch.playwright.wait_after_load_ms)
                if self.site.fetch.playwright.toggle_billing.enabled:
                    await self._toggle_billing(page)
                result.html = await page.content()
                result.final_url = page.url
                result.headers = dict(response.headers) if response else {}
                if self.settings.storage.save_screenshots and self.screenshot_dir:
                    self.screenshot_dir.mkdir(parents=True, exist_ok=True)
                    name = screenshot_name or f"page-{int(time.time() * 1000)}"
                    path = self.screenshot_dir / f"{name}.png"
                    try:
                        await page.screenshot(path=str(path), full_page=True)
                        result.screenshot_path = str(path)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("screenshot failed for %s: %s", url, exc)
                if capture_tasks:
                    await asyncio.gather(*capture_tasks, return_exceptions=True)
                result.xhr_payloads = [capture for capture in captures if capture]
                try:
                    page_text = (await page.title()) + "\n" + await page.locator("body").inner_text()
                except Exception:  # noqa: BLE001
                    page_text = ""
                blocked, reason = detect_block(
                    status=result.status,
                    html=result.html,
                    headers={key.lower(): value for key, value in result.headers.items()},
                    statuses=self.settings.proxy.policy.blocked_statuses,
                    markers=self.settings.proxy.policy.challenge_markers,
                    visible_text=page_text,
                )
                result.blocked = blocked
                result.block_reason = reason
            finally:
                await context.close()
        except Exception as exc:  # noqa: BLE001 - playwright exceptions are varied
            result.error = f"{type(exc).__name__}: {exc}"
            from hosting_scraper.crawl.playwright_fetch import browser_hint

            if "Executable doesn't exist" in str(exc) and not self._warned_playwright:
                self._warned_playwright = True
                result.error = browser_hint(result.error)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    async def _install_resource_blocking(self, page: Any) -> None:
        blocked = set(self.site.fetch.playwright.block_resource_types)
        if self.settings.storage.save_screenshots:
            blocked -= {"image", "font"}
        if not blocked:
            return

        async def handler(route: Any, request: Any) -> None:
            if request.resource_type in blocked:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", handler)

    async def _capture_xhr(self, response: Any, captures: list[dict[str, Any]]) -> None:
        try:
            resource_type = response.request.resource_type
            if resource_type not in {"xhr", "fetch"}:
                return
            content_type = (await response.all_headers()).get("content-type", "")
            if "json" not in content_type.lower():
                return
            body = await response.text()
            if not body or len(body) > 1_500_000:
                return
            import json

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return
            if len(captures) < 40:
                captures.append(
                    {"url": response.url, "status": response.status, "content_type": content_type, "body": payload}
                )
        except Exception:  # noqa: BLE001 - navigation can invalidate responses
            return

    async def _toggle_billing(self, page: Any) -> None:
        toggle = self.site.fetch.playwright.toggle_billing
        target = toggle.target
        labels = toggle.labels.get(target, [])
        if not labels:
            return
        for label in labels:
            pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
            try:
                locator = page.locator(
                    "button, [role=button], [role=tab], label, [role=switch]"
                ).filter(has_text=pattern)
                count = await locator.count()
                for index in range(min(count, 3)):
                    item = locator.nth(index)
                    if not await item.is_visible():
                        continue
                    await item.click(timeout=2500)
                    await page.wait_for_timeout(500)
                    logger.debug("clicked billing toggle %r on %s", label, page.url)
                    return
            except Exception:  # noqa: BLE001 - toggle is strictly best effort
                continue
