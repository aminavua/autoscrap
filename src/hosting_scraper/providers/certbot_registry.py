"""Load Certbot's hosting providers registry and normalize it into providers.

The upstream file is a JSON array where each entry looks like::

    {
      "name": "Snel.com",
      "link": "https://www.snel.com",
      "category": "full",
      "tutorial": "...",
      "announcement": "",
      "plan": "https://www.snel.com/webhosting/",
      "reviewed": "2022.11.17",
      "note": "Free & automated SSL for all-in web hosting plans"
    }

Some entries have no ``link`` at all, so we recover a domain from any other URL
in priority order. Entries that contain no usable URL are reported as skipped
and can be fixed with ``providers_source.overrides`` in the config.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from hosting_scraper.config import (
    ProviderOverride,
    ProvidersSourceConfig,
    normalize_domain,
)
from hosting_scraper.util.url import canonicalize, host_of, same_site

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "hosting_providers.json"
GITHUB_API = "https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
GITHUB_RAW = "https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
GITHUB_BLOB_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/blob/(?P<branch>[^/]+)/(?P<path>.+)$"
)
GITHUB_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+)$"
)


class RegistryError(RuntimeError):
    pass


class _CacheStale(RegistryError):
    pass


@dataclass
class Provider:
    name: str
    domain: str
    seed_url: str
    seed_urls: list[str] = field(default_factory=list)
    category: str = ""
    note: str = ""
    reviewed: str = ""
    registry_urls: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_free_category(self) -> bool:
        return self.category.lower() in {"no", "partial"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "seed_url": self.seed_url,
            "seed_urls": self.seed_urls,
            "category": self.category,
            "note": self.note,
            "reviewed": self.reviewed,
            "registry_urls": self.registry_urls,
        }


@dataclass
class ProviderLoad:
    providers: list[Provider]
    skipped: list[dict[str, str]]
    source: str
    fetched_at: str
    from_cache: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, *, include_providers: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": self.source,
            "fetched_at": self.fetched_at,
            "from_cache": self.from_cache,
            "provider_count": len(self.providers),
            "skipped_count": len(self.skipped),
            "skipped": self.skipped,
            "warnings": self.warnings,
        }
        if include_providers:
            data["providers"] = [p.to_dict() for p in self.providers]
        return data


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def _github_parts(url: str) -> tuple[str, str, str, str] | None:
    match = GITHUB_BLOB_RE.match(url) or GITHUB_RAW_RE.match(url)
    if not match:
        return None
    return (
        match.group("owner"),
        match.group("repo"),
        match.group("branch"),
        match.group("path"),
    )


def _http_get(url: str, *, user_agent: str, timeout: float) -> httpx.Response:
    headers = {"User-Agent": user_agent, "Accept": "application/vnd.github.raw, application/json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        return response


def _decode_github_response(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("encoding") == "base64":
            return base64.b64decode(payload.get("content", "")).decode("utf-8")
        # A proxy may return the raw JSON array with an application/json header.
        return json.dumps(payload, ensure_ascii=False)
    return response.text


def fetch_registry_text(
    url: str,
    *,
    user_agent: str,
    timeout: float = 30,
) -> str:
    """Fetch the registry, preferring the GitHub API for github.com blob URLs.

    The API is used first because it returns the file even when the raw host is
    not reachable, and it reports ``application/json`` reliably.
    """
    parts = _github_parts(url)
    errors: list[str] = []
    if parts:
        owner, repo, branch, path = parts
        api_url = GITHUB_API.format(owner=owner, repo=repo, path=path, branch=branch)
        try:
            return _decode_github_response(_http_get(api_url, user_agent=user_agent, timeout=timeout))
        except Exception as exc:  # noqa: BLE001 - fall back to raw
            errors.append(f"GitHub API failed: {exc}")
        raw_url = GITHUB_RAW.format(owner=owner, repo=repo, branch=branch, path=path)
        try:
            return _http_get(raw_url, user_agent=user_agent, timeout=timeout).text
        except Exception as exc:  # noqa: BLE001
            errors.append(f"raw.githubusercontent.com failed: {exc}")
    else:
        try:
            return _http_get(url, user_agent=user_agent, timeout=timeout).text
        except Exception as exc:  # noqa: BLE001
            errors.append(f"direct fetch failed: {exc}")
    raise RegistryError("; ".join(errors) or "could not fetch registry")


def _parse_registry(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry is not valid JSON: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("providers") or payload.get("hosting_providers") or []
    if not isinstance(payload, list):
        raise RegistryError("registry must be a JSON array of provider objects")
    entries = [entry for entry in payload if isinstance(entry, dict)]
    if not entries:
        raise RegistryError("registry contains no provider entries")
    return entries


# --------------------------------------------------------------------------- #
# caching
# --------------------------------------------------------------------------- #
def _read_cache(path: Path) -> tuple[list[dict[str, Any]], str, datetime]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    providers = raw.get("providers") if isinstance(raw, dict) else raw
    if not isinstance(providers, list):
        raise RegistryError("invalid provider cache")
    fetched_at = raw.get("fetched_at") if isinstance(raw, dict) else None
    timestamp = _parse_timestamp(fetched_at) or datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )
    return providers, raw.get("source", str(path)) if isinstance(raw, dict) else str(path), timestamp


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _write_cache(path: Path, providers: list[dict[str, Any]], source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": source,
        "providers": providers,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #
def _upgrade_to_https(value: str) -> str:
    """Upgrade http:// seeds unless that would break obvious local/test hosts."""
    if not value.startswith("http://"):
        return value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return value
    if not host or host == "localhost":
        return value
    try:
        ipaddress.ip_address(host)
        return value
    except ValueError:
        pass
    if port not in (None, 80):
        return value
    return "https://" + value[len("http://") :]


