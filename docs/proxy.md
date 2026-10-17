# Proxies

Proxy support is optional and off by default. When enabled, the engine can use
raw HTTP/SOCKS5 proxies or hosted scraping APIs (“smart proxies”) according to a
per-run policy and per-domain overrides.

Everything lives under `proxy` in `config.yml`:

```yaml
proxy:
  enabled: true
  policy:
    mode: "fallback"
  resources:
    - name: "dc-pool"
      type: "http_pool"
      enabled: true
      endpoints:
        - "http://user:pass@proxy1.example.com:8000"
        - "http://user:pass@proxy2.example.com:8000"
      rotate: "per_request"
  domains: {}
```

## Policies

| Mode | Behavior |
| --- | --- |
| `off` | Never route through a proxy, even if resources are enabled |
| `on` | Every request goes through the selected resource |
| `fallback` | Direct first; if the response is blocked (403/407/429/503, challenge marker) retry via a proxy and possibly Playwright |
| `per_domain` | Only domains with a matching `proxy.domains` rule and `use_proxy: true` use a proxy |

A per-domain rule can also force a mode:

```yaml
proxy:
  enabled: true
  policy:
    mode: "off"
  domains:
    "protected.example.com":
      use_proxy: true
      mode: "on"
      resource: "dc-pool"
```

Resolution order: matching domain rule `mode` → global `policy.mode`. If a rule
names a `resource`, that resource is used; otherwise enabled resources rotate.

## Resources

### `http_pool`

A list of HTTP/HTTPS proxy endpoints, optionally with credentials.

```yaml
- name: "dc-pool"
  type: "http_pool"
  enabled: true
  endpoints:
    - "http://user:pass@proxy1.example.com:8000"
    - "https://proxy2.example.com:443"
  rotate: "per_request"     # or "per_domain"
```

- `per_request` cycles through endpoints.
- `per_domain` assigns one sticky endpoint per domain.
- Credentials are passed through the proxy URL. Labels in logs contain only the
  resource name and proxy host.

### `socks5`

A single SOCKS5 endpoint (one resource per endpoint; add several resources if
you need a pool).

```yaml
- name: "tor"
  type: "socks5"
  endpoint: "socks5://127.0.0.1:9050"
```

SOCKS support in the HTTP fetcher needs `httpx[socks]` (`pip install -e .[socks]`).
Playwright supports SOCKS5 proxies for rendering.

### `smart_proxy`

A hosted scraping API that accepts the target URL as a parameter. The engine
turns a target request into:

```text
GET {endpoint}?{params}&{url_param}={target-url}
```

```yaml
- name: "scrapingbee"
  type: "smart_proxy"
  enabled: true
  endpoint: "https://app.scrapingbee.com/api/v1/"
  api_key_env: "SCRAPINGBEE_API_KEY"
  url_param: "url"
  params:
    render_js: "true"
    api_key: "{api_key}"
```

Fields:

| Key | Notes |
| --- | --- |
| `endpoint` | API base URL |
| `api_key_env` | Environment variable read at request time (recommended) |
| `api_key` | Literal key; discouraged because config files leak easily |
| `url_param` | Query parameter that receives the target URL; default `url` |
| `params` | Extra query parameters; `{url}`, `{api_key}` are interpolated |
| `header_name` / `header_format` | Optional auth header, e.g. `X-API-Key` |

Presets for ScrapingBee, ScraperAPI and ScrapingAnt live in
`hosting_scraper/proxy/providers.py` and can be built programmatically with
`smart_proxy_resource("scraperapi", ...)`. The config schema is generic, so any
GET-based scraping API can be described.

Zyte’s extract API is a POST/JSON API and is **not** compatible with this GET
pass-through. Use Zyte’s HTTP proxy endpoint as an `http_pool` resource instead
(or write a custom adapter).

Smart proxies cannot be used by Playwright: the browser cannot route through a
JSON API. If a page needs rendering and the only proxy is a smart proxy, the
engine logs a warning and retries that page without a proxy.

## How fallback detection works

A response is considered blocked when any of these match:

- status in `policy.blocked_statuses` (default `403, 407, 429, 503`)
- `cf-mitigated: challenge` response header
- a `policy.challenge_markers` string appears in the title/visible text of a
  short page (e.g. “just a moment”, “captcha”, “access denied”)

Markers are only checked on short pages so a legitimate page that mentions
“captcha” in a feature comparison is not treated as a challenge.

With `mode: fallback` the sequence is:

1. direct HTTP request
2. if blocked: retry through a proxy (HTTP)
3. if still blocked and Playwright is enabled: render with a browser, using the
   raw proxy when available
4. otherwise return the blocked result and record it in the run

With `mode: on`, step 1 is skipped. With `mode: off`, only step 1 happens.

`policy.retry_with_playwright: false` disables the browser escalation for blocked
responses.

## Credentials and security

- **Never commit `config.yml`** with inline proxy credentials. The repository
  git-ignores `config.yml`.
- Prefer environment interpolation:

  ```yaml
  endpoints:
    - "${PROXY_URL_1}"
  ```

- Prefer `api_key_env` over `api_key` for smart proxies.
- `run-config.json` and logs redact `user:pass@` in URL-like strings and hide
  `api_key`/`dsn` values. This is a safety net, not a substitute for secret
  management.
- Rotate credentials if a run directory is shared; raw HTML/JSON can contain
  sensitive page content but not proxy credentials.

## Troubleshooting

### `407 Proxy Authentication Required`

Usually a wrong/missing username or password in an `http_pool` endpoint. The
fetcher treats 407 as blocked, which can trigger a retry/Playwright escalation
that also fails. Check the endpoint URL encoding and try it with `curl -x`.

### TLS errors through a proxy

Corporate or MITM proxies may not be trusted by Python. Set the CA bundle used
by httpx:

```bash
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

Only set `fetch.http.verify_tls: false` for controlled debugging; it disables
verification for both HTTP and Playwright fetches.

### SOCKS errors

Install the SOCKS extra (`pip install -e .[socks]`). Ensure the endpoint starts
with `socks5://`; the schema rejects other schemes.

### Smart proxy returns 401/402/429

Those are upstream API errors. The engine records the target fetch as failed and
does not retry with another resource automatically. Check the API key, quota and
parameters (`render_js`, country, etc.).

### Smart proxy returns a JSON error page with HTTP 200

Some APIs do this. Block detection may not fire, and extraction will yield
nothing. Inspect `artifacts/<domain>/*.html` and the run log, and adjust
parameters. Raw proxy pools are more transparent for debugging.

### Playwright ignores the proxy

Only `http_pool` and `socks5` resources can drive Playwright. Smart proxies are
HTTP-only by design. Use a raw pool for rendering, or let the engine fall back to
a direct render (with the warning in the log).
