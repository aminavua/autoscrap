# Architecture and data model

## Pipeline

```text
Certbot registry
      │
      ▼
providers/certbot_registry.py     fetch → cache → normalize → Provider
      │
      ▼
engine.SiteCrawler                seed fetch, discovery, page budget
      │
      ├── crawl/discover.py       common paths, link ranking, page scoring
      ├── crawl/fetch.py          HTTP + proxy policy + Playwright fallback
      ├── crawl/robots.py         optional robots.txt cache + crawl delay
      └── proxy/                  route selection and request shaping
      ▼
extract/__init__.py               run extractors, merge, confidence filter
      │
      ├── schema_org.py           JSON-LD Product/Service/Offer
      ├── dom_tables.py           normal and transposed pricing tables
      ├── dom_cards.py            price cards / pricing grids
      ├── xhr_capture.py          captured XHR JSON + embedded state blobs
      └── offers.py               promotions, coupons, free quotas, guarantees
      ▼
storage/                          JSONL/CSV, PostgreSQL, HTML/extracted artifacts
      │
      ▼
reporting.py                      summary.json, warnings/errors, counts
```

## Source layout

```text
src/hosting_scraper/
├── cli.py                     argparse commands and logging bootstrap
├── config.py                  Pydantic schema, env expansion, overrides
├── engine.py                  async orchestration, site crawler, run lifecycle
├── logging_utils.py           console/JSON/file logging
├── reporting.py               summary builder
├── providers/certbot_registry.py
├── crawl/
│   ├── discover.py            link ranking, page scoring
│   ├── fetch.py               HTTP/Playwright fetcher, block detection
│   ├── playwright_fetch.py    optional dependency bootstrap
│   ├── rate_limit.py          per-domain spacing
│   └── robots.py
├── extract/
│   ├── models.py              Package, DirectOffer, FetchResult, PageResult, SiteResult
│   ├── normalize.py           price/period/feature normalization, dedupe keys
│   ├── confidence.py          extractor confidence bases and boosts
│   ├── schema_org.py          JSON-LD
│   ├── dom_tables.py
│   ├── dom_cards.py
│   ├── dom_utils.py           strike-through/price/heading helpers
│   ├── xhr_capture.py
│   ├── offers.py
│   └── __init__.py            extract_page(), merge_packages()
├── proxy/
│   ├── base.py                ProxyRoute, ProxyPlanner, smart-proxy request plans
│   ├── manager.py             public policy interface + logging
│   └── providers.py           smart-proxy presets
├── storage/
│   ├── artifacts.py           run directory, HTML, extracted JSON
│   ├── sink_jsonl.py          results/offers/sites JSONL + CSV
│   └── sink_postgres.py       optional SQLAlchemy sink
├── util/                      url, text/price parsing, hashing/slug
└── data/hosting_providers.json + config.example.yml
```

## Site crawl

1. `Provider` → domain → `SiteSettings` (global config merged with
   `overrides.sites`).
2. Fetch the seed (HTTP or Playwright by mode/policy). Failed seeds become a
   site-level error with the fetch diagnostics.
3. Probe the first half of the page budget with `discovery.common_paths`
   concurrently, limited by `per_domain_concurrency` and the per-domain delay.
4. Extract same-site links from the seed and successful probes. URL keywords,
   anchor text, stronger pricing paths and penalties for blog/login/about/too-deep
   URLs rank candidates.
5. Fill the remaining page budget from links + leftover common paths + extra
   registry URLs.
6. Select pages for extraction using the pricing-page score (title/headings,
   currency+amount density, period tokens, JSON-LD offers), plus URL heuristics.
7. Extract; if a static page yields nothing and looks like a JS shell, re-render
   once with Playwright (max two escalations per site).
8. Deduplicate packages across pages and offers, then write results.

The page budget (`max_candidate_pages_per_site`) counts candidate fetches, not
the landing page. Failed probes still consume the budget so a dead site cannot
cause unbounded requests.

## Concurrency and politeness

- `crawl.concurrency`: number of provider crawls at once (outer semaphore).
- `crawl.per_domain_concurrency`: in-flight page requests per provider crawl.
- Per-domain scheduling spaces request starts by `delay_seconds` (or a larger
  robots `Crawl-delay`). The limiter reserves start slots, so bursts from
  concurrent tasks are flattened.
