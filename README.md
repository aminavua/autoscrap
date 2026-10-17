# hosting-scraper

A config-driven scraping engine for hosting prices. It uses
[Certbot's community `hosting_providers.json`](https://github.com/certbot/website/blob/master/_data/hosting_providers.json)
as its provider registry, discovers each provider's pricing/plan pages, and
extracts normalized plans, prices, free tiers and promotions.

- **Registry-driven** — no hard-coded provider list. Fetch once, cache, or use a
  bundled offline snapshot.
- **Config-first** — discovery keywords, paths, fetch mode, throttles, proxies,
  extractors, storage and per-domain overrides all live in `config.yml`.
- **HTTP + Playwright** — fast static fetch first; render JS-heavy or challenged
  pages only when needed. Monthly/yearly toggles can be clicked automatically.
- **Proxy policies** — `off`, `on`, `fallback`, `per_domain`; HTTP pools, SOCKS5
  and hosted scraping APIs.
- **Four extractors** — schema.org JSON-LD, HTML tables, price cards, and XHR /
  embedded JSON. Results are merged and confidence-scored.
- **Auditable output** — JSONL/CSV, raw HTML snapshots, per-site JSON, optional
  PostgreSQL, structured logs and a run summary.

The extraction is deliberately best-effort. Hosting pages change often, use
custom markup, and are sometimes protected; see [Limitations](#limitations).

## Requirements

- Python 3.10+
- Optional: Playwright + Chromium for JS-rendered pages
- Optional: SQLAlchemy + a PostgreSQL driver for the SQL sink

## Install

```bash
python -m venv .venv
source .venv/bin/activate

pip install -e ".[playwright,postgres]"
playwright install chromium          # only needed if you use Playwright
```

Minimal install (static HTML only):

```bash
pip install -e .
```

## Quickstart

```bash
# 1. Write an editable config next to the project
hosting-scraper init-config --path config.yml

# 2. Check the schema before doing any work
hosting-scraper validate-config --config config.yml

# 3. Refresh the provider registry (optional; a bundled snapshot works offline)
hosting-scraper providers sync --config config.yml

# 4. Start with a small run
hosting-scraper run --config config.yml --max-sites 10
```

Example output:

```text
run:       20261017-104500
output:    /path/to/autoscrap/runs/20261017-104500
sites:     10 crawled, 8 ok, 2 partial, 0 error, 0 skipped
packages:  63 (4 free), offers: 17, pages: 71
summary:   /path/to/autoscrap/runs/20261017-104500/summary.json
```

Useful variants:

```bash
# Re-run a single provider while debugging
hosting-scraper run -c config.yml --include-domain hostinger.com --debug

# Always use a browser for this run
hosting-scraper run -c config.yml --set fetch.mode=playwright --max-sites 5

# Override any config key
hosting-scraper run -c config.yml --set crawl.concurrency=2 --set storage.formats.csv=true

# List the normalized provider registry
hosting-scraper providers list -c config.yml --limit 20 --json
```

## Run outputs

```
runs/<run-id>/
├── summary.json          # totals, statuses, warnings, paths, redacted config
├── sites.jsonl           # one record per provider with page/fetch details
├── results.jsonl         # one normalized package per line
├── offers.jsonl          # promotions, coupons, free quotas, guarantees
├── packages.csv          # optional (storage.formats.csv)
├── providers.json        # normalized registry used for this run (+ skipped)
├── run-config.json       # effective config, secrets redacted
├── run.log               # console + file log
└── artifacts/
    └── <domain>/
        ├── raw.html      # fetched landing page
        ├── page-*.html   # pricing/plan pages
        ├── extracted.json
        └── screenshots/  # optional
```

`results.jsonl` records include `provider`, `domain`, `page_url`, plan name,
raw and parsed price, currency, billing period, monthly/yearly price, free flag,
features, storage/RAM/bandwidth hints, extractor and confidence.

## CLI

| Command | Purpose |
| --- | --- |
| `hosting-scraper run` | Crawl providers and write a run directory |
| `hosting-scraper validate-config` | Validate YAML; `--dump` prints the redacted config |
| `hosting-scraper init-config` | Copy the documented example config |
| `hosting-scraper providers sync` | Force-refresh the registry cache |
| `hosting-scraper providers list` | List normalized providers (`--json`, `--refresh`) |
| `hosting-scraper doctor` | Check config, dependencies, browser and output dir |

`run` options: `--max-sites`, `--include-domain` / `--exclude-domain`
(repeatable, suffix match), `--output-dir`, `--run-id`, `--debug`, `--set KEY=VALUE`.

Exit codes: `0` success, `1` config/run error, `2` usage error, `130` interrupted.

## Registry seeds and proxy modes

**1. Homepage or every URL in a Certbot entry?**

Both are supported, controlled by config:

```yaml
providers_source:
  urls:
    include_extra_urls: false          # default: only the provider homepage
    extra_url_fields: ["announcement", "plan"]
    same_site_only: true
    base_url_from: ["link", "plan", "announcement", "tutorial"]
```

With `include_extra_urls: true`, plan/announcement URLs on the provider's own
site become high-priority crawl seeds. URLs on unrelated sites are ignored (and
reported as warnings). Entries without a `link` recover a domain from the other
fields and seed the site root, since a help article is a poor pricing landing
page; entries with no usable URL at all are skipped and can be fixed with
`providers_source.overrides` keyed by provider name.

**2. Raw proxies or hosted “smart proxy” APIs?**

Both. `http_pool` and `socks5` resources take raw endpoints; `smart_proxy`
resources wrap a hosted scraping API by templating the target URL, API key and
extra parameters into a GET request. Built-in presets cover ScrapingBee,
ScraperAPI and ScrapingAnt (see `hosting_scraper.proxy.providers`). See
[docs/proxy.md](docs/proxy.md).

## Configuration

Start from [`config.example.yml`](config.example.yml). Top-level sections:

```yaml
app:              # user agent, name, timezone
providers_source: # Certbot registry URL/file, cache, URL selection, overrides
crawl:            # concurrency, delays, retries, timeouts, robots, domain filters
discovery:        # common paths, URL/anchor keywords, pricing signals
fetch:            # http/playwright/auto, browser behavior, billing toggle
extraction:       # extractors, confidence threshold, currency, normalization
proxy:            # policy, resources, per-domain rules
storage:          # output dir, artifacts, JSONL/CSV, PostgreSQL
logging:          # level, JSON logs
overrides.sites:  # per-domain overrides of crawl/discovery/fetch/extraction
```

Strings support `${ENV_VAR}` interpolation with `${ENV_VAR:-default}`. Paths are
resolved relative to the config file. Unknown keys are rejected so typos surface
early. Full reference: [docs/configuration.md](docs/configuration.md).

A small proxy fallback example:

```yaml
proxy:
  enabled: true
  policy:
    mode: "fallback"          # direct first, retry via proxy on 403/429/captcha
  resources:
    - name: "datacenter"
      type: "http_pool"
      endpoints: ["http://user:pass@proxy.example.com:8000"]
      rotate: "per_request"
```

Prefer environment variables for secrets; do **not** commit `config.yml`
(it is git-ignored). `run-config.json` and logs redact credentials.

## How it works

1. Normalize registry entries to a provider homepage (and optional extra URLs).
2. Fetch the landing page; rank common pricing paths and internal links.
3. Probe a small number of likely pages, score each page for price signals.
4. Fetch the best candidates, escalating to Playwright for JS shells/blocks.
5. Run schema.org, table, card and XHR/embedded-JSON extractors in parallel.
6. Normalize prices/currencies/periods, merge duplicates, apply confidence.
7. Write incremental JSONL, HTML artifacts, a per-site JSON and a run summary.

Extractor confidence starts higher for structured data (schema.org) and lower
for generic card/XHR heuristics. The default acceptance threshold is `0.55`.
Details: [docs/architecture.md](docs/architecture.md).

## Politeness and legal

- `crawl.respect_robots_txt` defaults to **false** so the example works out of
  the box. Set it to `true` for strict compliance; disallowed URLs are skipped.
- There are per-domain concurrency limits and a configurable delay. Increase
  `crawl.delay_seconds` when a site is fragile.
- You are responsible for the target site's terms of service and applicable
  law. Do not use proxies or automation to bypass access controls you are not
  authorized to bypass. The default blocked-status/challenge handling is a
  best-effort retry mechanism, not a permission.
- Only fetch public pricing pages; do not crawl authenticated areas.

## Limitations

- No universal layout support. Pages with canvas prices, unusual shadow DOM, or
  prices loaded only after interactions may yield nothing.
- Consent banners are not auto-dismissed; a banner can hide pricing content.
- Playwright is optional; without a browser, JS-only pricing pages fail.
- Hosted smart proxies are HTTP-level. Playwright cannot run through a smart
  proxy resource; the engine warns and retries that page without it.
- Free/cheap intro prices are ambiguous by nature. The raw price, billing
  cadence and evidence snippet are preserved so you can audit them.
- Currency defaults to `USD` when a page omits it. Set `currency_default`.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

The test suite uses a local HTTP server and fixtures; it never scrapes the
public internet. It covers config validation, registry caching/overrides,
discovery, proxies, all extractors, sinks and an end-to-end run.

## Documentation

- [Configuration reference](docs/configuration.md)
- [Proxy resources and policies](docs/proxy.md)
- [Provider registry source](docs/providers_source.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture and data model](docs/architecture.md)

## License

MIT. See [LICENSE](LICENSE).
