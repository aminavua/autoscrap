"""Presets for hosted scraping APIs ("smart proxies").

These are convenience templates for ``proxy.resources`` entries. They are not
magic: you still need an account and an API key in an environment variable.
"""

from __future__ import annotations

from typing import Any

from hosting_scraper.config import SmartProxyResource

SMART_PROXY_PRESETS: dict[str, dict[str, Any]] = {
    "scrapingbee": {
        "endpoint": "https://app.scrapingbee.com/api/v1/",
        "api_key_env": "SCRAPINGBEE_API_KEY",
        "url_param": "url",
        "params": {"render_js": "true", "api_key": "{api_key}"},
    },
    "scraperapi": {
        "endpoint": "http://api.scraperapi.com/",
        "api_key_env": "SCRAPERAPI_KEY",
        "url_param": "url",
        "params": {"api_key": "{api_key}", "render": "false"},
    },
    "scrapingant": {
        "endpoint": "https://api.scrapingant.com/v2/general",
        "api_key_env": "SCRAPINGANT_API_KEY",
        "url_param": "url",
        "params": {"x-api-key": "{api_key}"},
    },
}


def smart_proxy_resource(preset: str, *, name: str | None = None, **overrides: Any) -> SmartProxyResource:
    """Build a validated ``smart_proxy`` resource from a preset name."""
    key = preset.lower().strip()
    if key not in SMART_PROXY_PRESETS:
        raise KeyError(f"unknown smart proxy preset {preset!r}; choose from {sorted(SMART_PROXY_PRESETS)}")
    data = dict(SMART_PROXY_PRESETS[key])
    data.pop("note", None)
    data.update(overrides)
    data.setdefault("name", name or f"smart-{key}")
    data["type"] = "smart_proxy"
    return SmartProxyResource(**data)
