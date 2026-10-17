"""Configuration loading, validation and per-domain resolution.

Everything a run depends on is expressed in YAML (see ``config.example.yml``).
This module turns that YAML into typed, validated models and provides helpers to
derive an effective site configuration from global defaults plus ``overrides``.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

DEFAULT_PROVIDER_URL = (
    "https://raw.githubusercontent.com/certbot/website/master/_data/hosting_providers.json"
)

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when a configuration file is invalid or cannot be loaded."""


Base = BaseModel


class StrictModel(Base):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _empty_sections_use_defaults(cls, data):
        """Treat ``section:`` with no content as an empty/default section."""
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        for name, field_info in cls.model_fields.items():
            if name in cleaned and cleaned[name] is None:
                if field_info.default_factory is not None:
                    cleaned.pop(name)
                elif field_info.default is not None:
                    cleaned.pop(name)
        return cleaned


# --------------------------------------------------------------------------- #
# app / logging
# --------------------------------------------------------------------------- #
class AppConfig(StrictModel):
    name: str = "hosting-scraper"
    user_agent: str = "hosting-scraper/1.0 (+https://github.com/aminavua/autoscrap)"
    timezone: str = "UTC"


class LoggingConfig(StrictModel):
    level: str = "INFO"
    json_logs: bool = False

    @field_validator("level")
    @classmethod
    def _level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"logging.level must be one of {sorted(allowed)}")
        return upper


# --------------------------------------------------------------------------- #
# providers source
# --------------------------------------------------------------------------- #
class CacheConfig(StrictModel):
    enabled: bool = True
    path: str = ".cache/hosting_providers.json"
    ttl_hours: float = Field(default=24, ge=0)


