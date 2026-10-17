"""Small async robots.txt cache.

Robots enforcement is opt-in (``crawl.respect_robots_txt``). When enabled we
fetch each origin's robots.txt once, use Python's stdlib parser, and honor
``Crawl-delay`` as a lower bound for the per-domain politeness delay.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)


class RobotsCache:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 15,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.headers = headers or {}
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _get(self, origin: str) -> RobotFileParser | None:
        if origin in self._cache:
            return self._cache[origin]
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._cache:
                return self._cache[origin]
            parser: RobotFileParser | None = RobotFileParser()
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": self.user_agent, **self.headers},
                ) as client:
                    response = await client.get(origin + "/robots.txt")
                if response.status_code >= 400:
                    parser = None
                else:
                    parser.parse(response.text.splitlines())
            except Exception as exc:  # noqa: BLE001 - network failure -> allow
                logger.debug("robots.txt fetch failed for %s: %s", origin, exc)
                parser = None
            self._cache[origin] = parser
            return parser

    async def can_fetch(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return True
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = await self._get(origin)
        if parser is None:
            return True
        allowed = parser.can_fetch(self.user_agent, url)
        if not allowed:
            logger.info("robots.txt disallows %s", url)
        return allowed

    async def crawl_delay(self, url: str) -> float:
        try:
            parts = urlsplit(url)
        except ValueError:
            return 0.0
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = await self._get(origin)
        if parser is None:
            return 0.0
        try:
            return float(parser.crawl_delay(self.user_agent) or 0.0)
        except (TypeError, ValueError):
            return 0.0
