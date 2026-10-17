"""Run directory layout and artifact persistence."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hosting_scraper.config import AppSettings
from hosting_scraper.extract.models import SiteResult, jsonable
from hosting_scraper.util.hashing import short_hash, slugify


def make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


@dataclass
class RunArtifacts:
    settings: AppSettings
    run_id: str
    root: Path
    artifacts_dir: Path
    screenshots_dir: Path
    _html_index: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        settings: AppSettings,
        *,
        run_id: str | None = None,
        output_dir: Path | None = None,
    ) -> RunArtifacts:
        run_id = run_id or make_run_id()
        base = output_dir or settings.output_dir
        root = base / run_id
        suffix = 1
        while root.exists():
            suffix += 1
            root = base / f"{run_id}-{suffix}"
        artifacts_dir = root / "artifacts"
        screenshots_dir = artifacts_dir / "_screenshots"
        root.mkdir(parents=True, exist_ok=False)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        instance = cls(
            settings=settings,
            run_id=root.name,
            root=root,
            artifacts_dir=artifacts_dir,
            screenshots_dir=screenshots_dir,
        )
        instance.write_json("run-config.json", settings.dump(redact=True))
        return instance

    # -- paths ----------------------------------------------------------- #
    def domain_dir(self, domain: str) -> Path:
        path = self.artifacts_dir / slugify(domain, max_length=120)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def screenshots_for(self, domain: str) -> Path:
        path = self.domain_dir(domain) / "screenshots"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def rel(self, path: Path | str) -> str:
        try:
            return str(Path(path).relative_to(self.root))
        except ValueError:
            return str(path)

    # -- writers --------------------------------------------------------- #
    def write_json(self, name: str, data: Any) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(jsonable(data), ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def save_html(self, domain: str, url: str, html: str, *, primary: bool = False) -> str:
        if not html:
            return ""
        directory = self.domain_dir(domain)
        if primary:
            path = directory / "raw.html"
        else:
            path = directory / f"page-{slugify(url, max_length=60)}-{short_hash(url, 8)}.html"
        path.write_text(html, encoding="utf-8", errors="replace")
        self._html_index[url] = self.rel(path)
        return self.rel(path)

    def save_site_extraction(self, site: SiteResult) -> str:
        directory = self.domain_dir(site.domain)
        path = directory / "extracted.json"
        payload = {
            "provider": site.provider_name,
            "domain": site.domain,
            "seed_url": site.seed_url,
            "packages": site.packages,
            "offers": site.offers,
            "pages": [
                {
                    "url": page.url,
                    "status": page.status,
                    "error": page.error,
                    "html_path": page.fetch.html_path if page.fetch else "",
                }
                for page in site.pages
            ],
        }
        path.write_text(
            json.dumps(jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.rel(path)

    def copy_into_run(self, source: Path, name: str) -> str:
        if not source.exists():
            return ""
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return self.rel(target)
