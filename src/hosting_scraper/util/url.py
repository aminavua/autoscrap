from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "msclkid",
    "ref",
    "referrer",
    "affiliate",
    "aff",
}

# Paths that are almost never pricing pages.
NON_PRICING_HINTS = (
    "/blog",
    "/news",
    "/press",
    "/careers",
    "/jobs",
    "/about",
    "/contact",
    "/support",
    "/help",
    "/login",
    "/signin",
    "/sign-in",
    "/register",
    "/signup",
    "/cart",
    "/checkout",
    "/privacy",
    "/terms",
    "/legal",
    "/status",
    "/docs",
    "/documentation",
    "/kb/",
    "/knowledgebase",
    "/community",
    "/forum",
)


def ensure_scheme(value: str, *, prefer_https: bool = True) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return ("https:" if prefer_https else "http:") + value
    if "://" not in value:
        return ("https://" if prefer_https else "http://") + value
    return value


def host_of(url: str) -> str:
    try:
        host = urlsplit(ensure_scheme(url)).hostname or ""
    except ValueError:
        return ""
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def canonicalize(url: str, *, prefer_https: bool = True) -> str:
    """Normalize a URL: lowercase host, drop fragment/tracking params, strip trailing slash."""
    if not url:
        return ""
    url = ensure_scheme(url, prefer_https=prefer_https)
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return ""
    host = (parts.hostname or "").lower().strip(".")
    if not host:
        return ""
    port = parts.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def absolutize(base: str, link: str, *, prefer_https: bool = True) -> str:
    link = (link or "").strip()
    if not link or link.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return ""
    try:
        joined = urljoin(base, link)
    except ValueError:
        return ""
    return canonicalize(joined, prefer_https=prefer_https)


def same_site(url: str, domain: str) -> bool:
    host = host_of(url)
    domain = domain.lower().lstrip(".")
    return bool(host) and (host == domain or host.endswith("." + domain))


def path_of(url: str) -> str:
    try:
        return urlsplit(url).path.lower().rstrip("/") or "/"
    except ValueError:
        return ""


def looks_non_pricing(url: str) -> bool:
    path = path_of(url)
    return any(hint in path for hint in NON_PRICING_HINTS)


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
