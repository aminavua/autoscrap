from __future__ import annotations

import logging

from hosting_scraper.config import ProxyConfig
from hosting_scraper.proxy.base import ProxyPlanner, ProxyRoute

logger = logging.getLogger(__name__)


class ProxyManager:
    """Decide when and how to use proxy resources for a domain.

    Modes (``proxy.policy.mode``):

    * ``off``       - never use a proxy
    * ``on``        - always route through a proxy
    * ``fallback``  - direct first; retry through a proxy when blocked
    * ``per_domain``- only domains with ``proxy.domains[...].use_proxy: true``
    """

    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self._planner = ProxyPlanner(config)

    @property
    def enabled(self) -> bool:
        return self._planner.enabled

    def mode_for(self, domain: str) -> str:
        if not self.enabled:
            return "off"
        return self._planner.mode_for(domain)

    def should_use(self, domain: str, *, retry: bool = False) -> bool:
        return self._planner.should_use(domain, retry=retry)

    def route_for(self, domain: str, *, retry: bool = False) -> ProxyRoute | None:
        if not self.should_use(domain, retry=retry):
            return None
        route = self._planner.select_route(domain)
        if route is None:
            return None
        logger.debug("proxy route for %s: %s", domain, route.label())
        return route

    def has_playwright_capable_route(self, domain: str, *, retry: bool = False) -> bool:
        route = self.route_for(domain, retry=retry)
        return bool(route and route.is_http_proxy)
