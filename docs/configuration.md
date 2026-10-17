# Configuration reference

`config.yml` is validated with Pydantic. Unknown keys are errors. Every string
supports `${ENV_VAR}` and `${ENV_VAR:-default}` interpolation. Relative paths are
resolved against the directory containing the config file.

Start with `hosting-scraper init-config` or copy
[`config.example.yml`](../config.example.yml).

```bash
hosting-scraper validate-config --config config.yml
hosting-scraper validate-config --config config.yml --dump   # redacted effective config
```

Any value can be overridden per run without editing the file:

```bash
hosting-scraper run -c config.yml \
  --set fetch.mode=playwright \
  --set crawl.concurrency=2 \
  --set storage.formats.csv=true
```

## `app`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | string | `hosting-scraper` | Label used in metadata |
| `user_agent` | string | `hosting-scraper/1.0 (+https://github.com/aminavua/autoscrap)` | Sent on every request; put a real contact here |
| `timezone` | string | `UTC` | Metadata only; timestamps are UTC |

Use a truthful, contactable user agent. Many sites block generic UAs.

## `providers_source`

The provider registry. `type` is one of `certbot_github`, `url`, `file`.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `type` | enum | `certbot_github` | GitHub-aware fetch of Certbot's JSON |
| `url` | string | Certbot raw JSON URL | Used by `certbot_github` and `url` |
| `path` | string/null | `null` | Required for `type: file` |
| `cache.enabled` | bool | `true` | Cache successful fetches |
| `cache.path` | string | `.cache/hosting_providers.json` | Relative to config |
| `cache.ttl_hours` | number | `24` | `0` forces a fetch each run |
| `urls.include_extra_urls` | bool | `false` | Crawl plan/announcement seeds too |
| `urls.extra_url_fields` | list | `["announcement","plan"]` | Fields used when extras are enabled |
| `urls.same_site_only` | bool | `true` | Ignore extra URLs on other domains |
| `urls.base_url_from` | list | `["link","plan","announcement","tutorial"]` | Domain recovery priority; non-link URLs seed the site root |
| `overrides` | mapping | `{}` | Per-provider fixes, keyed by name or domain |
| `exclude` | list | `[]` | Names/domains to skip |

Provider override fields:

```yaml
providers_source:
  overrides:
    "Cloudways":                       # provider name or domain
      base_url: "https://www.cloudways.com/"
      include: true
      extra_url_fields: ["plan"]      # registry fields to add as seeds
      extra_urls: ["https://www.cloudways.com/en/pricing.php"]  # literal seeds
      category: "full"
```

`include: false` skips a provider. This is the escape hatch for registry entries
with no usable URL (the bundled snapshot has ~106 such entries) and for wrong
domains.

### Fetch/caching behavior

1. Fresh cache (unless `force_refresh`) → 2. GitHub API/raw URL → 3. stale cache
(with warning) → 4. bundled snapshot (with warning).

`hosting-scraper providers sync` forces step 2 and writes the cache. Set
`GITHUB_TOKEN` (or `GH_TOKEN`) to raise GitHub API rate limits. See
[providers_source.md](providers_source.md).

## `crawl`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `max_sites` | int | `0` | `0` = all providers; CLI `--max-sites` wins |
| `concurrency` | int | `8` | Provider sites crawled in parallel |
| `per_domain_concurrency` | int | `2` | Simultaneous page requests per site |
| `delay_seconds` | number | `0.5` | Minimum gap between request starts |
| `timeout_seconds` | number | `30` | Per HTTP request |
| `retries` | int | `2` | Transient/5xx retries |
| `retry_backoff_seconds` | number | `1.0` | Multiplied by attempt number |
| `respect_robots_txt` | bool | `false` | If true, disallowed URLs are skipped; `Crawl-delay` raises the delay |
| `headers` | mapping | `{}` | Extra request headers |
| `include_domains` | list | `[]` | Non-empty = allowlist (suffix match) |
| `exclude_domains` | list | `[]` | Denylist (suffix match) |
| `prefer_https` | bool | `true` | Upgrade `http://` registry seeds when safe (not localhost/IP/custom ports) |
| `max_redirects` | int | `8` | HTTP redirect limit |

`concurrency` is the number of provider crawls; each crawl can issue up to
`per_domain_concurrency` requests at once. Effective max in-flight pages is the
product. Delay is enforced per domain, including concurrent requests.

