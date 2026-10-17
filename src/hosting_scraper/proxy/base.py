"""Proxy route primitives shared by the HTTP and Playwright fetchers."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from hosting_scraper.config import (
    HttpPoolResource,
    ProxyConfig,
    ProxyDomainRule,
    ProxyResource,
    SmartProxyResource,
    Socks5Resource,
)
from hosting_scraper.config import normalize_domain as normalize


class ProxyError(RuntimeError):
    pass


@dataclass
class ProxyRoute:
    """One concrete way to route a request through a proxy resource."""

    resource: ProxyResource
    endpoint: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.resource.name

    @property
    def kind(self) -> str:
        return self.resource.type

    @property
    def is_smart(self) -> bool:
        return isinstance(self.resource, SmartProxyResource)

    @property
    def is_http_proxy(self) -> bool:
        return isinstance(self.resource, (HttpPoolResource, Socks5Resource))

    def label(self) -> str:
        host = ""
        if self.endpoint:
            try:
                host = urlsplit(self.endpoint).hostname or ""
            except ValueError:
                host = ""
        if self.is_smart:
            return f"{self.name}:{self.endpoint}"
        return f"{self.name}:{host or self.endpoint or 'direct'}"

    # -- outgoing request shaping --------------------------------------- #
    def request_plan(self, target_url: str) -> tuple[str, dict[str, str], str | None]:
        """Return ``(url, headers, httpx_proxy)`` for one target request."""
        if not self.is_smart:
            return target_url, dict(self.extra_headers), self.endpoint
        assert isinstance(self.resource, SmartProxyResource)
        resource = self.resource
        api_key = ""
        if resource.api_key_env:
            api_key = os.environ.get(resource.api_key_env, "")
            if not api_key:
                raise ProxyError(
                    f"proxy resource {resource.name!r} needs environment variable "
                    f"{resource.api_key_env!r}"
                )
        elif resource.api_key:
            api_key = resource.api_key
        params: dict[str, str] = {}
        for key, value in resource.params.items():
            params[key] = value.format(url=target_url, api_key=api_key)
        params.setdefault(resource.url_param, target_url)
        headers = dict(self.extra_headers)
        if resource.header_name and api_key:
            headers[resource.header_name] = resource.header_format.format(api_key=api_key)
        endpoint = resource.endpoint
        parts = urlsplit(endpoint)
        existing = dict(
            pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair
        )
        existing.update({key: str(value) for key, value in params.items()})
        query = urlencode(existing)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
        return url, headers, None

    def playwright_proxy(self) -> dict[str, str] | None:
        """Playwright proxy settings, or ``None`` when the resource is a smart API."""
        if self.is_smart:
            return None
        parts = urlsplit(self.endpoint)
        server = f"{parts.scheme}://{parts.hostname or ''}"
        if parts.port:
            server += f":{parts.port}"
        settings = {"server": server}
        if parts.username:
            settings["username"] = quote(parts.username, safe="")
        if parts.password:
            settings["password"] = quote(parts.password, safe="")
        return settings


class ProxyPlanner:
    """Pure policy/rotation logic; :class:`ProxyManager` adds logging."""

    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self._resource_counter = 0
        self._endpoint_counter = 0
        self._domain_endpoints: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self.config.enabled and any(r.enabled for r in self.config.resources)

    def rule_for(self, domain: str) -> ProxyDomainRule | None:
        domain = normalize(domain)
        for key, rule in self.config.domains.items():
            if normalize(key) == domain:
                return rule
        return None

    def mode_for(self, domain: str) -> str:
        rule = self.rule_for(domain)
        if rule and rule.mode:
            return rule.mode
        return self.config.policy.mode

    def should_use(self, domain: str, *, retry: bool = False) -> bool:
        if not self.enabled:
            return False
        if not retry and self.mode_for(domain) == "fallback":
            return False
        if retry and self.mode_for(domain) == "off":
            return False
        if retry and self.mode_for(domain) == "per_domain":
            rule = self.rule_for(domain)
            return bool(rule and rule.use_proxy)
        if self.mode_for(domain) == "per_domain":
            rule = self.rule_for(domain)
            return bool(rule and rule.use_proxy)
        if self.mode_for(domain) == "on":
            return True
        if self.mode_for(domain) == "fallback":
            return retry
        return False

    def select_route(self, domain: str) -> ProxyRoute | None:
        if not self.enabled:
            return None
        rule = self.rule_for(domain)
        resource: ProxyResource | None = None
        if rule and rule.resource:
            resource = self.config.resource_by_name(rule.resource)
        if resource is None or not resource.enabled:
            enabled = [r for r in self.config.resources if r.enabled]
            if not enabled:
                return None
            resource = enabled[self._resource_counter % len(enabled)]
            self._resource_counter += 1
        if isinstance(resource, HttpPoolResource):
            if not resource.endpoints:
                raise ProxyError(f"proxy resource {resource.name!r} has no endpoints")
            if resource.rotate == "per_domain":
                key = normalize(domain)
                if key not in self._domain_endpoints:
                    self._domain_endpoints[key] = self._endpoint_counter % len(resource.endpoints)
                    self._endpoint_counter += 1
                endpoint = resource.endpoints[self._domain_endpoints[key]]
            else:
                endpoint = resource.endpoints[self._endpoint_counter % len(resource.endpoints)]
                self._endpoint_counter += 1
            return ProxyRoute(resource=resource, endpoint=endpoint)
        if isinstance(resource, Socks5Resource):
            return ProxyRoute(resource=resource, endpoint=resource.endpoint)
        if isinstance(resource, SmartProxyResource):
            return ProxyRoute(resource=resource, endpoint=resource.endpoint)
        raise ProxyError(f"unsupported proxy resource type: {resource!r}")