class ProviderUrlsConfig(StrictModel):
    """Controls which URLs inside a registry entry become crawl seeds.

    Certbot's registry may carry up to four URLs per provider: ``link``,
    ``announcement``, ``plan`` and ``tutorial``. By default only the provider
    homepage (``link``) is crawled. Set ``include_extra_urls: true`` to also
    crawl plan/announcement pages, or list the fields explicitly in
    ``extra_url_fields``.
    """

    include_extra_urls: bool = False
    extra_url_fields: list[str] = Field(default_factory=lambda: ["announcement", "plan"])
    # Extra URLs are only followed when they stay on the provider's own site.
    same_site_only: bool = True
    # Used to recover a domain when ``link`` is empty, in priority order.
    base_url_from: list[str] = Field(
        default_factory=lambda: ["link", "plan", "announcement", "tutorial"]
    )

    @field_validator("extra_url_fields")
    @classmethod
    def _extra_fields(cls, value: list[str]) -> list[str]:
        allowed = {"link", "announcement", "plan", "tutorial"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown registry URL fields: {sorted(unknown)}")
        return value

    @field_validator("base_url_from")
    @classmethod
    def _base_fields(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("providers_source.urls.base_url_from cannot be empty")
        allowed = {"link", "announcement", "plan", "tutorial"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown registry URL fields: {sorted(unknown)}")
        return value


class ProviderOverride(StrictModel):
    """Per-provider override, keyed by provider name or domain in YAML."""

    base_url: str | None = None
    include: bool = True
    # Literal extra seed URLs (e.g. a hidden pricing page).
    extra_urls: list[str] = Field(default_factory=list)
    # Registry fields to seed in addition to the base URL.
    extra_url_fields: list[str] = Field(default_factory=list)
    category: str | None = None

    @field_validator("extra_url_fields")
    @classmethod
    def _extra_fields(cls, values: list[str]) -> list[str]:
        allowed = {"link", "announcement", "plan", "tutorial"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown registry URL fields: {sorted(unknown)}")
        return values


class ProvidersSourceConfig(StrictModel):
    type: Literal["certbot_github", "url", "file"] = "certbot_github"
    url: str = DEFAULT_PROVIDER_URL
    path: str | None = None
    cache: CacheConfig = Field(default_factory=CacheConfig)
    urls: ProviderUrlsConfig = Field(default_factory=ProviderUrlsConfig)
    overrides: dict[str, ProviderOverride] = Field(default_factory=dict)
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_source(self) -> ProvidersSourceConfig:
        if self.type == "file" and not self.path:
            raise ValueError("providers_source.path is required when type is 'file'")
        return self


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
class CrawlConfig(StrictModel):
    max_sites: int = Field(default=0, ge=0)
    concurrency: int = Field(default=8, ge=1)
    per_domain_concurrency: int = Field(default=2, ge=1)
    delay_seconds: float = Field(default=0.5, ge=0)
    timeout_seconds: float = Field(default=30, gt=0)
    retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=1.0, ge=0)
    respect_robots_txt: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    prefer_https: bool = True
    max_redirects: int = Field(default=8, ge=0)

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def _normalize_domains(cls, values: list[str]) -> list[str]:
        return [normalize_domain(v) for v in values if v and v.strip()]


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
DEFAULT_COMMON_PATHS = [
    "/pricing",
    "/plans",
    "/plan",
    "/pricing/",
    "/web-hosting",
    "/hosting",
    "/vps",
    "/wordpress-hosting",
    "/cloud-hosting",
    "/dedicated",
    "/shared-hosting",
    "/reseller-hosting",
]

DEFAULT_URL_KEYWORDS = [
    "pricing",
    "plans",
    "hosting",
    "vps",
    "dedicated",
    "reseller",
    "wordpress",
    "cloud",
    "deal",
    "offer",
    "black-friday",
    "sale",
]

DEFAULT_ANCHOR_KEYWORDS = ["pricing", "plans", "compare", "packages", "rates", "buy"]


class KeywordsConfig(StrictModel):
    url: list[str] = Field(default_factory=lambda: list(DEFAULT_URL_KEYWORDS))
    anchor_text: list[str] = Field(default_factory=lambda: list(DEFAULT_ANCHOR_KEYWORDS))


class PageSignalsConfig(StrictModel):
    currency_symbols: list[str] = Field(default_factory=lambda: ["$", "€", "£", "₹", "¥", "₦"])
    period_tokens: list[str] = Field(
        default_factory=lambda: [
            "/mo",
            "per month",
            "monthly",
            "per year",
            "annually",
            "billed",
            "/yr",
            "yearly",
        ]
    )


class DiscoveryConfig(StrictModel):
    max_candidate_pages_per_site: int = Field(default=8, ge=1)
    common_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_COMMON_PATHS))
    keywords: KeywordsConfig = Field(default_factory=KeywordsConfig)
    page_signals: PageSignalsConfig = Field(default_factory=PageSignalsConfig)
    max_links_to_scan: int = Field(default=80, ge=1)

    @field_validator("common_paths")
    @classmethod
    def _leading_slash(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                continue
            out.append(value if value.startswith("/") else f"/{value}")
        return out


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
class ToggleBillingConfig(StrictModel):
    enabled: bool = True
    # Which billing cadence to select before extraction.
    target: Literal["monthly", "yearly"] = "monthly"
    labels: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "monthly": ["Monthly", "Per month", "Month", "mo"],
            "yearly": ["Yearly", "Annually", "Per year", "Annual", "yr"],
        }
    )


class PlaywrightConfig(StrictModel):
    enabled: bool = True
    headless: bool = True
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    navigation_timeout_seconds: float = Field(default=45, gt=0)
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "networkidle"
    wait_after_load_ms: int = Field(default=750, ge=0)
    toggle_billing: ToggleBillingConfig = Field(default_factory=ToggleBillingConfig)
    block_resource_types: list[str] = Field(
        default_factory=lambda: ["image", "media", "font"]
    )
    # Extra Chromium arguments, e.g. ["--disable-blink-features=AutomationControlled"]
    browser_args: list[str] = Field(default_factory=list)


class HttpFetchConfig(StrictModel):
    verify_tls: bool = True
    follow_redirects: bool = True


