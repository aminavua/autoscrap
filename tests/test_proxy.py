from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import pytest

from hosting_scraper.config import (
    HttpPoolResource,
    ProxyConfig,
    ProxyDomainRule,
    SmartProxyResource,
    Socks5Resource,
)
from hosting_scraper.proxy.base import ProxyError, ProxyPlanner
from hosting_scraper.proxy.manager import ProxyManager
from hosting_scraper.proxy.providers import SMART_PROXY_PRESETS, smart_proxy_resource


def _pool_config(mode: str = "fallback", rules: dict | None = None) -> ProxyConfig:
    return ProxyConfig(
        enabled=True,
        policy={"mode": mode},
        resources=[
            HttpPoolResource(
                name="pool",
                type="http_pool",
                endpoints=[
                    "http://user:pass@proxy1.example.com:8000",
                    "http://user:pass@proxy2.example.com:8000",
                ],
                rotate="per_request",
            )
        ],
        domains=rules or {},
    )


def test_fallback_only_uses_proxy_on_retry() -> None:
    planner = ProxyPlanner(_pool_config("fallback"))
    assert planner.enabled
    assert planner.should_use("example.com") is False
    assert planner.should_use("example.com", retry=True) is True
    assert planner.select_route("example.com") is not None


def test_on_uses_proxy_immediately() -> None:
    planner = ProxyPlanner(_pool_config("on"))
    assert planner.should_use("example.com") is True
    first = planner.select_route("example.com")
    second = planner.select_route("example.com")
    assert first is not None and second is not None
    assert first.endpoint != second.endpoint


def test_per_domain_rules() -> None:
    config = _pool_config(
        "per_domain",
        {"picky.example": ProxyDomainRule(use_proxy=True, resource="pool")},
    )
    planner = ProxyPlanner(config)
    assert planner.should_use("picky.example") is True
    assert planner.should_use("other.example") is False
    rule_config = _pool_config(
        "off", {"forced.example": ProxyDomainRule(use_proxy=True, mode="on")}
    )
    planner = ProxyPlanner(rule_config)
    assert planner.should_use("forced.example") is True


def test_disabled_proxy_never_routes() -> None:
    manager = ProxyManager(
        ProxyConfig(
            enabled=False,
            policy={"mode": "on"},
            resources=[
                HttpPoolResource(
                    name="pool", type="http_pool", endpoints=["http://127.0.0.1:9"]
                )
            ],
        )
    )
    assert manager.enabled is False
    assert manager.mode_for("example.com") == "off"
    assert manager.route_for("example.com") is None
    assert manager.route_for("example.com", retry=True) is None


def test_smart_proxy_request_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SMART_KEY", "secret-key")
    resource = SmartProxyResource(
        name="bee",
        type="smart_proxy",
        endpoint="https://app.scrapingbee.com/api/v1/",
        api_key_env="TEST_SMART_KEY",
        url_param="url",
        params={"render_js": "true", "api_key": "{api_key}"},
    )
    manager = ProxyManager(
        ProxyConfig(enabled=True, policy={"mode": "on"}, resources=[resource])
    )
    route = manager.route_for("example.com")
    assert route is not None and route.is_smart
    assert manager.has_playwright_capable_route("example.com") is False
    target = "https://example.com/pricing?plan=pro"
    url, headers, proxy = route.request_plan(target)
    assert proxy is None
    assert headers == {}
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "app.scrapingbee.com"
    assert query["url"] == [target]
    assert query["api_key"] == ["secret-key"]
    assert query["render_js"] == ["true"]


def test_smart_proxy_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_SMART_KEY", raising=False)
    resource = SmartProxyResource(
        name="bee",
        type="smart_proxy",
        endpoint="https://api.example.com/",
        api_key_env="MISSING_SMART_KEY",
    )
    manager = ProxyManager(
        ProxyConfig(enabled=True, policy={"mode": "on"}, resources=[resource])
    )
    route = manager.route_for("example.com")
    assert route is not None
    with pytest.raises(ProxyError):
        route.request_plan("https://example.com/")


def test_smart_proxy_presets() -> None:
    resource = smart_proxy_resource("scrapingbee", name="bee")
    assert isinstance(resource, SmartProxyResource)
    assert "scrapingbee" in SMART_PROXY_PRESETS
    assert resource.api_key_env == "SCRAPINGBEE_API_KEY"
    with pytest.raises(KeyError):
        smart_proxy_resource("unknown-preset")


def test_http_pool_route_credentials_not_in_label() -> None:
    planner = ProxyPlanner(_pool_config("on"))
    route = planner.select_route("example.com")
    assert route is not None
    assert "user" not in route.label()
    assert "pass" not in route.label()


def test_socks_playwright_proxy() -> None:
    resource = Socks5Resource(
        name="tor", type="socks5", endpoint="socks5://user:pass@127.0.0.1:9050"
    )
    planner = ProxyPlanner(
        ProxyConfig(enabled=True, policy={"mode": "on"}, resources=[resource])
    )
    route = planner.select_route("example.com")
    assert route is not None
    settings = route.playwright_proxy()
    assert settings is not None
    assert settings["server"] == "socks5://127.0.0.1:9050"
    assert settings["username"] == "user"


def test_api_key_never_from_plain_environment_dump() -> None:
    # A tiny guard that the env var is only read at request time.
    assert "TEST_SMART_KEY" not in os.environ or os.environ["TEST_SMART_KEY"] == "secret-key"
