"""Optional Playwright integration helpers.

The fetch pipeline lives in :mod:`hosting_scraper.crawl.fetch`; this module keeps
the optional dependency and browser launch details in one place so the rest of
the package can degrade gracefully when Playwright or its browsers are absent.
"""

from __future__ import annotations

import importlib.util
from typing import Any

INSTALL_COMMAND_HINT = (
    "Run 'pip install -e .[playwright]' and 'playwright install chromium'."
)
INSTALL_HINT = "Playwright is not installed. " + INSTALL_COMMAND_HINT


def is_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def browser_hint(error: Exception | str) -> str:
    text = str(error)
    if "Executable doesn't exist" in text or "playwright install" in text.lower():
        return f"{text} (run: playwright install chromium)"
    return text


async def start_playwright() -> Any:
    """Start and return the Playwright driver, importing lazily."""
    if not is_available():
        raise RuntimeError(INSTALL_HINT)
    from playwright.async_api import async_playwright

    return await async_playwright().start()