- HTTP retries are limited and back off; blocked responses are not blindly
  retried with the same strategy.
- Playwright browsers are cached per proxy route for a site and closed when the
  site finishes.

## Package normalization

- `parse_price` scores candidate numbers: currency symbol/code > period proximity
  > decimal shape, with penalties for unit contexts (`8 GB`, `4 vCPU`, `%`).
  This prevents feature specs from becoming prices.
- `parse_billing_period` recognizes `/mo`, `per month`, `/yr`, `billed every 12
  months`, “annually”, and more. Multiple cadences: the price text wins over the
  surrounding context; the explicit default is `normalize.default_billing_period`.
- Currency from code or symbol; `extraction.currency_default` when absent.
- Monthly/yearly values are derived with `ROUND_HALF_UP`.
- `is_free` when the price text is “Free” (and has no numeric price) or the
  parsed amount is zero.
- Storage/RAM/bandwidth hints are parsed from features and context.
- Duplicate keys use domain + normalized name + currency + amount + period;
  a fuzzy key additionally strips trailing “plan/package/hosting”. When merging,
  the higher-confidence record wins and richer features/description are kept.

## Extractors and confidence

| Extractor | Base | Signals |
| --- | --- | --- |
| `schema_org` | 0.78–0.90 | name + price + currency + billing duration |
| `dom_tables` | 0.62–0.72 | header price column, features, transposed orientation |
| `dom_cards` | 0.55–0.80 | local heading, price node, feature list, card class |
| `xhr_json` | 0.52–0.72 | name + price + currency/period/features in JSON |

The default acceptance threshold is `0.55`; records below it are dropped and the
page extraction result records a warning. Confidence is a heuristic, not a
probability.

## Data model (`results.jsonl`)

A package record contains:

- provenance: `run_id`, `provider`, `domain`, `page_url`, `captured_at`
- identity: `name`, `category`, `description`
- price: `price_raw`, `price_amount`, `currency`, `billing_period`,
  `billing_months`, `monthly_price`, `yearly_price`, `is_free`
- specs/features: `features[]`, `storage`, `disk_type`, `ram`, `bandwidth`
- quality: `extractor`, `confidence`, `evidence`

Offers (`offers.jsonl`): `title`, `description`, `url`, `kind`
(`promotion`, `coupon`, `free_quota`, `money_back`), `discount_text`,
`coupon_code`, `expires`, `confidence`, `extractor`, `evidence`.

Sites (`sites.jsonl`): provider/domain/seed, status (`ok`, `partial`, `error`,
`skipped`), proxy label, counts, and page records with fetch diagnostics,
package/offer counts, candidate counts and extraction warnings.

`summary.json`: run timestamps/duration, redacted config snapshot, provider
source/counts/skips, site and package totals, extractor/proxy/status breakdowns,
warnings, errors, output paths and per-site results.

## Storage and artifacts

- JSONL writes happen as each site finishes, so an interrupted run preserves
  completed work. CSV mirrors packages when enabled.
- `artifacts/<domain>/raw.html` is the landing page; other pages use
  `page-<slug>-<hash>.html`. Re-rendered HTML overwrites the static snapshot.
- `extracted.json` contains the site's deduplicated packages/offers and page
  manifest.
- `run-config.json` is the effective config with secrets redacted.
- PostgreSQL uses `metadata.create_all`; inserts are batched per site. A sink
  failure disables SQL for the rest of the run and adds a warning. JSONL remains.

## Extending

- **New extractor**: add a module returning `Package` objects via
  `normalize.build_package`, register it in `extract_page`, and add the name to
  the `ExtractionConfig.enabled_extractors` validator.
- **New proxy type**: extend the `ProxyResource` union, `ProxyPlanner.select_route`
  and either `ProxyRoute.request_plan` or the Playwright proxy mapping.
- **New sink**: consume `SiteResult` and write per site; add CLI/config wiring.
- **Site-specific hacks**: prefer `overrides.sites` (paths, fetch mode, waits)
  over code. If a layout needs code, keep it opt-in or behind a generic heuristic.

## Failure isolation

Provider crawls are independent. A failed site becomes a `SiteResult` with
`status: error` and is still written to `sites.jsonl`; other providers continue.
Sink failures are logged and do not stop crawling. Playwright is optional and
errors are reported per page. The run writes a summary even when interrupted.
