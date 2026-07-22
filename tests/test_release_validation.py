from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from core.validation_policy import ValidationPolicy, load_validation_policy
from scripts.validate_release import evaluate_validation_gates
from validate.directory_overlap import DirectoryScore


def _policy() -> ValidationPolicy:
    return ValidationPolicy(
        version="test",
        directory_congresses=(113,),
        directory_chambers=("H", "S"),
        directory_committee_scopes=("standing",),
        minimum_directory_coverage=0.95,
        minimum_member_resolution=0.99,
        maximum_membership_issue_rows=0,
    )


def _directory(chamber: str, status: str = "PASS") -> DirectoryScore:
    return DirectoryScore(
        congress_no=113,
        snapshot_date=date(2014, 1, 1),
        chamber=chamber,
        committee_scope="standing",
        directory_member_entries=10,
        resolved_directory_assignments=10,
        unresolved_member_entries=0,
        unmapped_committee_entries=0,
        unmapped_committees=0,
        observed_assignments=10,
        overlap_assignments=10,
        directory_only_assignments=0,
        observed_only_assignments=0,
        member_resolution_pct=100.0,
        directory_coverage_pct=100.0,
        observed_overlap_pct=100.0,
        gate_status=status,
    )


def test_required_validation_cells_all_pass() -> None:
    failures = evaluate_validation_gates(
        _policy(),
        congress_from=113,
        congress_to=113,
        directory_scores=[_directory("H"), _directory("S")],
    )

    assert failures == []


def test_missing_and_failed_required_cells_fail_closed() -> None:
    failures = evaluate_validation_gates(
        _policy(),
        congress_from=113,
        congress_to=113,
        directory_scores=[_directory("H", "NO_REFERENCE")],
    )

    assert [
        (row.validation_type, row.chamber, row.gate_status) for row in failures
    ] == [
        ("directory_overlap", "H", "NO_REFERENCE"),
        ("directory_overlap", "S", "MISSING_CELL"),
    ]


def test_policy_loader_rejects_out_of_range_threshold(tmp_path: Path) -> None:
    raw = json.loads(
        Path("config/release-validation-policy.json").read_text(encoding="utf-8")
    )
    raw["directory"]["minimum_directory_coverage"] = 1.1
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="between 0 and 1"):
        load_validation_policy(path)


def test_production_policy_loads() -> None:
    policy = load_validation_policy(Path("config/release-validation-policy.json"))

    assert policy.version == "1.1.0"
    assert policy.directory_congresses == (113, 114, 115, 116, 117, 118)
    assert policy.directory_committee_scopes == ("standing",)
