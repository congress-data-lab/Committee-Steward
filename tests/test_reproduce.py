from __future__ import annotations

import json
from pathlib import Path

from scripts.reproduce import (
    CommandResult,
    ReproduceConfig,
    _build_child_env,
    _manifest_path,
    parse_args,
    plan_stages,
    run_reproduction,
)


def test_cli_keeps_validation_only_house_journal_stages_opt_in() -> None:
    assert parse_args([]).enable_journals is False
    assert parse_args(["--enable-journals"]).enable_journals is True
    assert parse_args(["--disable-journals"]).enable_journals is False


def _config(tmp_path: Path, **overrides: object) -> ReproduceConfig:
    values = {
        "congress_from": 113,
        "congress_to": 114,
        "database_url": None,
        "source_mode": "local",
        "refresh_govinfo": False,
        "source_bundle_index": tmp_path / "source-bundles.json",
        "source_classification": tmp_path / "source-classification.json",
        "validation_policy": tmp_path / "release-validation-policy.json",
        "build_source_bundles": False,
        "output_dir": tmp_path,
        "resume": False,
        "dry_run": False,
        "enable_journals": False,
        "python_executable": "python-test",
    }
    values.update(overrides)
    return ReproduceConfig(**values)


def test_plan_stages_orders_required_sections_and_disables_journals_by_default(tmp_path: Path):
    config = _config(tmp_path)

    stages = plan_stages(config)
    stage_ids = [stage.stage_id for stage in stages]

    assert stage_ids[:5] == [
        "sources.113.verify_required",
        "sources.114.verify_required",
        "schema.apply",
        "reference.load_committees",
        "reference.load_members",
    ]
    committee_stage = next(
        stage for stage in stages if stage.stage_id == "reference.load_committees"
    )
    member_stage = next(
        stage for stage in stages if stage.stage_id == "reference.load_members"
    )
    assert {path.name for path in committee_stage.input_paths} == {
        "load_committees.py",
        "committees-current.yaml",
        "committees-historical.yaml",
    }
    assert {path.name for path in member_stage.input_paths} == {
        "load_members.py",
        "legislators-current.yaml",
        "legislators-historical.yaml",
    }
    assert stage_ids[5:10] == [
        "events.113.house_resolutions",
        "events.113.house_crec",
        "events.113.house_journal",
        "events.113.senate_resolutions",
        "events.113.senate_crec",
    ]
    assert stage_ids[-5:] == [
        "validation_reference.load_directories",
        "validation.113.membership_integrity",
        "validation.114.membership_integrity",
        "validation.release_gates",
        "release.export",
    ]

    build_index = stage_ids.index("membership.build")
    rank_build_index = stage_ids.index("membership.build_ranks")
    assert build_index < rank_build_index < stage_ids.index(
        "validation_reference.load_directories"
    )
    assert "validation_reference.load_stewart" not in stage_ids

    journal_stage = next(stage for stage in stages if stage.stage_id == "events.113.house_journal")
    assert journal_stage.optional is True
    assert journal_stage.enabled is False

    exit_114 = stage_ids.index("events.114.member_service_exit")
    validation_index = stage_ids.index("validation.113.membership_integrity")
    release_gate_index = stage_ids.index("validation.release_gates")
    export_index = stage_ids.index("release.export")
    assert (
        exit_114
        < build_index
        < rank_build_index
        < validation_index
        < release_gate_index
        < export_index
    )

    integrity_stage = next(
        stage for stage in stages if stage.stage_id == "validation.113.membership_integrity"
    )
    assert "--fail-on-issues" in integrity_stage.command
    assert "--fail-on-gate" in stages[release_gate_index].command


def test_build_child_env_passes_database_url_and_source_mode(tmp_path: Path):
    config = _config(
        tmp_path,
        database_url="postgresql://example.invalid/db",
        source_mode="frozen-bundle",
    )

    env = _build_child_env(config)

    assert env["NEON_DATABASE_URL"] == "postgresql://example.invalid/db"
    assert env["REPRO_SOURCE_MODE"] == "frozen-bundle"
    assert env["REPRO_OUTPUT_DIR"] == str(tmp_path)


def test_frozen_bundle_mode_hydrates_before_schema(tmp_path: Path):
    config = _config(tmp_path, source_mode="frozen-bundle")

    stages = plan_stages(config)

    assert stages[0].stage_id == "sources.113.hydrate"
    assert stages[1].stage_id == "sources.114.hydrate"
    assert stages[2].stage_id == "schema.apply"
    assert "--index" in stages[0].command