## `discovery`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `max_candidate_pages_per_site` | int | `8` | Page budget beyond the landing page |
| `common_paths` | list | `/pricing`, `/plans`, … | Seed paths probed early; leading slash optional |
| `keywords.url` | list | pricing, plans, hosting, vps, … | Rank internal links by path |
| `keywords.anchor_text` | list | pricing, plans, compare, … | Rank by anchor text |
| `page_signals.currency_symbols` | list | `$ € £ ₹ ¥ ₦` | Page scoring |
| `page_signals.period_tokens` | list | `/mo`, `per month`, … | Page scoring |
| `max_links_to_scan` | int | `80` | Links extracted per fetched page |

Discovery probes up to half the page budget on common paths, then ranks internal
links (same site, non-pricing paths such as `/blog`, `/login`, `/about` are
excluded) and fills the rest. Registry extra URLs (when enabled) rank highest.

## `fetch`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `mode` | enum | `auto` | `http`, `playwright`, or `auto`; `playwright` requires `playwright.enabled: true` |
| `http.verify_tls` | bool | `true` | Also applied to browser contexts |
| `http.follow_redirects` | bool | `true` | HTTP fetcher |
| `playwright.enabled` | bool | `true` | May be true while mode is `http` |
| `playwright.headless` | bool | `true` | |
| `playwright.browser` | enum | `chromium` | `chromium`, `firefox`, `webkit` |
| `playwright.navigation_timeout_seconds` | number | `45` | Navigation timeout |
| `playwright.wait_until` | enum | `networkidle` | `commit`, `domcontentloaded`, `load`, `networkidle` |
| `playwright.wait_after_load_ms` | int | `750` | Extra wait for price widgets |
| `playwright.block_resource_types` | list | `image, media, font` | Images/fonts stay enabled when screenshots are on |
| `playwright.browser_args` | list | `[]` | Extra launch args |

Billing toggle:

```yaml
fetch:
  playwright:
    toggle_billing:
      enabled: true
      target: "monthly"        # "monthly" | "yearly"
      labels:
        monthly: ["Monthly", "Per month", "Month", "mo"]
        yearly: ["Yearly", "Annually", "Per year", "Annual", "yr"]
```

The toggle is best-effort: it clicks the first visible button/tab/label whose
text exactly matches a label (case-insensitive). It never clicks plain links.

### Fetch modes

- `http`: never launch a browser. Fastest; fails on JS-only pricing.
- `playwright`: render every page. Slow but predictable.
- `auto`: HTTP first; escalate on blocks, challenge markers or JS shells.
  Pages that render but still yield no packages can be re-rendered once (up to
  two escalations per site).

## `extraction`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `enabled_extractors` | list | all four | `schema_org`, `dom_tables`, `dom_cards`, `xhr_json` |
| `min_confidence_to_accept` | 0–1 | `0.55` | Drop lower-confidence records |
| `currency_default` | ISO code | `USD` | Used when a page omits currency |
| `max_packages_per_page` | int | `120` | Safety cap |
| `normalize.infer_billing_term_months` | bool | `true` | Derive monthly/yearly values |
| `normalize.default_billing_period` | enum | `month` | `month`, `year`, `unknown` when no cadence is visible |
| `normalize.monthly_tokens` | list | `/mo`, `per month`, … | Extra cadence tokens |
| `normalize.yearly_tokens` | list | `/yr`, `per year`, … | Extra cadence tokens |
| `normalize.dedupe` | bool | `true` | Merge duplicates across extractors |

Confidence bases: schema.org `0.78`, tables `0.62`, cards `0.55`, XHR `0.52`,
with adjustments for currency, period, features and strong containers. A free
plan (`Free`, `$0`) is retained with `is_free: true`.

## `proxy`

See [proxy.md](proxy.md) for detailed behavior and examples.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch |
| `policy.mode` | enum | `fallback` | `off`, `on`, `fallback`, `per_domain` |
| `policy.blocked_statuses` | list | `403, 407, 429, 503` | Trigger fallback/Playwright |
| `policy.challenge_markers` | list | captcha, `just a moment`, … | Body/title markers, short pages only |
| `policy.retry_with_playwright` | bool | `true` | Allow browser escalation after a block |
| `resources` | list | `[]` | `http_pool`, `socks5`, `smart_proxy` |
| `domains` | mapping | `{}` | Per-domain `use_proxy`, `resource`, `mode` |

Resource shapes:

```yaml
resources:
  - name: "corp-dc-1"
    type: "http_pool"
    endpoints: ["http://user:pass@proxy1.example.com:8000"]
    rotate: "per_request"       # or "per_domain" (sticky endpoint)

  - name: "tor"
    type: "socks5"
    endpoint: "socks5://127.0.0.1:9050"

  - name: "scrapingbee"
    type: "smart_proxy"
    endpoint: "https://app.scrapingbee.com/api/v1/"
    api_key_env: "SCRAPINGBEE_API_KEY"
    url_param: "url"
    params:
      render_js: "true"
      api_key: "{api_key}"
```

