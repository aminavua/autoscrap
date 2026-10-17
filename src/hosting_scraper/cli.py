"""Command line interface for hosting-scraper."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from hosting_scraper import __version__
from hosting_scraper.config import (
    AppSettings,
    ConfigError,
    apply_dotted_overrides,
    load_config,
    write_default_config,
)
from hosting_scraper.engine import RunOptions, ScraperEngine
from hosting_scraper.logging_utils import configure_logging
from hosting_scraper.providers.certbot_registry import RegistryError, load_providers, sync_registry
from hosting_scraper.util.hashing import slugify

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hosting-scraper",
        description="Discover hosting pricing pages from Certbot's provider registry and extract plans.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run a scrape")
    _add_config_args(run)
    run.add_argument("--max-sites", type=int, default=None, help="limit provider count (0 = no limit)")
    run.add_argument(
        "--include-domain",
        action="append",
        default=None,
        metavar="DOMAIN",
        help="only crawl this domain (repeatable, suffix match)",
    )
    run.add_argument(
        "--exclude-domain",
        action="append",
        default=None,
        metavar="DOMAIN",
        help="skip this domain (repeatable, suffix match)",
    )
    run.add_argument("--output-dir", type=Path, default=None, help="override storage.output_dir")
    run.add_argument("--run-id", default=None, help="explicit run directory name")
    run.add_argument("--debug", action="store_true", help="enable DEBUG logging")
    run.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override any config key, e.g. --set fetch.mode=playwright",
    )

    validate = subparsers.add_parser("validate-config", help="validate config.yml and exit")
    _add_config_args(validate)
    validate.add_argument("--dump", action="store_true", help="print the redacted effective config")

    init = subparsers.add_parser("init-config", help="write an example config.yml")
    init.add_argument("--path", type=Path, default=Path("config.yml"))
    init.add_argument("--force", action="store_true", help="overwrite an existing file")

    providers = subparsers.add_parser("providers", help="inspect or refresh the provider registry")
    provider_sub = providers.add_subparsers(dest="providers_command", required=True)
    sync = provider_sub.add_parser("sync", help="force-refresh the registry cache")
    _add_config_args(sync)
    sync.add_argument("--debug", action="store_true")
    listing = provider_sub.add_parser("list", help="list normalized providers")
    _add_config_args(listing)
    listing.add_argument("--json", action="store_true", help="emit JSON")
    listing.add_argument("--limit", type=int, default=0)
    listing.add_argument("--refresh", action="store_true", help="ignore a fresh cache")
    listing.add_argument("--debug", action="store_true")

    doctor = subparsers.add_parser("doctor", help="check dependencies and configuration")
    _add_config_args(doctor)
    doctor.add_argument("--skip-browser", action="store_true", help="do not launch Playwright")
    return parser


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", type=Path, default=Path("config.yml"))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load(args: argparse.Namespace) -> AppSettings:
    settings = load_config(args.config)
    overrides = list(getattr(args, "overrides", []) or [])
    if getattr(args, "debug", False):
        overrides.append("logging.level=DEBUG")
    if overrides:
        settings = apply_dotted_overrides(settings, overrides)
    return settings


def _cache_path(settings: AppSettings) -> Path:
    return settings.resolve_path(settings.providers_source.cache.path)


def _load_registry(
    settings: AppSettings,
    *,
    force_refresh: bool = False,
    allow_snapshot: bool = True,
):
    return load_providers(
        settings.providers_source,
        cache_path=_cache_path(settings),
        user_agent=settings.app.user_agent,
        timeout=settings.crawl.timeout_seconds,
        force_refresh=force_refresh,
        use_snapshot=allow_snapshot,
        base_dir=settings.config_dir,
        prefer_https=settings.crawl.prefer_https,
    )


def _run_logging(settings: AppSettings, log_file: Path | None = None) -> None:
    configure_logging(
        level=settings.logging.level,
        json_logs=settings.logging.json_logs,
        log_file=log_file,
    )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_validate(args: argparse.Namespace) -> int:
    try:
        settings = _load(args)
    except ConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {args.config.resolve()}")
    print(f"  user_agent       {settings.app.user_agent}")
    print(f"  provider source  {settings.providers_source.type} -> {settings.providers_source.url}")
    print(f"  output_dir       {settings.output_dir}")
    print(f"  concurrency      {settings.crawl.concurrency} sites / {settings.crawl.per_domain_concurrency} per domain")
    print(f"  fetch mode       {settings.fetch.mode} (playwright: {settings.fetch.playwright.enabled})")
    print(f"  extractors       {', '.join(settings.extraction.enabled_extractors)}")
    proxy_mode = settings.proxy.policy.mode if settings.proxy.enabled else "off"
    print(f"  proxy            {proxy_mode} ({len(settings.proxy.resources)} resource(s))")
    print(f"  site overrides   {len(settings.overrides.get('sites') or {})}")
    if args.dump:
        print(json.dumps(settings.dump(redact=True), indent=2, ensure_ascii=False))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    target = args.path
    if target.exists() and not args.force:
        print(f"refusing to overwrite {target} (use --force)", file=sys.stderr)
        return 1
    if target.exists() and args.force:
        source = Path(__file__).resolve().parent / "data" / "config.example.yml"
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {target}")
        return 0
    try:
        path = write_default_config(target)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"wrote {path}")
    print("Edit it, then run: hosting-scraper run --config config.yml")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        settings = _load(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    if args.max_sites is not None and args.max_sites < 0:
        print("--max-sites must be >= 0", file=sys.stderr)
        return 2
    _run_logging(settings)
    logger.info("hosting-scraper %s starting", __version__)
    try:
        provider_load = _load_registry(settings)
    except (RegistryError, OSError) as exc:
        logger.error("could not load providers: %s", exc)
        return 1
    logger.info(
        "providers: %d loaded from %s (%s), %d skipped",
        len(provider_load.providers),
        provider_load.source,
        "cache" if provider_load.from_cache else "remote",
        len(provider_load.skipped),
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else None
    options = RunOptions(
        max_sites=args.max_sites,
        include_domains=args.include_domain,
        exclude_domains=args.exclude_domain,
        output_dir=output_dir,
        run_id=slugify(args.run_id, max_length=60) if args.run_id else None,
    )
    try:
        engine = ScraperEngine(settings, provider_load, options)
    except Exception as exc:  # noqa: BLE001 - filesystem/permission errors
        logger.error("could not create run directory: %s", exc)
        return 1
    _run_logging(settings, engine.artifacts.root / "run.log")
    logger.info("run directory: %s", engine.artifacts.root)
    try:
        outcome = asyncio.run(engine.run())
    except KeyboardInterrupt:
        print("\ninterrupted; partial results were written if the run had started", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("run failed: %s", exc)
        return 1
    summary = outcome.summary
    totals = summary["totals"]
    print()
    print(f"run:       {outcome.artifacts.run_id}")
    print(f"output:    {outcome.artifacts.root}")
    print(
        f"sites:     {summary['sites']['total']} crawled, "
        f"{summary['sites']['ok']} ok, {summary['sites']['partial']} partial, "
        f"{summary['sites']['error']} error, {summary['sites']['skipped']} skipped"
    )
    print(
        f"packages:  {totals['packages']} ({totals['free_packages']} free), "
        f"offers: {totals['offers']}, pages: {totals['pages_fetched']}"
    )
    print(f"summary:   {outcome.artifacts.root / 'summary.json'}")
    if summary.get("warnings"):
        print(f"warnings:  {len(summary['warnings'])} (see summary.json / run.log)")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    try:
        settings = _load(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    _run_logging(settings)
    try:
        if args.providers_command == "sync":
            load = sync_registry(
                settings.providers_source,
                cache_path=_cache_path(settings),
                user_agent=settings.app.user_agent,
                timeout=settings.crawl.timeout_seconds,
                base_dir=settings.config_dir,
                prefer_https=settings.crawl.prefer_https,
            )
            print(
                f"synced {len(load.providers)} provider(s) from {load.source} "
                f"({len(load.skipped)} skipped)"
            )
            if settings.providers_source.cache.enabled and settings.providers_source.type != "file":
                print(f"cache: {_cache_path(settings)}")
            return 0
        load = _load_registry(settings, force_refresh=args.refresh)
        providers = load.providers
        if args.limit:
            providers = providers[: args.limit]
        if args.json:
            print(
                json.dumps(
                    {
                        "source": load.source,
                        "from_cache": load.from_cache,
                        "provider_count": len(load.providers),
                        "skipped_count": len(load.skipped),
                        "providers": [provider.to_dict() for provider in providers],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        print(f"source: {load.source} ({'cache' if load.from_cache else 'remote'})")
        print(f"{len(load.providers)} provider(s), {len(load.skipped)} skipped")
        for provider in providers:
            print(f"{provider.name:<32} {provider.domain:<32} {provider.category:<8} {provider.seed_url}")
        return 0
    except (RegistryError, OSError) as exc:
        print(f"provider registry error: {exc}", file=sys.stderr)
        return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        settings = _load(args)
    except ConfigError as exc:
        print(f"config: INVALID ({exc})", file=sys.stderr)
        return 1
    print("config: OK")
    print(f"python: {sys.version.split()[0]}")
    print(f"package: {__version__}")
    from hosting_scraper.crawl.playwright_fetch import is_available

    print(f"playwright installed: {is_available()}")
    print(f"provider cache: {_cache_path(settings)} ({'present' if _cache_path(settings).exists() else 'missing'})")
    print(f"output dir: {settings.output_dir} ({'writable' if _writable(settings.output_dir) else 'NOT writable'})")
    if settings.proxy.enabled:
        print(
            f"proxy: {settings.proxy.policy.mode}, "
            f"{sum(1 for r in settings.proxy.resources if r.enabled)} enabled resource(s)"
        )
    else:
        print("proxy: disabled")
    if not args.skip_browser and is_available():
        ok = asyncio.run(_check_browser(settings))
        print(f"chromium launch: {'OK' if ok else 'FAILED (run: playwright install chromium)'}")
    if not _writable(settings.output_dir):
        return 1
    return 0


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


async def _check_browser(settings: AppSettings) -> bool:
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser_type = getattr(playwright, settings.fetch.playwright.browser)
            browser = await browser_type.launch(headless=True)
            await browser.close()
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "validate-config":
        return cmd_validate(args)
    if args.command == "init-config":
        return cmd_init(args)
    if args.command == "providers":
        return cmd_providers(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
