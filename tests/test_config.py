from __future__ import annotations

from pathlib import Path

import pytest

from hosting_scraper.config import (
    AppSettings,
    ConfigError,
    apply_dotted_overrides,
    load_config,
)

REPO = Path(__file__).resolve().parents[1]


def test_example_config_loads() -> None:
    settings = load_config(REPO / "config.example.yml")
    assert settings.app.name == "hosting-scraper"
    assert settings.proxy.enabled is False
    assert settings.extraction.min_confidence_to_accept == 0.55
    site = settings.for_domain("www.hostinger.com")
    assert site.discovery.max_candidate_pages_per_site == 12
    assert site.fetch.mode == "playwright"


def test_packaged_example_is_identical() -> None:
    packaged = REPO / "src" / "hosting_scraper" / "data" / "config.example.yml"
    assert packaged.read_text(encoding="utf-8") == (REPO / "config.example.yml").read_text(
        encoding="utf-8"
    )


def test_env_expansion_and_missing_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yml"
    config.write_text(
        "app:\n  user_agent: \"${TEST_UA:-fallback-agent}\"\n"
        "providers_source:\n  type: file\n  path: providers.json\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_UA", raising=False)
    settings = load_config(config)
    assert settings.app.user_agent == "fallback-agent"
    monkeypatch.setenv("TEST_UA", "agent-from-env")
    assert load_config(config).app.user_agent == "agent-from-env"

    config.write_text(
        "app:\n  user_agent: \"${DEFINITELY_MISSING_VAR}\"\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="DEFINITELY_MISSING_VAR"):
        load_config(config)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text("crawl:\n  bogus_option: true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="bogus_option"):
        load_config(config)


def test_empty_sections_fall_back_to_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text("proxy:\n  domains:\n  resources:\n", encoding="utf-8")
    settings = load_config(config)
    assert settings.proxy.domains == {}
    assert settings.proxy.resources == []


def test_site_override_parent_domain(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text(
        "overrides:\n"
        "  sites:\n"
        "    example.com:\n"
        "      crawl:\n"
        "        delay_seconds: 3\n"
        "      discovery:\n"
        "        max_candidate_pages_per_site: 2\n",
        encoding="utf-8",
    )
    settings = load_config(config)
    site = settings.for_domain("shop.example.com")
    assert site.crawl.delay_seconds == 3
    assert site.discovery.max_candidate_pages_per_site == 2
    assert settings.for_domain("unrelated.dev").crawl.delay_seconds != 3


def test_dotted_overrides() -> None:
    settings = load_config(REPO / "config.example.yml")
    updated = apply_dotted_overrides(settings, ["fetch.mode=http", "crawl.max_sites=3"])
    assert updated.fetch.mode == "http"
    assert updated.crawl.max_sites == 3


def test_redaction() -> None:
    settings = AppSettings(
        proxy={
            "enabled": True,
            "policy": {"mode": "on"},
            "resources": [
                {
                    "name": "pool",
                    "type": "http_pool",
                    "enabled": True,
                    "endpoints": ["http://user:secret@proxy.example.com:8000"],
                }
            ],
        },
        storage={"postgres": {"enabled": True, "dsn": "postgresql://user:secret@host/db"}},
    )
    dumped = settings.dump(redact=True)
    assert "secret" not in str(dumped)
    assert "***" in dumped["proxy"]["resources"][0]["endpoints"][0]


def test_proxy_requires_resources_when_enabled() -> None:
    with pytest.raises(ValueError, match="no enabled proxy resources"):
        AppSettings(
            proxy={"enabled": True, "policy": {"mode": "fallback"}, "resources": []}
        )


def test_unknown_site_override_section_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text(
        "overrides:\n  sites:\n    example.com:\n      scraper:\n        nope: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown section"):
        load_config(config)


def test_unknown_top_level_override_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text("overrides:\n  domains:\n    example.com: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown overrides section"):
        load_config(config)


def test_unquoted_yaml_on_off_proxy_mode(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text(
        "proxy:\n  policy:\n    mode: on\n",
        encoding="utf-8",
    )
    settings = load_config(config)
    assert settings.proxy.policy.mode == "on"