def _as_http_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if "://" not in value:
        value = "https://" + value
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return ""
    if not host or "." not in host:
        return ""
    return canonicalize(value)


def _override_lookup(
    overrides: dict[str, ProviderOverride], name: str, domains: list[str]
) -> ProviderOverride | None:
    name_key = name.lower().strip()
    domain_keys = {normalize_domain(domain) for domain in domains if domain}
    for key, value in overrides.items():
        raw = key.lower().strip()
        if raw == name_key or normalize_domain(key) in domain_keys:
            return value
    return None


def normalize_providers(
    entries: list[dict[str, Any]],
    config: ProvidersSourceConfig,
    *,
    prefer_https: bool = True,
    source: str = "certbot_github",
    fetched_at: str | None = None,
    from_cache: bool = False,
    warnings: list[str] | None = None,
) -> ProviderLoad:
    """Turn raw registry entries into providers, honoring config overrides."""
    providers: list[Provider] = []
    skipped: list[dict[str, str]] = []
    seen_domains: set[str] = set()
    exclude = {normalize_domain(v) for v in config.exclude if v}
    exclude_names = {v.lower().strip() for v in config.exclude if v}
    warnings = list(warnings or [])

    for entry in entries:
        name = str(entry.get("name") or "").strip()
        link = str(entry.get("link") or "").strip()
        inferred_domains = [normalize_domain(link)]
        for field_name in ("announcement", "plan", "tutorial"):
            inferred_domains.append(normalize_domain(str(entry.get(field_name) or "")))
        override = _override_lookup(config.overrides, name, inferred_domains)

        # Determine the seed URL. An explicit override or a real homepage link
        # keeps its exact URL/path. When the registry only has plan/announcement/
        # tutorial URLs, use them to recover the domain but seed the site root:
        # a help article is a poor landing page for price discovery.
        seed_url = ""
        seed_field = ""
        if override and override.base_url:
            seed_url = _as_http_url(override.base_url)
            seed_field = "override"
        if not seed_url:
            link_value = _as_http_url(str(entry.get("link") or ""))
            if link_value:
                seed_url = link_value
                seed_field = "link"
        if not seed_url:
            for field_name in config.urls.base_url_from:
                if field_name == "link":
                    continue
                value = _as_http_url(str(entry.get(field_name) or ""))
                if not value:
                    continue
                seed_url = value
                seed_field = field_name
                break
            if seed_url:
                parts = urlsplit(seed_url)
                host = parts.netloc
                if host.lower().startswith("blog."):
                    # A blog article is a poor pricing landing page.
                    host = host.split(".", 1)[1]
                seed_url = f"{parts.scheme}://{host}"
        if prefer_https:
            seed_url = _upgrade_to_https(seed_url)

        if not seed_url:
            # Some providers are listed only by name (which is often the domain).
            name_domain = normalize_domain(name)
            if name_domain and "." in name_domain and " " not in name:
                seed_url = f"https://{name_domain}/"
                seed_field = "name"
            else:
                skipped.append({"name": name, "reason": "no usable URL in registry entry"})
                continue

        domain = normalize_domain(override.base_url) if override and override.base_url else normalize_domain(seed_url)
        if not domain:
            skipped.append({"name": name, "reason": "could not derive a domain"})
            continue
        if override and not override.include:
            skipped.append({"name": name, "reason": "disabled by providers_source.overrides"})
            continue
        if domain in exclude or name.lower() in exclude_names:
            skipped.append({"name": name, "reason": "excluded by config"})
            continue
        if domain in seen_domains:
            skipped.append({"name": name, "reason": f"duplicate domain {domain}"})
            continue

        registry_urls: dict[str, str] = {}
        for field_name in ("link", "announcement", "plan", "tutorial"):
            value = _as_http_url(str(entry.get(field_name) or ""))
            if value:
                if prefer_https:
                    value = _upgrade_to_https(value)
                registry_urls[field_name] = value

        seed_urls = [seed_url]
        extra_fields = list(config.urls.extra_url_fields)
        if override and override.extra_url_fields:
            extra_fields = list(override.extra_url_fields)
        if config.urls.include_extra_urls or (override and (override.extra_urls or override.extra_url_fields)):
            for field_name in extra_fields:
                value = registry_urls.get(field_name)
                if not value:
                    continue
                if config.urls.same_site_only and not same_site(value, domain):
                    warnings.append(
                        f"{name}: ignoring {field_name} URL on another site ({host_of(value)})"
                    )
                    continue
                if value not in seed_urls:
                    seed_urls.append(value)
        for literal in (override.extra_urls if override else []):
            value = _as_http_url(literal)
            if not value:
                continue
            if config.urls.same_site_only and not same_site(value, domain):
                warnings.append(
                    f"{name}: ignoring extra seed URL on another site ({host_of(value)})"
                )
                continue
            if prefer_https:
                value = _upgrade_to_https(value)
            if value not in seed_urls:
                seed_urls.append(value)

        providers.append(
            Provider(
                name=name or domain,
                domain=domain,
                seed_url=seed_url,
                seed_urls=seed_urls,
                category=str(override.category if override and override.category else entry.get("category") or ""),
                note=str(entry.get("note") or ""),
                reviewed=str(entry.get("reviewed") or ""),
                registry_urls=registry_urls,
                raw={"seed_field": seed_field, **entry},
            )
        )
        seen_domains.add(domain)

    providers.sort(key=lambda p: p.name.lower())
    return ProviderLoad(
        providers=providers,
        skipped=skipped,
        source=source,
        fetched_at=fetched_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        from_cache=from_cache,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# public loader
# --------------------------------------------------------------------------- #
def load_providers(
    config: ProvidersSourceConfig,
    *,
    cache_path: Path,
    user_agent: str,
    timeout: float = 30,
    force_refresh: bool = False,
    use_snapshot: bool = True,
    base_dir: Path | None = None,
    prefer_https: bool = True,
) -> ProviderLoad:
    """Load providers from file/URL/cache, falling back to the bundled snapshot.

    Resolution order for ``certbot_github`` / ``url``:

    1. fresh cache (unless ``force_refresh``)
    2. remote registry
    3. stale cache (with a warning)
    4. bundled snapshot (with a warning)
    """
    if config.type == "file":
        path = Path(config.path or "").expanduser()
        if not path.is_absolute():
            path = (base_dir or Path.cwd()) / path
        text = path.read_text(encoding="utf-8")
        entries = _parse_registry(text)
        return normalize_providers(entries, config, source=str(path), prefer_https=prefer_https)

    cache_enabled = config.cache.enabled
    cache = cache_path
    cached_entries: list[dict[str, Any]] | None = None
    cached_at: datetime | None = None
    cached_source = ""
    cache_warning = ""
    if cache_enabled and cache.exists():
        try:
            cached_entries, cached_source, cached_at = _read_cache(cache)
            age_hours = (
                datetime.now(timezone.utc) - cached_at.astimezone(timezone.utc)
            ).total_seconds() / 3600
            if not force_refresh and age_hours <= config.cache.ttl_hours:
                return normalize_providers(
                    cached_entries,
                    config,
                    source=cached_source,
                    fetched_at=cached_at.isoformat(),
                    from_cache=True,
                    prefer_https=prefer_https,
                )
        except Exception as exc:  # noqa: BLE001 - corrupt cache should not abort
            cache_warning = f"could not read provider cache: {exc}"

    warnings: list[str] = []
    if cache_warning:
        warnings.append(cache_warning)
    try:
        text = fetch_registry_text(config.url, user_agent=user_agent, timeout=timeout)
        entries = _parse_registry(text)
        if cache_enabled:
            try:
                _write_cache(cache, entries, config.url)
            except OSError as exc:
                warnings.append(f"could not write provider cache: {exc}")
        return normalize_providers(
            entries,
            config,
            source=config.url,
            warnings=warnings,
            prefer_https=prefer_https,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"registry fetch failed: {exc}")

    if cached_entries is not None:
        warnings.append("using stale cached registry")
        return normalize_providers(
            cached_entries,
            config,
            source=cached_source or str(cache),
            fetched_at=cached_at.isoformat() if cached_at else None,
            from_cache=True,
            warnings=warnings,
            prefer_https=prefer_https,
        )

    if use_snapshot and SNAPSHOT_PATH.exists():
        entries = _parse_registry(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        warnings.append(
            "using bundled registry snapshot; run 'hosting-scraper providers sync' when online"
        )
        return normalize_providers(
            entries,
            config,
            source=f"bundled snapshot ({SNAPSHOT_PATH.name})",
            warnings=warnings,
            prefer_https=prefer_https,
        )

    raise RegistryError("; ".join(warnings) or "no provider registry available")


def sync_registry(
    config: ProvidersSourceConfig,
    *,
    cache_path: Path,
    user_agent: str,
    timeout: float = 30,
    base_dir: Path | None = None,
    prefer_https: bool = True,
) -> ProviderLoad:
    """Force-refresh the cache and return normalized providers."""
    return load_providers(
        config,
        cache_path=cache_path,
        user_agent=user_agent,
        timeout=timeout,
        force_refresh=True,
        use_snapshot=False,
        base_dir=base_dir,
        prefer_https=prefer_https,
    )