def test_govinfo_mode_reuses_existing_manifest_as_pinned_snapshot(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.reproduce.ROOT", tmp_path)
    manifest = tmp_path / "manifests/113.csv"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("snapshot\n", encoding="utf-8")
    config = _config(tmp_path, source_mode="govinfo")

    stage = plan_stages(config)[0]

    assert stage.stage_id == "sources.113.govinfo_manifest"
    assert "--refresh-existing" in stage.command
    assert manifest in stage.input_paths


def test_govinfo_refresh_flag_allows_live_rediscovery(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.reproduce.ROOT", tmp_path)
    manifest = tmp_path / "manifests/113.csv"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("snapshot\n", encoding="utf-8")
    config = _config(tmp_path, source_mode="govinfo", refresh_govinfo=True)

    stage = plan_stages(config)[0]

    assert "--refresh-existing" not in stage.command
    assert manifest not in stage.input_paths


def test_manifest_path_prefers_production_layout_and_falls_back(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.reproduce.ROOT", tmp_path)
    legacy = tmp_path / "data/manifests/manifest_113.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy\n", encoding="utf-8")

    assert _manifest_path(113) == legacy

    production = tmp_path / "manifests/113.csv"
    production.parent.mkdir(parents=True)
    production.write_text("production\n", encoding="utf-8")

    assert _manifest_path(113) == production


def test_run_reproduction_dry_run_writes_ledger_without_invoking_runner(tmp_path: Path):
    config = _config(tmp_path, dry_run=True)
    calls: list[list[str]] = []

    def runner(command: list[str], _env: dict[str, str], _cwd: Path) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "", 0.0)

    exit_code = run_reproduction(config, runner=runner)

    ledger = json.loads((tmp_path / "run_ledger.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert calls == []
    assert ledger["run_status"] == "dry_run"
    statuses = {stage["id"]: stage["status"] for stage in ledger["stages"]}
    assert statuses["schema.apply"] == "dry_run"
    assert statuses["sources.113.verify_required"] == "dry_run"
    assert statuses["events.113.house_journal"] == "disabled"
    assert statuses["release.export"] == "dry_run"
    assert (tmp_path / "run_ledger.json").stat().st_mode & 0o777 == 0o644


def test_run_reproduction_records_successful_stage_counts_and_release_command(tmp_path: Path):
    config = _config(tmp_path, congress_from=113, congress_to=113, enable_journals=True)
    seen_commands: list[list[str]] = []
    seen_schema_env: list[dict[str, str]] = []

    def runner(command: list[str], env: dict[str, str], _cwd: Path) -> CommandResult:
        seen_commands.append(command)
        command_text = " ".join(command)
        if "ensure_schema.py" in command_text:
            seen_schema_env.append(env)
            stdout = "CREATE TABLE\n"
        elif "validate_membership_integrity.py" in command_text:
            stdout = f"Wrote 3 flagged rows to {command[-1]}\n"
        elif "load_member_service_exit_events" in command_text:
            stdout = "MEMBER SERVICE EXIT INGESTION (write): congress=113 candidates=4 inserted=2\n"
        elif "export_release.py" in command_text:
            stdout = "Assignments exported: 9\nSources exported: 7\n"
        else:
            stdout = "Events inserted: 5\nFiles processed: 2\n"
        return CommandResult(0, stdout, "", 0.01)

    exit_code = run_reproduction(config, runner=runner)

    ledger = json.loads((tmp_path / "run_ledger.json").read_text(encoding="utf-8"))
    by_id = {stage["id"]: stage for stage in ledger["stages"]}

    assert exit_code == 0
    assert ledger["run_status"] == "completed"
    assert by_id["events.113.member_service_exit"]["count_summary"] == {
        "congress": 113,
        "candidates": 4,
        "inserted": 2,
    }
    assert by_id["validation.113.membership_integrity"]["count_summary"]["flagged_rows"] == 3
    assert by_id["release.export"]["count_summary"]["assignments_exported"] == 9
    assert by_id["release.export"]["count_summary"]["sources_exported"] == 7
    assert any(command[:2] == ["python-test", "-m"] for command in seen_commands)
    assert seen_commands[-1][1:].count(str(tmp_path / "release")) == 1
    assert seen_schema_env[0]["REPRO_SOURCE_MODE"] == "local"


def test_run_reproduction_marks_failures_and_resume_skips_completed_prefix(tmp_path: Path):
    config = _config(tmp_path, congress_from=113, congress_to=113)
    first_run_commands: list[str] = []

    def failing_runner(command: list[str], _env: dict[str, str], _cwd: Path) -> CommandResult:
        stage_name = command[2] if command[1] == "-m" else Path(command[1]).name
        first_run_commands.append(stage_name)
        if stage_name == "ingest.load_members":
            return CommandResult(2, "", "boom\n", 0.02)
        return CommandResult(0, "Events inserted: 1\n", "", 0.01)

    first_exit = run_reproduction(config, runner=failing_runner)
    failed_ledger = json.loads((tmp_path / "run_ledger.json").read_text(encoding="utf-8"))
    failed_stage = next(stage for stage in failed_ledger["stages"] if stage["status"] == "failed")

    assert first_exit == 2
    assert failed_ledger["run_status"] == "failed"
    assert failed_stage["id"] == "reference.load_members"
    assert failed_stage["error"] == "boom"

    resumed_config = _config(tmp_path, congress_from=113, congress_to=113, resume=True)
    resumed_commands: list[str] = []

    def resumed_runner(command: list[str], _env: dict[str, str], _cwd: Path) -> CommandResult:
        stage_name = command[2] if command[1] == "-m" else Path(command[1]).name
        resumed_commands.append(stage_name)
        return CommandResult(0, "Events inserted: 1\n", "", 0.01)

    resumed_exit = run_reproduction(resumed_config, runner=resumed_runner)
    resumed_ledger = json.loads((tmp_path / "run_ledger.json").read_text(encoding="utf-8"))
    by_id = {stage["id"]: stage for stage in resumed_ledger["stages"]}

    assert resumed_exit == 0
    assert resumed_ledger["run_status"] == "completed"
    assert by_id["schema.apply"]["status"] == "completed"
    assert resumed_commands[0] == "ingest.load_members"
    assert "ingest.load_committees" not in resumed_commands
