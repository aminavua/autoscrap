from __future__ import annotations

import json
from pathlib import Path

import yaml

from hosting_scraper.cli import main

REPO = Path(__file__).resolve().parents[1]


def _write_config(path: Path, provider_file: Path, tmp_path: Path, site_url: str = "") -> Path:
    data = {
        "providers_source": {"type": "file", "path": str(provider_file)},
        "crawl": {
            "concurrency": 2,
            "per_domain_concurrency": 2,
            "delay_seconds": 0,
            "timeout_seconds": 5,
            "retries": 0,
        },
        "discovery": {
            "max_candidate_pages_per_site": 4,
            "common_paths": ["/pricing", "/plans"],
        },
        "fetch": {"mode": "http", "playwright": {"enabled": False}},
        "storage": {"output_dir": str(tmp_path / "runs")},
        "logging": {"level": "WARNING"},
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_validate_example(capsys) -> None:
    code = main(["validate-config", "--config", str(REPO / "config.example.yml")])
    captured = capsys.readouterr()
    assert code == 0
    assert "OK:" in captured.out
    assert "extractors" in captured.out


def test_validate_invalid(tmp_path: Path, capsys) -> None:
    config = tmp_path / "bad.yml"
    config.write_text("crawl:\n  nope: 1\n", encoding="utf-8")
    code = main(["validate-config", "--config", str(config)])
    captured = capsys.readouterr()
    assert code == 1
    assert "INVALID" in captured.err


def test_init_config(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config.yml"
    assert main(["init-config", "--path", str(target)]) == 0
    assert target.exists()
    assert "providers_source" in target.read_text(encoding="utf-8")
    assert main(["init-config", "--path", str(target)]) == 1
    assert "refusing" in capsys.readouterr().err


def test_providers_list_local(tmp_path: Path, local_provider_file: Path, capsys) -> None:
    config = _write_config(tmp_path / "config.yml", local_provider_file, tmp_path)
    code = main(["providers", "list", "--config", str(config), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["provider_count"] == 1
    assert payload["providers"][0]["domain"] == "127.0.0.1"


def test_providers_sync_local(tmp_path: Path, local_provider_file: Path, capsys) -> None:
    config = _write_config(tmp_path / "config.yml", local_provider_file, tmp_path)
    code = main(["providers", "sync", "--config", str(config)])
    captured = capsys.readouterr()
    assert code == 0
    assert "synced 1 provider" in captured.out


def test_run_cli_end_to_end(
    tmp_path: Path,
    local_provider_file: Path,
    site_server: str,
    capsys,
) -> None:
    config = _write_config(tmp_path / "config.yml", local_provider_file, tmp_path, site_server)
    code = main(["run", "--config", str(config), "--max-sites", "1"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "packages:" in captured.out
    assert "summary:" in captured.out
    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    summary = json.loads((run_dirs[0] / "summary.json").read_text(encoding="utf-8"))
    assert summary["sites"]["total"] == 1
    assert summary["totals"]["packages"] >= 5
    assert (run_dirs[0] / "run.log").exists()
