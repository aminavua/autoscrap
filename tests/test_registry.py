from __future__ import annotations

import json
from pathlib import Path

import pytest

from hosting_scraper.config import CacheConfig, ProviderOverride, ProvidersSourceConfig
from hosting_scraper.providers import certbot_registry
from hosting_scraper.providers.certbot_registry import (
    load_providers,
    normalize_providers,
)

ENTRIES = [
    {
        "name": "Alpha Host",
        "link": "",
        "category": "full",
        "announcement": "",
        "plan": "https://alpha-host.com/webhosting/",
        "tutorial": "",
        "reviewed": "2025.01.01",
        "note": "",
    },
    {
        "name": "Beta Host",
        "link": "http://www.beta-host.com",
        "category": "partial",
        "announcement": "https://beta-host.com/deals",
        "plan": "https://beta-host.com/pricing",
        "tutorial": "https://docs.other-site.org/beta",
        "reviewed": "2025.02.02",
        "note": "free ssl",
    },
    {
        "name": "Cloudways",
        "link": "",
        "category": "no",
        "announcement": "",
        "plan": "",
        "tutorial": "",
        "reviewed": "",
        "note": "",
    },
    {
        "name": "Duplicate",
        "link": "https://www.beta-host.com/",
        "category": "full",
        "announcement": "",
        "plan": "",
        "tutorial": "",
        "reviewed": "",
        "note": "",
    },
]


def _config(**overrides) -> ProvidersSourceConfig:
    data = {"type": "file", "path": "providers.json"}
    data.update(overrides)
    return ProvidersSourceConfig(**data)


def test_url_recovery_and_homepage_only() -> None:
    config = _config()
    load = normalize_providers(ENTRIES, config)
    by_name = {provider.name: provider for provider in load.providers}
    assert by_name["Alpha Host"].domain == "alpha-host.com"
    assert by_name["Alpha Host"].seed_url == "https://alpha-host.com"
    assert by_name["Beta Host"].seed_url == "https://www.beta-host.com/"
    assert by_name["Beta Host"].seed_urls == ["https://www.beta-host.com/"]
    assert by_name["Beta Host"].registry_urls["plan"] == "https://beta-host.com/pricing"
    assert "Cloudways" not in by_name
    assert any(skip["name"] == "Cloudways" for skip in load.skipped)
    assert any("duplicate" in skip["reason"] for skip in load.skipped)


def test_extra_urls_are_optional_and_same_site() -> None:
    config = _config(
        urls={
            "include_extra_urls": True,
            "extra_url_fields": ["announcement", "plan", "tutorial"],
        }
    )
    load = normalize_providers(ENTRIES, config)
    beta = next(provider for provider in load.providers if provider.name == "Beta Host")
    assert beta.seed_urls == [
        "https://www.beta-host.com/",
        "https://beta-host.com/deals",
        "https://beta-host.com/pricing",
    ]
    assert not any("other-site.org" in url for url in beta.seed_urls)
    assert any("another site" in warning for warning in load.warnings)


def test_override_by_provider_name_adds_domain() -> None:
    config = _config(
        overrides={"Cloudways": ProviderOverride(base_url="https://www.cloudways.com/")}
    )
    load = normalize_providers(ENTRIES, config)
    cloudways = next(provider for provider in load.providers if provider.name == "Cloudways")
    assert cloudways.domain == "cloudways.com"
    assert cloudways.seed_url == "https://www.cloudways.com/"


def test_exclude() -> None:
    config = _config(exclude=["beta-host.com"])
    load = normalize_providers(ENTRIES, config)
    names = {provider.name for provider in load.providers}
    assert "Beta Host" not in names
    assert "Alpha Host" in names


def test_load_from_file(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(ENTRIES), encoding="utf-8")
    config = _config(path=str(path))
    load = load_providers(
        config,
        cache_path=tmp_path / "cache.json",
        user_agent="test-agent",
        base_dir=tmp_path,
    )
    assert load.source.endswith("registry.json")
    assert len(load.providers) == 2


def test_cache_then_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = [
        {
            "name": "Cached",
            "link": "https://cached.example.com/",
            "category": "full",
            "announcement": "",
            "plan": "",
            "tutorial": "",
            "reviewed": "",
            "note": "",
        }
    ]
    monkeypatch.setattr(
        certbot_registry, "fetch_registry_text", lambda *args, **kwargs: json.dumps(cached)
    )
    cache_path = tmp_path / "cache" / "providers.json"
    config = _config(
        type="url",
        url="https://example.com/registry.json",
        cache=CacheConfig(enabled=True, path=str(cache_path), ttl_hours=24),
    )
    first = load_providers(config, cache_path=cache_path, user_agent="test")
    assert first.from_cache is False
    assert cache_path.exists()

    def fail(*args, **kwargs):
        raise certbot_registry.RegistryError("offline")

    monkeypatch.setattr(certbot_registry, "fetch_registry_text", fail)
    second = load_providers(config, cache_path=cache_path, user_agent="test")
    assert second.from_cache is True
    assert second.providers[0].domain == "cached.example.com"


def test_bundled_snapshot_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise certbot_registry.RegistryError("offline")

    monkeypatch.setattr(certbot_registry, "fetch_registry_text", fail)
    config = _config(
        type="url",
        url="https://example.com/registry.json",
        cache=CacheConfig(enabled=False),
    )
    load = load_providers(
        config,
        cache_path=tmp_path / "missing.json",
        user_agent="test",
        use_snapshot=True,
    )
    assert len(load.providers) > 50
    assert any("snapshot" in warning for warning in load.warnings)


def test_snapshot_shape() -> None:
    text = certbot_registry.SNAPSHOT_PATH.read_text(encoding="utf-8")
    data = json.loads(text)
    assert isinstance(data, list) and len(data) > 200


def test_tutorial_only_entry_seeds_site_root() -> None:
    entries = [
        {
            "name": "Docs Host",
            "link": "",
            "category": "partial",
            "announcement": "",
            "plan": "",
            "tutorial": "https://blog.docs-host.com/lets-encrypt-setup",
            "reviewed": "",
            "note": "",
        }
    ]
    load = normalize_providers(entries, _config())
    provider = load.providers[0]
    assert provider.domain == "docs-host.com"
    assert provider.seed_url == "https://docs-host.com"


def test_provider_override_extra_urls() -> None:
    config = _config(
        overrides={
            "Beta Host": ProviderOverride(
                extra_url_fields=["plan"],
                extra_urls=["https://beta-host.com/hidden-pricing"],
            )
        }
    )
    load = normalize_providers(ENTRIES, config)
    beta = next(provider for provider in load.providers if provider.name == "Beta Host")
    assert "https://beta-host.com/pricing" in beta.seed_urls
    assert "https://beta-host.com/hidden-pricing" in beta.seed_urls
