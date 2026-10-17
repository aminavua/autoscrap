"""Per-domain request spacing that works with concurrent page fetches."""

from __future__ import annotations

import asyncio


class DomainRateLimiter:
    def __init__(self) -> None:
        self._next_start: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, domain: str, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            return
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            scheduled = max(now, self._next_start.get(domain, 0.0))
            wait = scheduled - now
            self._next_start[domain] = scheduled + delay_seconds
        if wait > 0:
            await asyncio.sleep(wait)
