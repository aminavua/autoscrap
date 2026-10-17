# Provider registry

The provider list comes from Certbot's community data file:

- <https://github.com/certbot/website/blob/master/_data/hosting_providers.json>
- Raw: <https://raw.githubusercontent.com/certbot/website/master/_data/hosting_providers.json>

Each entry is a JSON object:

```json
{
  "name": "Snel.com",
  "link": "https://www.snel.com",
  "category": "full",
  "tutorial": "",
  "announcement": "",
  "plan": "https://www.snel.com/webhosting/",
  "reviewed": "2022.11.17",
  "note": "Free & automated SSL for all-in web hosting plans"
}
```

A snapshot is bundled at
`src/hosting_scraper/data/hosting_providers.json` so the tool works offline.
The snapshot fetched on 2026-10-17 contains 296 entries; the default URL policy
resolves 190 providers and reports 106 skipped (mostly entries with no URL at
all). Numbers change as the upstream file evolves.

## Loading

`providers_source.type`:

| Type | Behavior |
| --- | --- |
| `certbot_github` | GitHub-aware: try the API first, then raw, with cache/snapshot fallback |
| `url` | Plain HTTP(S) GET of a JSON URL |
| `file` | Read a local registry JSON file (`path`) |

Resolution order for network sources:

1. cache younger than `cache.ttl_hours` (unless force refresh)
2. remote registry
3. stale cache, with a warning
4. bundled snapshot, with a warning

`hosting-scraper providers sync` forces step 2 and writes the cache. If every
network path fails and no cache exists, the command fails instead of silently
using the snapshot.

### GitHub API and rate limits

For GitHub URLs the loader first calls:

```text
https://api.github.com/repos/<owner>/<repo>/contents/<path>?ref=<branch>
```

and falls back to raw.githubusercontent.com. Unauthenticated GitHub API requests
are limited (60/hour per IP). Export `GITHUB_TOKEN` or `GH_TOKEN` to authenticate
and raise the limit:

```bash
export GITHUB_TOKEN=ghp_...
hosting-scraper providers sync -c config.yml
```

TLS failures on one GitHub host are common behind corporate proxies; the loader
tries the other endpoint. If Python cannot verify the certificate (but curl
works), point it at the system bundle:

```bash
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

## Choosing URLs from an entry

Default behavior: **one seed per provider, the homepage.**

1. If `providers_source.overrides` provides `base_url`, use it exactly.
2. If the entry has a `link`, use it exactly (this is the normal case).
3. Otherwise use the first usable URL in `urls.base_url_from`
   (default `link`, `plan`, `announcement`, `tutorial`) to recover the domain,
   and seed the site root. A tutorial or KB article is not a good pricing
   landing page.
4. If no field has a URL, use the provider name when it looks like a domain
   (`example.com`, no spaces).
5. Otherwise skip the provider with a reason.

All registry URLs (`link`, `announcement`, `plan`, `tutorial`) are kept in
`providers.json` for debugging and for optional extra seeds.

### Extra registry URLs

Some entries have an empty `link` or keep pricing on a separate `plan` page. To
crawl them:

```yaml
providers_source:
  urls:
    include_extra_urls: true
    extra_url_fields: ["announcement", "plan"]
    same_site_only: true
```

These URLs become high-priority candidates in the same per-site page budget.
`same_site_only` drops URLs that point at third-party sites (a tutorial hosted on
a vendor blog, for example) and records a warning. Set it to `false` only for
registries you trust.

With `include_extra_urls: false` the URLs are still used to recover a domain
when `link` is empty (`base_url_from`), but only that first recovered URL is
seeded.

### HTTPS preference

`crawl.prefer_https: true` upgrades plain `http://` seeds to HTTPS when it is
safe: domain hosts on port 80 or no explicit port. It leaves `localhost`, IP
addresses and custom ports unchanged, because those are usually test or internal
endpoints. Set it to `false` if a provider only serves HTTP.

## Fixing missing or wrong providers

`providers_source.overrides` is keyed by provider name or domain (case
insensitive for names):

```yaml
providers_source:
  overrides:
    "Cloudways":                       # entry has no URLs in the registry
      base_url: "https://www.cloudways.com/"
    "example.com":
      base_url: "https://www.example.com/pricing"
      extra_url_fields: ["plan"]
      extra_urls: ["https://www.example.com/hidden-pricing"]
    "spam-provider":
      include: false
  exclude: ["another-domain.com"]
```

- `base_url` replaces the seed and the derived domain.
- `include: false` skips the provider.
- `extra_url_fields` opts this provider into extra registry seeds even if the
  global `include_extra_urls` is false, and selects which fields to use.
- `extra_urls` adds literal seed URLs (same-site unless
  `urls.same_site_only: false`).
- `category` overrides the registry category in reports.

`providers.json` in each run contains the normalized providers **and** the
skipped list with reasons. Check it first when a provider is missing.

## Cache

```yaml
providers_source:
  cache:
    enabled: true
    path: ".cache/hosting_providers.json"
    ttl_hours: 24
```

The cache file is a wrapper:

```json
{
  "fetched_at": "2026-10-17T10:29:00+00:00",
  "source": "https://raw.githubusercontent.com/...",
  "providers": [ ...raw registry entries... ]
}
```

It is JSON, human-readable, and safe to delete. `.cache/` is git-ignored. TTL is
measured from `fetched_at`; `0` means always stale. `providers sync` bypasses
TTL.

## Local registry files

Useful for tests, curated lists and air-gapped runs:

```yaml
providers_source:
  type: file
  path: "./my_providers.json"
```

The file can contain the same array as upstream. A minimal list:

```json
[
  {
    "name": "Local Fixture",
    "link": "http://127.0.0.1:8080/",
    "category": "full",
    "tutorial": "",
    "announcement": "",
    "plan": "",
    "reviewed": "",
    "note": ""
  }
]
```

Relative `path` values resolve against the config file directory when using the
CLI. When calling `load_providers()` as a library, pass `base_dir=`.

## Updating the bundled snapshot

```bash
curl -fsSL \
  https://raw.githubusercontent.com/certbot/website/master/_data/hosting_providers.json \
  -o src/hosting_scraper/data/hosting_providers.json
```

Keep the upstream array shape. `providers sync` does not overwrite the bundled
snapshot; it only writes the cache.

## Categories

The registry uses `full`, `partial` and `no` to describe Let's Encrypt support.
This scraper does not filter on category. It is preserved on the provider in
`providers.json` and can be used for external analysis. `category` can be
overridden per provider in config.
