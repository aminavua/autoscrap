from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hosting_scraper.config import AppSettings

FIXTURES = Path(__file__).parent / "fixtures" / "site"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture(scope="session")
def site_server():
    handler = partial(QuietHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def local_provider_file(tmp_path: Path, site_server: str) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "Local Hosting",
                    "link": site_server + "/",
                    "category": "full",
                    "tutorial": "",
                    "announcement": "",
                    "plan": "",
                    "reviewed": "2026.01.01",
                    "note": "fixture provider",
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def local_settings(tmp_path: Path, local_provider_file: Path) -> AppSettings:
    return AppSettings(
        app={"user_agent": "hosting-scraper-tests/1.0 (+test@example.com)"},
        providers_source={"type": "file", "path": str(local_provider_file)},
        crawl={
            "concurrency": 2,
            "per_domain_concurrency": 2,
            "delay_seconds": 0,
            "timeout_seconds": 5,
            "retries": 0,
        },
        discovery={
            "max_candidate_pages_per_site": 5,
            "common_paths": ["/pricing", "/plans", "/plan"],
        },
        fetch={"mode": "http", "playwright": {"enabled": False}},
        storage={"output_dir": str(tmp_path / "runs"), "save_raw_html": True},
    )
