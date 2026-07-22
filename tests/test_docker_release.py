from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_isolates_postgres_and_runs_production_reproducer() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    postgres = compose["services"]["postgres"]
    pipeline = compose["services"]["pipeline"]

    assert "ports" not in postgres
    assert postgres["healthcheck"]["test"][0] == "CMD-SHELL"
    assert pipeline["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert pipeline["command"][:2] == ["python", "scripts/reproduce.py"]
    assert "--source-mode" in pipeline["command"]
    assert "--source-bundle-index" in pipeline["command"]
    assert "--enable-journals" not in pipeline["command"]
    assert "GOVINFO_API_KEY" in pipeline["environment"]
    assert "./bundles:/workspace/bundles:ro" in pipeline["volumes"]
    assert "source-manifests:/workspace/data/manifests" in pipeline["volumes"]
    assert "source-manifests" in compose["volumes"]


def test_docker_context_excludes_secrets_raw_corpora_and_outputs() -> None:
    patterns = set(Path(".dockerignore").read_text(encoding="utf-8").splitlines())

    assert {
        ".env",
        ".venv",
        "output",
        "release-output",
        "bundles",
        "*.dump",
        "data/crec",
        "data/journals",
        "data/primary",
        "data/resolutions",
        "data/congressional_directories",
    } <= patterns


def test_release_policy_includes_docker_entrypoints() -> None:
    policy = yaml.safe_load(Path("config/release-files.json").read_text(encoding="utf-8"))
    destinations = {entry["destination"] for entry in policy["files"]}

    assert {"Dockerfile", "docker-compose.yml", ".dockerignore", "docs/DOCKER.md"} <= destinations


def test_docker_installs_the_frozen_uv_lock() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv export --frozen --no-emit-project" in dockerfile
    assert "--require-hashes" in dockerfile
