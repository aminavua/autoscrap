# Troubleshooting

Start with:

```bash
hosting-scraper doctor --config config.yml
hosting-scraper run --config config.yml --max-sites 1 --include-domain example.com --debug
```

Then inspect the run directory:

```text
run.log                 # full debug trace
summary.json            # errors, warnings, per-site status
sites.jsonl             # page-level fetch status and candidate counts
artifacts/<domain>/     # raw.html, page-*.html, extracted.json
providers.json          # whether the provider was loaded/skipped and why
```

## “No providers selected” / a site is missing

- Check `providers.json` → `skipped` (missing URL, duplicate domain, excluded,
  override `include: false`).
- `crawl.include_domains` is an allowlist; an empty list means all.
- CLI `--include-domain` overrides the config allowlist for that run.
- `crawl.max_sites` and `--max-sites` cap the provider list after filtering.
- Registry entries with no URL need a `providers_source.overrides` entry keyed by
  the provider name. See [providers_source.md](providers_source.md).

## Provider cache/network failures

```text
registry fetch failed: ... certificate verify failed
```

- Try `hosting-scraper providers sync -c config.yml`.
- Set `GITHUB_TOKEN` for API rate limits.
- Behind a corporate TLS-inspecting proxy, set
  `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` (or your CA bundle).
- If the network is unavailable, the bundled snapshot is used with a warning.
  `providers sync` intentionally does not use the snapshot so failures are
  visible.
- Delete `.cache/hosting_providers.json` to force a clean fetch.

## Site status “error”: seed fetch failed

`sites.jsonl` → `pages[0].fetch.error` usually contains the reason:

| Error | Action |
| --- | --- |
| `ConnectError` / DNS | Check the registry URL, overrides and network |
| `TimeoutException` | Raise `crawl.timeout_seconds`; try `fetch.mode: playwright` |
| SSL wrong version / certificate | Site may not serve HTTPS; try `prefer_https: false` or fix CA |
| `HTTP 403/429` | Slow down, enable fallback proxy, or use Playwright |
| `unsupported content type` | URL points at a PDF/image/API; fix the seed |

A blocked seed is reported as `status: error` with `blocked: true` in
`sites.jsonl`. The engine already retried according to the proxy/Playwright
policy.

## Site status “partial”: pages fetched, no packages

The site responded but extraction found nothing above the confidence threshold.
Check in this order:

1. Open `artifacts/<domain>/raw.html` and the `page-*.html` files. Are prices in
   the HTML?
2. If the HTML is an app shell (`<div id="root"></div>`, “Loading plans…”), run
   with a browser:

   ```bash
   hosting-scraper run -c config.yml --include-domain example.com \
     --set fetch.mode=playwright --set fetch.playwright.wait_after_load_ms=2000
   ```

3. If prices are present but not extracted, lower the threshold for one run:

   ```bash
   --set extraction.min_confidence_to_accept=0.45
   ```

   Then inspect `extracted.json` and the extractor names. Don’t keep a low
   threshold globally; it increases noise.
4. Check whether the page shows a billing toggle. Set
   `fetch.playwright.toggle_billing.target: monthly` (default) or `yearly`.
5. A consent banner may cover prices; see below.

## JavaScript-rendered pricing

Symptoms: tiny HTML, “enable JavaScript”, `__NEXT_DATA__`/Nuxt hydration,
prices only after XHR. Actions:

- Install the extra and browser:

  ```bash
  pip install -e ".[playwright]"
  playwright install chromium
  ```

- Set `fetch.mode: "playwright"` or leave `auto` (HTTP first, render on shells
  and blocks).
- If `networkidle` times out (long polling, chat widgets), switch to
  `wait_until: "domcontentloaded"` and raise `wait_after_load_ms`.
- A navigation timeout is tolerated: the fetcher keeps the DOM rendered so far
  and logs a warning. If still empty, try a larger wait.
