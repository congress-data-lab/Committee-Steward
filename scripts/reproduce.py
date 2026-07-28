#!/usr/bin/env python3
"""
Deterministic production reproduction orchestrator.

This lane plans and runs the existing ingest/export CLIs in one ordered flow,
recording a machine-readable JSON ledger that supports resumable execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONGRESS_FROM = 113
DEFAULT_CONGRESS_TO = 118
DEFAULT_OUTPUT_DIR = ROOT / "output" / "reproduction"
DEFAULT_SOURCE_CLASSIFICATION = ROOT / "config" / "source-classification.json"
DEFAULT_SOURCE_BUNDLE_INDEX = ROOT / "manifests" / "source-bundles.json"
DEFAULT_VALIDATION_POLICY = ROOT / "config" / "release-validation-policy.json"
LEDGER_FILENAME = "run_ledger.json"
LEDGER_VERSION = 1
TAIL_LINE_LIMIT = 20
SOURCE_MODE_CHOICES = ("local", "frozen-bundle", "govinfo")
DEFAULT_SOURCE_MODE = "frozen-bundle"


@dataclass(frozen=True)
class ReproduceConfig:
    congress_from: int
    congress_to: int
    database_url: str | None
    source_mode: str
    refresh_govinfo: bool
    source_bundle_index: Path
    source_classification: Path
    validation_policy: Path
    build_source_bundles: bool
    output_dir: Path
    resume: bool
    dry_run: bool
    enable_journals: bool
    python_executable: str


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    label: str
    command: tuple[str, ...]
    input_paths: tuple[Path, ...]
    category: str
    congress_no: int | None = None
    optional: bool = False
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


Runner = Callable[[list[str], dict[str, str], Path], CommandResult]


def parse_args(argv: list[str] | None = None) -> ReproduceConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--congress-from", type=int, default=DEFAULT_CONGRESS_FROM)
    parser.add_argument("--congress-to", type=int, default=DEFAULT_CONGRESS_TO)
    parser.add_argument("--database-url", default=None, help="Override NEON_DATABASE_URL for all child stages.")
    parser.add_argument(
        "--source-mode",
        choices=SOURCE_MODE_CHOICES,
        default=DEFAULT_SOURCE_MODE,
        help="Choose local bundle construction, frozen-bundle hydration, or GovInfo retrieval.",
    )
    parser.add_argument(
        "--refresh-govinfo",
        action="store_true",
        help=(
            "Explicitly rediscover live GovInfo inputs. Without this flag, an existing "
            "GovInfo manifest is treated as the pinned source snapshot."
        ),
    )
    parser.add_argument(
        "--source-bundle-index",
        type=Path,
        default=DEFAULT_SOURCE_BUNDLE_INDEX,
        help="Provider-neutral source-bundles.json used by frozen-bundle mode.",
    )
    parser.add_argument(
        "--source-classification",
        type=Path,
        default=DEFAULT_SOURCE_CLASSIFICATION,
        help="Required/optional/validation-only source classification JSON.",
    )
    parser.add_argument(
        "--validation-policy",
        type=Path,
        default=DEFAULT_VALIDATION_POLICY,
        help="Versioned Directory and integrity release-gate policy.",
    )
    parser.add_argument(
        "--build-source-bundles",
        action="store_true",
        help=(
            "Explicitly build archival ZIP bundles. This can perform sustained "
            "I/O and is disabled in the default bounded reproduction path."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for ledger and validation/export outputs (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from the last compatible completed stage.")
    parser.add_argument("--dry-run", action="store_true", help="Write the planned ledger without executing commands.")
    journal_group = parser.add_mutually_exclusive_group()
    journal_group.add_argument(
        "--enable-journals",
        dest="enable_journals",
        action="store_true",
        default=False,
        help="Enable validation-only House journal ingestion stages.",
    )
    journal_group.add_argument(
        "--disable-journals",
        dest="enable_journals",
        action="store_false",
        help="Disable validation-only House journal ingestion (default).",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used for child Python commands (default: current interpreter).",
    )
    args = parser.parse_args(argv)

    if args.congress_from > args.congress_to:
        parser.error("--congress-from must be less than or equal to --congress-to")

    return ReproduceConfig(
        congress_from=args.congress_from,
        congress_to=args.congress_to,
        database_url=args.database_url,
        source_mode=args.source_mode,
        refresh_govinfo=args.refresh_govinfo,
        source_bundle_index=args.source_bundle_index.resolve(),
        source_classification=args.source_classification.resolve(),
        validation_policy=args.validation_policy.resolve(),
        build_source_bundles=args.build_source_bundles,
        output_dir=args.output_dir.resolve(),
        resume=args.resume,
        dry_run=args.dry_run,
        enable_journals=args.enable_journals,
        python_executable=args.python,
    )


def _plan_source_stages(config: ReproduceConfig) -> list[StageSpec]:
    py = config.python_executable
    stages: list[StageSpec] = []
    generated_index = config.output_dir / "source-bundles.json"
    for congress_no in _congresses(config):
        manifest = _manifest_path(congress_no)
        if config.source_mode == "frozen-bundle":
            script = ROOT / "scripts" / "hydrate_sources.py"
            stages.append(
                StageSpec(
                    stage_id=f"sources.{congress_no}.hydrate",
                    label=f"Congress {congress_no}: Hydrate frozen source bundle",
                    command=(
                        py,
                        str(script),
                        "--index",
                        str(config.source_bundle_index),
                        "--congress",
                        str(congress_no),
                        "--root",
                        str(ROOT),
                    ),
                    input_paths=(script, config.source_bundle_index),
                    category="sources",
                    congress_no=congress_no,
                )
            )
            continue

        if config.source_mode == "govinfo":
            script = ROOT / "scripts" / "build_govinfo_committee_manifest.py"
            manifest_args = (
                ("--refresh-existing",)
                if manifest.exists() and not config.refresh_govinfo
                else ()
            )
            acquisition_args = (
                manifest_args
                if manifest_args
                else (
                    "--download-missing-candidate-class",
                    "committee_assignment",
                    "--max-downloads",
                    "25",
                )
            )
            stages.append(
                StageSpec(
                    stage_id=f"sources.{congress_no}.govinfo_manifest",
                    label=f"Congress {congress_no}: Retrieve GovInfo manifest inputs",
                    command=(
                        py,
                        str(script),
                        str(congress_no),
                        "--output",
                        str(manifest),
                        *acquisition_args,
                    ),
                    input_paths=(script, manifest) if manifest_args else (script,),
                    category="sources",
                    congress_no=congress_no,
                )
            )

        verify_script = ROOT / "scripts" / "verify_source_manifest.py"
        stages.append(
            StageSpec(
                stage_id=f"sources.{congress_no}.verify_required",
                label=f"Congress {congress_no}: Verify required source files",
                command=(
                    py,
                    str(verify_script),
                    str(manifest),
                    "--root",
                    str(ROOT),
                    "--classification-config",
                    str(config.source_classification),
                ),
                input_paths=(verify_script, manifest, config.source_classification),
                category="sources",
                congress_no=congress_no,
            )
        )
        if config.build_source_bundles:
            bundle_script = ROOT / "scripts" / "build_source_bundle.py"
            stages.append(
                StageSpec(
                    stage_id=f"sources.{congress_no}.build_bundle",
                    label=f"Congress {congress_no}: Build archival source bundle",
                    command=(
                        py,
                        str(bundle_script),
                        str(manifest),
                        "--archive",
                        str(
                            config.output_dir
                            / "bundles"
                            / f"source-bundle-{congress_no}.zip"
                        ),
                        "--index",
                        str(generated_index),
                        "--root",
                        str(ROOT),
                        "--classification-config",
                        str(config.source_classification),
                    ),
                    input_paths=(
                        bundle_script,
                        manifest,
                        config.source_classification,
                    ),
                    category="sources",
                    congress_no=congress_no,
                    optional=True,
                )
            )
    return stages


def _manifest_path(congress_no: int) -> Path:
    """Prefer the production layout while supporting the current workspace."""
    production_path = ROOT / "manifests" / f"{congress_no}.csv"
    if production_path.exists():
        return production_path
    return ROOT / "data" / "manifests" / f"manifest_{congress_no}.csv"


def plan_stages(config: ReproduceConfig) -> list[StageSpec]:
    py = config.python_executable
    congresses = _congresses(config)
    validation_dir = config.output_dir / "validation"
    release_dir = config.output_dir / "release"

    stages: list[StageSpec] = _plan_source_stages(config)
    stages.extend([
        StageSpec(
            stage_id="schema.apply",
            label="Create or verify schema",
            command=(
                py,
                str(ROOT / "scripts" / "ensure_schema.py"),
                "--schema",
                str(ROOT / "db" / "schema.sql"),
            ),
            input_paths=(
                ROOT / "scripts" / "ensure_schema.py",
                ROOT / "db" / "schema.sql",
            ),
            category="schema",
            metadata={"source_mode": config.source_mode},
        ),
        _python_module_stage(
            py,
            "reference.load_committees",
            "Load committees",
            "ingest.load_committees",
            "reference",
            extra_input_paths=(
                ROOT / "data" / "reference" / "committees-current.yaml",
                ROOT / "data" / "reference" / "committees-historical.yaml",
            ),
        ),
        _python_module_stage(
            py,
            "reference.load_members",
            "Load members",
            "ingest.load_members",
            "reference",
            extra_input_paths=(
                ROOT / "data" / "reference" / "legislators-current.yaml",
                ROOT / "data" / "reference" / "legislators-historical.yaml",
            ),
        ),
    ])

    for congress_no in congresses:
        stages.extend(
            [
                _python_module_stage(
                    py,
                    f"events.{congress_no}.house_resolutions",
                    f"Congress {congress_no}: House resolutions",
                    "ingest.load_resolution_events",
                    "events",
                    congress_no=congress_no,
                    extra_args=("-c", str(congress_no)),
                ),
                _python_module_stage(
                    py,
                    f"events.{congress_no}.house_crec",
                    f"Congress {congress_no}: House CREC",
                    "ingest.load_crec_events",
                    "events",
                    congress_no=congress_no,
                    extra_args=("-c", str(congress_no), "--chamber", "house"),
                ),
                _python_module_stage(
                    py,
                    f"events.{congress_no}.house_journal",
                    f"Congress {congress_no}: House journal",
                    "ingest.load_journal_events",
                    "events",
                    congress_no=congress_no,
                    optional=True,
                    enabled=config.enable_journals,
                    extra_args=("-c", str(congress_no), "--chamber", "H", "--allow-write"),
                ),
                _python_module_stage(
                    py,
                    f"events.{congress_no}.senate_resolutions",
                    f"Congress {congress_no}: Senate resolutions",
                    "ingest.load_senate_resolution_events",
                    "events",
                    congress_no=congress_no,
                    extra_args=("-c", str(congress_no)),
                ),
                _python_module_stage(
                    py,
                    f"events.{congress_no}.senate_crec",
                    f"Congress {congress_no}: Senate CREC",
                    "ingest.load_crec_events",
                    "events",
                    congress_no=congress_no,
                    extra_args=("-c", str(congress_no), "--chamber", "senate"),
                ),
            ]
        )

    for congress_no in congresses:
        stages.append(
            _python_module_stage(
                py,
                f"events.{congress_no}.member_service_exit",
                f"Congress {congress_no}: Member service exits",
                "ingest.load_member_service_exit_events",
                "derivation",
                congress_no=congress_no,
                extra_args=("--congress", str(congress_no), "--allow-write"),
            )
        )

    stages.append(
        _python_module_stage(
            py,
            "membership.build",
            "Build committee memberships",
            "ingest.build_membership",
            "derivation",
            extra_args=("--congress", *(str(congress_no) for congress_no in congresses)),
        )
    )

    stages.append(
        _python_module_stage(
            py,
            "membership.build_ranks",
            "Build committee party ranks",
            "ingest.build_membership_ranks",
            "derivation",
            extra_args=("--congress", *(str(congress_no) for congress_no in congresses)),
        )
    )

    # Directory references are loaded only after extraction and interval
    # construction, so they cannot influence parser output.
    stages.append(
        _python_module_stage(
            py,
            "validation_reference.load_directories",
            "Load directory validation snapshots",
            "ingest.load_directory_snapshots",
            "validation_reference",
        )
    )

    for congress_no in congresses:
        stages.append(
            _python_script_stage(
                py,
                f"validation.{congress_no}.membership_integrity",
                f"Congress {congress_no}: Membership integrity validation",
                ROOT / "scripts" / "validate_membership_integrity.py",
                "validation",
                congress_no=congress_no,
                extra_args=(
                    "--congress",
                    str(congress_no),
                    "--output",
                    str(validation_dir / f"membership_integrity_{congress_no}.csv"),
                    "--fail-on-issues",
                ),
            )
        )

    stages.append(
        StageSpec(
            stage_id="validation.release_gates",
            label="Enforce Directory release gates",
            command=(
                py,
                str(ROOT / "scripts" / "validate_release.py"),
                "--congress-from",
                str(config.congress_from),
                "--congress-to",
                str(config.congress_to),
                "--output-dir",
                str(validation_dir),
                "--policy",
                str(config.validation_policy),
                "--fail-on-gate",
            ),
            input_paths=(
                ROOT / "scripts" / "validate_release.py",
                config.validation_policy,
            ),
            category="validation",
        )
    )

    stages.append(
        _python_script_stage(
            py,
            "release.export",
            "Export release artifacts",
            ROOT / "scripts" / "export_release.py",
            "export",
            extra_args=(
                "--congress-from",
                str(config.congress_from),
                "--congress-to",
                str(config.congress_to),
                "--output-dir",
                str(release_dir),
            ),
            metadata={"designed_for": "scripts/export_release.py"},
        )
    )
    return stages


def run_reproduction(config: ReproduceConfig, runner: Runner | None = None) -> int:
    runner = runner or _default_runner
    config.output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = config.output_dir / LEDGER_FILENAME
    ledger = _initialize_ledger(config, plan_stages(config), ledger_path)

    print(
        f"Planned {len(ledger['stages'])} stages for Congresses "
        f"{config.congress_from}-{config.congress_to}; ledger={ledger_path}"
    )

    if config.dry_run:
        ledger["run_status"] = "dry_run"
        now = _iso_now()
        for stage in ledger["stages"]:
            if stage["status"] == "disabled":
                continue
            stage["status"] = "dry_run"
            stage["started_at"] = now
            stage["ended_at"] = now
            stage["duration_seconds"] = 0.0
        ledger["started_at"] = ledger.get("started_at") or now
        ledger["ended_at"] = now
        ledger["updated_at"] = now
        _write_json_atomic(ledger_path, ledger)
        print("Dry run complete.")
        return 0

    ledger["run_status"] = "running"
    ledger["started_at"] = ledger.get("started_at") or _iso_now()
    ledger["updated_at"] = _iso_now()
    _write_json_atomic(ledger_path, ledger)

    base_env = _build_child_env(config)
    for index, stage_record in enumerate(ledger["stages"], start=1):
        if stage_record["status"] in {"completed", "disabled"}:
            print(f"[{index}/{len(ledger['stages'])}] {stage_record['label']} [{stage_record['status']}]")
            continue

        stage = _stage_from_record(stage_record)
        print(f"[{index}/{len(ledger['stages'])}] {stage.label}")
        _refresh_stage_file_hashes(stage_record)
        started = time.monotonic()
        stage_record["status"] = "running"
        stage_record["started_at"] = _iso_now()
        stage_record["error"] = None
        ledger["updated_at"] = _iso_now()
        _write_json_atomic(ledger_path, ledger)

        try:
            result = runner(list(stage.command), _stage_env(base_env, stage), ROOT)
        except Exception as exc:
            elapsed = time.monotonic() - started
            _record_failure(stage_record, error=str(exc), elapsed_seconds=elapsed)
            ledger["run_status"] = "failed"
            ledger["ended_at"] = _iso_now()
            ledger["updated_at"] = _iso_now()
            _write_json_atomic(ledger_path, ledger)
            print(f"  FAILED: {exc}")
            return 1

        elapsed = result.elapsed_seconds
        stage_record["ended_at"] = _iso_now()
        stage_record["duration_seconds"] = round(elapsed, 3)
        stage_record["exit_code"] = result.returncode
        stage_record["stdout_tail"] = _tail_lines(result.stdout)
        stage_record["stderr_tail"] = _tail_lines(result.stderr)
        stage_record["count_summary"] = _summarize_counts(result.stdout, result.stderr)

        if result.returncode != 0:
            stage_record["status"] = "failed"
            stage_record["error"] = _format_stage_error(result)
            ledger["run_status"] = "failed"
            ledger["ended_at"] = _iso_now()
            ledger["updated_at"] = _iso_now()
            _write_json_atomic(ledger_path, ledger)
            print(f"  FAILED with exit={result.returncode}")
            return result.returncode

        stage_record["status"] = "completed"
        ledger["updated_at"] = _iso_now()
        _write_json_atomic(ledger_path, ledger)
        print(f"  completed in {elapsed:.2f}s")

    ledger["run_status"] = "completed"
    ledger["ended_at"] = _iso_now()
    ledger["updated_at"] = _iso_now()
    _write_json_atomic(ledger_path, ledger)
    print("Reproduction complete.")
    return 0


def _python_module_stage(
    python_executable: str,
    stage_id: str,
    label: str,
    module_name: str,
    category: str,
    *,
    congress_no: int | None = None,
    optional: bool = False,
    enabled: bool = True,
    extra_args: tuple[str, ...] = (),
    extra_input_paths: tuple[Path, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> StageSpec:
    return StageSpec(
        stage_id=stage_id,
        label=label,
        command=(python_executable, "-m", module_name, *extra_args),
        input_paths=(
            ROOT / Path(*module_name.split(".")).with_suffix(".py"),
            *extra_input_paths,
        ),
        category=category,
        congress_no=congress_no,
        optional=optional,
        enabled=enabled,
        metadata=metadata or {},
    )


def _python_script_stage(
    python_executable: str,
    stage_id: str,
    label: str,
    script_path: Path,
    category: str,
    *,
    congress_no: int | None = None,
    optional: bool = False,
    enabled: bool = True,
    extra_args: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> StageSpec:
    return StageSpec(
        stage_id=stage_id,
        label=label,
        command=(python_executable, str(script_path), *extra_args),
        input_paths=(script_path,),
        category=category,
        congress_no=congress_no,
        optional=optional,
        enabled=enabled,
        metadata=metadata or {},
    )


def _initialize_ledger(config: ReproduceConfig, stages: list[StageSpec], ledger_path: Path) -> dict[str, Any]:
    ledger = _new_ledger(config, stages)
    if not config.resume or not ledger_path.exists():
        return ledger

    previous = json.loads(ledger_path.read_text(encoding="utf-8"))
    if previous.get("resume_fingerprint") != ledger.get("resume_fingerprint"):
        return ledger
    previous_by_id = {stage["id"]: stage for stage in previous.get("stages", [])}
    reusable_prefix = True
    for stage in ledger["stages"]:
        prior = previous_by_id.get(stage["id"])
        if reusable_prefix and prior and _is_reusable_completed_stage(stage, prior):
            for key in (
                "status",
                "started_at",
                "ended_at",
                "duration_seconds",
                "exit_code",
                "stdout_tail",
                "stderr_tail",
                "count_summary",
                "error",
            ):
                stage[key] = prior.get(key)
            continue
        reusable_prefix = False
    return ledger


def _new_ledger(config: ReproduceConfig, stages: list[StageSpec]) -> dict[str, Any]:
    now = _iso_now()
    return {
        "version": LEDGER_VERSION,
        "repo_root": str(ROOT),
        "run_status": "planned",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "ended_at": None,
        "resume_fingerprint": _resume_fingerprint(config),
        "config": {
            "congress_from": config.congress_from,
            "congress_to": config.congress_to,
            "source_mode": config.source_mode,
            "source_bundle_index": str(config.source_bundle_index),
            "source_classification": str(config.source_classification),
            "validation_policy": str(config.validation_policy),
            "build_source_bundles": config.build_source_bundles,
            "output_dir": str(config.output_dir),
            "resume": config.resume,
            "dry_run": config.dry_run,
            "enable_journals": config.enable_journals,
            "python": config.python_executable,
            "database_url_supplied": _resolve_database_url(config) is not None,
            "database_target_sha256": _database_target_sha256(config),
        },
        "stages": [_stage_record(stage) for stage in stages],
    }


def _stage_record(stage: StageSpec) -> dict[str, Any]:
    input_hashes = {
        "stage_definition": _sha256_json(
            {
                "stage_id": stage.stage_id,
                "command": list(stage.command),
                "category": stage.category,
                "congress_no": stage.congress_no,
                "optional": stage.optional,
                "enabled": stage.enabled,
                "metadata": stage.metadata,
            }
        ),
        "files": {
            _display_path(path): _sha256_file(path) for path in stage.input_paths
        },
    }
    return {
        "id": stage.stage_id,
        "label": stage.label,
        "category": stage.category,
        "congress_no": stage.congress_no,
        "optional": stage.optional,
        "enabled": stage.enabled,
        "metadata": stage.metadata,
        "command": list(stage.command),
        "status": "disabled" if not stage.enabled else "pending",
        "started_at": None,
        "ended_at": None,
        "duration_seconds": None,
        "exit_code": None,
        "count_summary": {},
        "stdout_tail": [],
        "stderr_tail": [],
        "error": None,
        "input_hashes": input_hashes,
    }


def _refresh_stage_file_hashes(stage_record: dict[str, Any]) -> None:
    files = stage_record["input_hashes"]["files"]
    for path_text in list(files):
        path = Path(path_text)
        if not path.is_absolute():
            path = ROOT / path
        files[path_text] = _sha256_file(path)


def _stage_from_record(stage_record: dict[str, Any]) -> StageSpec:
    input_paths = tuple(
        (ROOT / rel_path).resolve() if not Path(rel_path).is_absolute() else Path(rel_path)
        for rel_path in stage_record["input_hashes"]["files"]
    )
    return StageSpec(
        stage_id=stage_record["id"],
        label=stage_record["label"],
        command=tuple(stage_record["command"]),
        input_paths=input_paths,
        category=stage_record["category"],
        congress_no=stage_record.get("congress_no"),
        optional=stage_record.get("optional", False),
        enabled=stage_record.get("enabled", True),
        metadata=stage_record.get("metadata", {}),
    )


def _is_reusable_completed_stage(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    return (
        previous.get("status") == current.get("status", "pending") == "completed"
        or (
            previous.get("status") == "completed"
            and current.get("status") == "pending"
            and previous.get("input_hashes") == current.get("input_hashes")
        )
    )


def _build_child_env(config: ReproduceConfig) -> dict[str, str]:
    env = os.environ.copy()
    database_url = _resolve_database_url(config)
    if database_url:
        env["NEON_DATABASE_URL"] = database_url
    env["REPRO_SOURCE_MODE"] = config.source_mode
    env["REPRO_OUTPUT_DIR"] = str(config.output_dir)
    return env


def _stage_env(base_env: dict[str, str], stage: StageSpec) -> dict[str, str]:
    return dict(base_env)


def _resolve_database_url(config: ReproduceConfig) -> str | None:
    if config.database_url:
        return config.database_url
    if os.environ.get("NEON_DATABASE_URL"):
        return os.environ["NEON_DATABASE_URL"]
    env_path = ROOT / ".env"
    if not env_path.exists():
        return None
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == "NEON_DATABASE_URL":
            return value.strip()
    return None


def _database_target_sha256(config: ReproduceConfig) -> str | None:
    database_url = _resolve_database_url(config)
    if database_url is None:
        return None
    return hashlib.sha256(database_url.encode("utf-8")).hexdigest()


def _resume_fingerprint(config: ReproduceConfig) -> str:
    return _sha256_json(
        {
            "congress_from": config.congress_from,
            "congress_to": config.congress_to,
            "build_source_bundles": config.build_source_bundles,
            "database_target_sha256": _database_target_sha256(config),
            "enable_journals": config.enable_journals,
            "python": config.python_executable,
            "source_mode": config.source_mode,
            "source_bundle_index_sha256": _sha256_file(config.source_bundle_index),
            "source_classification_sha256": _sha256_file(
                config.source_classification
            ),
            "validation_policy_sha256": _sha256_file(config.validation_policy),
            "code_sha256": _code_fingerprint(),
        }
    )


def _code_fingerprint() -> str:
    digest = hashlib.sha256()
    roots = (ROOT / "core", ROOT / "db", ROOT / "ingest", ROOT / "scripts", ROOT / "validate")
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix in {".py", ".sql"}
            )
    for path in sorted(files):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        file_hash = _sha256_file(path)
        digest.update((file_hash or "").encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _default_runner(command: list[str], env: dict[str, str], cwd: Path) -> CommandResult:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - started
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed_seconds=elapsed,
    )


def _record_failure(stage_record: dict[str, Any], *, error: str, elapsed_seconds: float) -> None:
    stage_record["status"] = "failed"
    stage_record["ended_at"] = _iso_now()
    stage_record["duration_seconds"] = round(elapsed_seconds, 3)
    stage_record["exit_code"] = None
    stage_record["error"] = error
    stage_record["stdout_tail"] = []
    stage_record["stderr_tail"] = []
    stage_record["count_summary"] = {}


def _format_stage_error(result: CommandResult) -> str:
    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    if stderr:
        return stderr.splitlines()[-1]
    if stdout:
        return stdout.splitlines()[-1]
    return f"Command failed with exit code {result.returncode}"


def _summarize_counts(stdout: str, stderr: str) -> dict[str, int]:
    summary: dict[str, int] = {}
    for raw_line in (stdout + "\n" + stderr).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        colon_match = re.match(r"^([A-Za-z][A-Za-z0-9 ()/_-]*?):\s*(-?\d+)\s*$", line)
        if colon_match:
            label = _slugify_label(colon_match.group(1))
            summary[label] = int(colon_match.group(2))

        for key, value in re.findall(r"([a-z_]+)=(-?\d+)", line):
            summary[key] = int(value)

        wrote_match = re.search(r"\bWrote\s+(-?\d+)\s+flagged rows\b", line)
        if wrote_match:
            summary["flagged_rows"] = int(wrote_match.group(1))
    return summary


def _tail_lines(text: str, limit: int = TAIL_LINE_LIMIT) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def _congresses(config: ReproduceConfig) -> list[int]:
    return list(range(config.congress_from, config.congress_to + 1))


def _sha256_json(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    temp_path.replace(path)
    path.chmod(0o644)


def _slugify_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    return run_reproduction(config)


if __name__ == "__main__":
    raise SystemExit(main())