Per-domain rules:

```yaml
proxy:
  enabled: true
  policy:
    mode: "per_domain"
  resources: [...]
  domains:
    "example.com":
      use_proxy: true
      resource: "corp-dc-1"
      mode: "on"                # optional per-domain override
```

If `proxy.enabled` is true and the mode is not `off`, at least one enabled
resource is required by validation.

## `storage`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `output_dir` | path | `runs` | Relative to config; `--output-dir` overrides |
| `save_raw_html` | bool | `true` | Save fetched HTML per page |
| `save_screenshots` | bool | `false` | Playwright only |
| `save_extracted_json` | bool | `true` | `artifacts/<domain>/extracted.json` |
| `formats.jsonl` | bool | `true` | `results/offers/sites.jsonl` |
| `formats.csv` | bool | `false` | `packages.csv` |
| `postgres.enabled` | bool | `false` | Requires SQLAlchemy + driver |
| `postgres.dsn` | string | — | e.g. `postgresql+psycopg://user:pass@host/db` |
| `postgres.packages_table` | string | `hosting_packages` | Identifier chars only |
| `postgres.offers_table` | string | `hosting_offers` | Identifier chars only |

Install the SQL extra with `pip install -e ".[postgres]"`. Any SQLAlchemy URL
works; SQLite is useful for tests. Rows are inserted per site. The JSONL files
are the canonical stream; database writes are additive.

## `logging`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `level` | enum | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `json_logs` | bool | `false` | JSON lines on console and in `run.log` |

`--debug` sets `logging.level=DEBUG` for one run. Logs go to stderr and
`runs/<id>/run.log`.

## `overrides.sites`

Per-domain overrides of `crawl`, `discovery`, `fetch` and `extraction`. Keys
match an exact domain, a parent domain, or `*`. Unknown sections are rejected.

```yaml
overrides:
  sites:
    "hostinger.com":                 # exact; also matches www.hostinger.com
      discovery:
        max_candidate_pages_per_site: 12
      fetch:
        mode: "playwright"
    "example.com":                   # parent of shop.example.com
      crawl:
        delay_seconds: 2
    "*":
      crawl:
        retries: 0
```

Matching order: exact domain (with `www.` stripped) → parent domains → `*`.

## Environment interpolation examples

```yaml
app:
  user_agent: "${SCRAPER_UA:-hosting-scraper/1.0 (+contact@example.com)}"

storage:
  postgres:
    enabled: true
    dsn: "${DATABASE_URL}"

proxy:
  enabled: true
  resources:
    - name: "pool"
      type: "http_pool"
      endpoints:
        - "${PROXY_URL_1}"
        - "${PROXY_URL_2:-http://127.0.0.1:8080}"
```

A missing variable without a default is a config error, reported with its path.

## Example recipes

### Playwright always-on

```yaml
fetch:
  mode: "playwright"
  playwright:
    enabled: true
    wait_until: "domcontentloaded"
    wait_after_load_ms: 1500
```

Run `playwright install chromium` first.

### Proxy fallback with a raw pool

```yaml
proxy:
  enabled: true
  policy:
    mode: "fallback"
  resources:
    - name: "dc"
      type: "http_pool"
      endpoints: ["${PROXY_URL_1}", "${PROXY_URL_2}"]
      rotate: "per_request"
```

### Hosted scraping API

```yaml
proxy:
  enabled: true
  policy:
    mode: "on"
  resources:
    - name: "scraperapi"
      type: "smart_proxy"
      endpoint: "http://api.scraperapi.com/"
      api_key_env: "SCRAPERAPI_KEY"
      url_param: "url"
      params:
        api_key: "{api_key}"
        render: "false"
```

Smart proxies run at HTTP level. Pages that still need a real browser will be
retried without the smart proxy (with a warning). Prefer a raw proxy pool when
you need Playwright.

### Only common hosting paths

```yaml
discovery:
  common_paths: ["/pricing", "/shared-hosting", "/web-hosting"]
  keywords:
    url: ["pricing", "shared-hosting", "web-hosting"]
    anchor_text: ["pricing", "shared hosting"]
```

There is no hard package-level category filter; discovery narrows pages, and the
`category` field is inferred from the URL for reporting.

### CSV + PostgreSQL

```yaml
storage:
  formats:
    jsonl: true
    csv: true
  postgres:
    enabled: true
    dsn: "${DATABASE_URL}"
```

Both sinks receive the same deduplicated site results.