class FetchConfig(StrictModel):
    mode: Literal["auto", "http", "playwright"] = "auto"
    playwright: PlaywrightConfig = Field(default_factory=PlaywrightConfig)
    http: HttpFetchConfig = Field(default_factory=HttpFetchConfig)

    @model_validator(mode="after")
    def _check_mode(self) -> FetchConfig:
        if self.mode == "playwright" and not self.playwright.enabled:
            raise ValueError(
                "fetch.mode is 'playwright' but fetch.playwright.enabled is false; "
                "enable it or choose mode 'auto'"
            )
        return self


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
class NormalizeConfig(StrictModel):
    infer_billing_term_months: bool = True
    # Used when a page shows a price without any cadence token. "unknown"
    # leaves billing_period empty instead of guessing.
    default_billing_period: Literal["month", "year", "unknown"] = "month"
    monthly_tokens: list[str] = Field(
        default_factory=lambda: ["/mo", "/month", "per month", "monthly", "mo", "month"]
    )
    yearly_tokens: list[str] = Field(
        default_factory=lambda: ["/yr", "/year", "per year", "yearly", "annual", "annually", "yr"]
    )
    dedupe: bool = True


class ExtractionConfig(StrictModel):
    enabled_extractors: list[str] = Field(
        default_factory=lambda: ["schema_org", "dom_tables", "dom_cards", "xhr_json"]
    )
    min_confidence_to_accept: float = Field(default=0.55, ge=0, le=1)
    currency_default: str = "USD"
    max_packages_per_page: int = Field(default=120, ge=1)
    normalize: NormalizeConfig = Field(default_factory=NormalizeConfig)

    @field_validator("enabled_extractors")
    @classmethod
    def _known_extractors(cls, values: list[str]) -> list[str]:
        allowed = {"schema_org", "dom_tables", "dom_cards", "xhr_json"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown extractors: {sorted(unknown)}")
        return values

    @field_validator("currency_default")
    @classmethod
    def _currency(cls, value: str) -> str:
        value = value.upper().strip()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("extraction.currency_default must be a 3-letter ISO 4217 code")
        return value


# --------------------------------------------------------------------------- #
# proxy
# --------------------------------------------------------------------------- #
class ProxyResourceBase(StrictModel):
    name: str
    enabled: bool = True


class HttpPoolResource(ProxyResourceBase):
    type: Literal["http_pool"]
    endpoints: list[str] = Field(default_factory=list)
    rotate: Literal["per_request", "per_domain"] = "per_request"

    @field_validator("endpoints")
    @classmethod
    def _endpoints(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith(("http://", "https://")):
                raise ValueError(f"http_pool endpoint must start with http:// or https://: {value!r}")
        return values


class Socks5Resource(ProxyResourceBase):
    type: Literal["socks5"]
    endpoint: str

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        if not value.startswith("socks5://"):
            raise ValueError("socks5 endpoint must start with socks5://")
        return value


class SmartProxyResource(ProxyResourceBase):
    """A hosted scraping API (Zyte, ScrapingBee, ScraperAPI, ...).

    The engine sends ``GET {endpoint}`` with the target URL in ``url_param`` and
    credentials from an environment variable. ``params`` is interpolated with
    ``{url}`` and ``{api_key}`` placeholders.
    """

    type: Literal["smart_proxy"]
    endpoint: str
    api_key_env: str | None = None
    api_key: str | None = None  # discouraged: prefer api_key_env
    url_param: str = "url"
    params: dict[str, str] = Field(default_factory=dict)
    header_name: str | None = None
    header_format: str = "Bearer {api_key}"


ProxyResource = HttpPoolResource | Socks5Resource | SmartProxyResource


class ProxyPolicyConfig(StrictModel):
    mode: Literal["off", "on", "fallback", "per_domain"] = "fallback"

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_booleans(cls, value: object) -> object:
        # PyYAML (YAML 1.1) parses unquoted `on`/`off`/`yes`/`no` as booleans,
        # so `mode: on` would otherwise fail enum validation.
        if value is True:
            return "on"
        if value is False:
            return "off"
        return value
    blocked_statuses: list[int] = Field(default_factory=lambda: [403, 407, 429, 503])
    challenge_markers: list[str] = Field(
        default_factory=lambda: [
            "attention required",
            "just a moment",
            "cf-challenge",
            "captcha",
            "are you a human",
            "enable javascript and cookies",
            "access denied",
            "unusual traffic",
        ]
    )
    retry_with_playwright: bool = True


class ProxyDomainRule(StrictModel):
    use_proxy: bool = True
    resource: str | None = None
    mode: Literal["off", "on", "fallback"] | None = None


class ProxyConfig(StrictModel):
    enabled: bool = False
    policy: ProxyPolicyConfig = Field(default_factory=ProxyPolicyConfig)
    resources: list[ProxyResource] = Field(default_factory=list)
    domains: dict[str, ProxyDomainRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_resources(self) -> ProxyConfig:
        names = [r.name for r in self.resources]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate proxy resource names: {sorted(duplicates)}")
        if self.enabled and self.policy.mode in {"on", "fallback", "per_domain"}:
            if not any(r.enabled for r in self.resources):
                raise ValueError("proxy.enabled is true but no enabled proxy resources are configured")
        return self

    def resource_by_name(self, name: str) -> ProxyResource | None:
        for resource in self.resources:
            if resource.name == name:
                return resource
        return None


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
class FormatsConfig(StrictModel):
    jsonl: bool = True
    csv: bool = False


class PostgresConfig(StrictModel):
    enabled: bool = False
    dsn: str | None = None
    packages_table: str = "hosting_packages"
    offers_table: str = "hosting_offers"


class StorageConfig(StrictModel):
    output_dir: str = "runs"
    save_raw_html: bool = True
    save_screenshots: bool = False
    save_extracted_json: bool = True
    formats: FormatsConfig = Field(default_factory=FormatsConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)

    @model_validator(mode="after")
    def _check_postgres(self) -> StorageConfig:
        if self.postgres.enabled and not self.postgres.dsn:
            raise ValueError("storage.postgres.dsn is required when postgres is enabled")
        return self


# --------------------------------------------------------------------------- #
# top-level config
# --------------------------------------------------------------------------- #
class SiteSettings(StrictModel):
    """Effective crawl/discovery/fetch/extraction settings for one domain."""

    crawl: CrawlConfig
    discovery: DiscoveryConfig
    fetch: FetchConfig
    extraction: ExtractionConfig


class AppSettings(StrictModel):
    app: AppConfig = Field(default_factory=AppConfig)
    providers_source: ProvidersSourceConfig = Field(default_factory=ProvidersSourceConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    _config_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check_overrides(self) -> AppSettings:
        unknown = set(self.overrides) - {"sites"}
        if unknown:
            raise ValueError(f"unknown overrides section(s): {sorted(unknown)}")
        sites = self.overrides.get("sites") or {}
        if not isinstance(sites, dict):
            raise ValueError("overrides.sites must be a mapping of domain -> settings")
        for domain, block in sites.items():
            if block is None:
                continue
            if not isinstance(block, dict):
                raise ValueError(f"overrides.sites[{domain!r}] must be a mapping")
            unexpected = set(block) - {"crawl", "discovery", "fetch", "extraction"}
            if unexpected:
                raise ValueError(
                    f"overrides.sites[{domain!r}] has unknown section(s): {sorted(unexpected)}"
                )
        return self

    # -- helpers ---------------------------------------------------------- #
    @property
    def config_dir(self) -> Path:
        return self._config_dir

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self._config_dir / path
        return path

    @property
    def output_dir(self) -> Path:
        return self.resolve_path(self.storage.output_dir)

    def override_for(self, domain: str) -> dict[str, Any]:
        """Return the override block matching ``domain`` (exact, parent, or ``*``)."""
        sites = self.overrides.get("sites") or {}
        if not isinstance(sites, dict):
            raise ConfigError("overrides.sites must be a mapping of domain -> settings")
        domain = normalize_domain(domain)
        if domain in sites:
            return copy.deepcopy(sites[domain] or {})
        labels = domain.split(".")
        for index in range(1, len(labels) - 1):
            parent = ".".join(labels[index:])
            if parent in sites:
                return copy.deepcopy(sites[parent] or {})
        if "*" in sites:
            return copy.deepcopy(sites["*"] or {})
        return {}

    def for_domain(self, domain: str) -> SiteSettings:
        """Merge global settings with the matching ``overrides.sites`` block."""
        base: dict[str, Any] = {
            "crawl": self.crawl.model_dump(),
            "discovery": self.discovery.model_dump(),
            "fetch": self.fetch.model_dump(),
            "extraction": self.extraction.model_dump(),
        }
        deep_merge(base, self.override_for(domain))
        # Only site-relevant sections can be overridden; keep errors readable.
        unknown = set(base) - {"crawl", "discovery", "fetch", "extraction"}
        if unknown:  # pragma: no cover - deep_merge cannot add top-level keys here
            raise ConfigError(f"unsupported site override sections: {sorted(unknown)}")
        return SiteSettings(**base)

    def dump(self, *, redact: bool = False) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if redact:
            _redact(data)
        return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (lists are replaced)."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def normalize_domain(value: str) -> str:
    """Normalize a domain or URL to a bare lower-case host name."""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        value = urlsplit(value).netloc
    else:
        value = value.split("/", 1)[0]
    value = value.split("@")[-1].split(":", 1)[0].strip(".").lower()
    if value.startswith("www."):
        value = value[4:]
    return value


def _expand_env(value: Any, *, path: str = "") -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item, path=f"{path}.{key}".strip(".")) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item, path=f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(
                f"environment variable {name!r} referenced at {path or '<root>'} is not set"
            )

        return ENV_PATTERN.sub(replace, value)
    return value


def _redact(data: Any) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                _redact(value)
            elif key in {"api_key", "dsn"} and isinstance(value, str) and value:
                data[key] = "***"
            elif isinstance(value, str):
                data[key] = re.sub(r"//[^/@\s]+@", "//***@", value)
    elif isinstance(data, list):
        for index, item in enumerate(data):
            if isinstance(item, str):
                data[index] = re.sub(r"//[^/@\s]+@", "//***@", item)
            else:
                _redact(item)


def load_config(path: str | Path) -> AppSettings:
    """Load, expand and validate a YAML config file."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")
    expanded = _expand_env(raw)
    # Backwards/forwards friendly aliases kept intentionally small.
    if "provider_source" in expanded and "providers_source" not in expanded:
        expanded["providers_source"] = expanded.pop("provider_source")
    try:
        settings = AppSettings(**expanded)
    except Exception as exc:  # pydantic ValidationError or ValueError
        raise ConfigError(f"invalid configuration in {config_path}: {exc}") from exc
    settings._config_dir = config_path.parent
    settings._raw = expanded
    return settings


def write_default_config(path: str | Path) -> Path:
    """Copy the bundled example config to ``path`` (refuses to overwrite)."""
    target = Path(path).expanduser()
    if target.exists():
        raise ConfigError(f"refusing to overwrite existing file: {target}")
    source = Path(__file__).resolve().parent / "data" / "config.example.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def apply_dotted_overrides(settings: AppSettings, assignments: list[str]) -> AppSettings:
    """Apply ``--set a.b=c`` style overrides then re-validate the config."""
    if not assignments:
        return settings
    data = copy.deepcopy(settings.dump())
    for assignment in assignments:
        if "=" not in assignment:
            raise ConfigError(f"--set expects key=value, got {assignment!r}")
        dotted, raw_value = assignment.split("=", 1)
        value: Any = yaml.safe_load(raw_value)
        cursor = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = cursor.get(part)
            if not isinstance(node, dict):
                node = {}
                cursor[part] = node
            cursor = node
        cursor[parts[-1]] = value
    expanded = _expand_env(data)
    try:
        updated = AppSettings(**expanded)
    except Exception as exc:
        raise ConfigError(f"invalid --set override: {exc}") from exc
    updated._config_dir = settings._config_dir
    updated._raw = expanded
    return updated