- XHR JSON captured during rendering, plus `<script type="application/json">`
  blobs, are fed to the `xhr_json` extractor. If the API is authenticated behind
  anti-bot JS, this may still fail.

## Blocked pages / captcha / Cloudflare

- Default detection: 403/407/429/503, `cf-mitigated: challenge`, and short pages
  containing challenge markers.
- Configure a proxy resource and use `fallback` (direct first) or `on` (always).
  See [proxy.md](proxy.md).
- `policy.retry_with_playwright: false` disables browser escalation.
- Slow down (`crawl.delay_seconds`, `per_domain_concurrency: 1`) before adding
  more infrastructure. Aggressive retries make blocks worse.
- The engine does not solve captchas or bypass access controls. If a site
  requires authentication or explicitly forbids automation, stop.

## Consent banners

There is no automatic cookie-banner clicker because selector sets differ by
vendor (OneTrust, Cookiebot, TrustArc, …) and clicking the wrong control can
change consent state. Options:

- Run with Playwright and inspect `artifacts/<domain>/*.html`; many banners do
  not remove prices from the DOM even if they cover them visually.
- Add a per-domain override to use `fetch.mode: playwright` and a longer wait.
- For one-off debugging, use Playwright’s codegen/trace to find the banner and
  extend `SiteFetcher` with a site-specific click (keep it opt-in).

## Proxies

See [proxy.md](proxy.md) for 407, SOCKS, TLS and smart-proxy issues. Common
mistakes:

- `proxy.enabled: false` while expecting `policy.mode: on` to work.
- `per_domain` mode without a `proxy.domains` rule.
- A smart proxy used with `fetch.mode: playwright`; the browser cannot use a
  smart proxy (the engine warns and retries without it).
- Missing `api_key_env` variable. The request fails with a clear
  `ProxyError` mentioning the variable name.

## Database sink

```text
Postgres storage requires SQLAlchemy and a driver
```

Install `pip install -e ".[postgres]"`. The DSN must be a SQLAlchemy URL, e.g.
`postgresql+psycopg://user:pass@host:5432/db`. Table names must be plain
identifiers. A failed DB write disables the sink for the rest of the run and
adds a warning; JSONL/artifacts continue.

## Playwright install/browser errors

```text
Executable doesn't exist ... (run: playwright install chromium)
```

Run the install command inside the same virtualenv. On Linux CI, missing system
libraries are common; install the browser with dependencies:

```bash
playwright install --with-deps chromium
```

`hosting-scraper doctor` launches the configured browser headlessly and reports
success/failure.

## Robots.txt

`crawl.respect_robots_txt` defaults to false. If you enable it:

- disallowed seed URLs produce `status: skipped`;
- disallowed candidates are silently skipped (debug log);
- `Crawl-delay` is combined with `crawl.delay_seconds` (the larger wins);
- robots fetch failures are treated as allow (standard practice), while a 4xx
  robots response means no restrictions.

## Too slow / too many requests

- `concurrency` = provider crawls in parallel. `per_domain_concurrency` =
  requests per site. Reduce both before increasing timeouts.
- `discovery.max_candidate_pages_per_site` drives the request budget per site;
  lower it to 3–4 for broad runs.
- `discovery.max_links_to_scan` affects CPU, not requests.
- Playwright is the slow path. Keep `auto`, and only force it for domains that
  need it via `overrides.sites`.

## Interrupted run

Ctrl+C writes a summary with the sites that finished, then exits 130. JSONL files
are flushed per site. Re-run with `--include-domain` for unfinished providers.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Config error, registry error, unreadable output dir, or run failure |
| `2` | CLI usage error |
| `130` | Interrupted |

## Reporting a problem

Include the run summary, `providers.json` skipped list, the failing
`sites.jsonl` record, and the tail of `run.log`. Redact credentials before
sharing; raw HTML may contain account-specific data.
