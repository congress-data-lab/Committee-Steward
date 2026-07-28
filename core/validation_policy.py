"""Load and validate the versioned production validation policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VALIDATION_POLICY_PATH = ROOT / "config" / "release-validation-policy.json"


@dataclass(frozen=True)
class ValidationPolicy:
    version: str
    directory_congresses: tuple[int, ...]
    directory_chambers: tuple[str, ...]
    directory_committee_scopes: tuple[str, ...]
    minimum_directory_coverage: float
    minimum_member_resolution: float
    maximum_membership_issue_rows: int


def _string(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return raw.strip()


def _values(raw: object, field: str, allowed: set[str]) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field} must be a non-empty array")
    values = tuple(_string(value, field) for value in raw)
    invalid = sorted(set(values) - allowed)
    if invalid:
        raise ValueError(f"{field} contains invalid values: {invalid}")
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must not contain duplicates")
    return values


def _congresses(raw: object, field: str) -> tuple[int, ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(value, int) for value in raw):
        raise ValueError(f"{field} must be a non-empty integer array")
    values = tuple(sorted(raw))
    if len(values) != len(set(values)) or any(value <= 0 for value in values):
        raise ValueError(f"{field} must contain unique positive integers")
    return values


def _ratio(raw: object, field: str) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError(f"{field} must be a numeric ratio")
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return value


def load_validation_policy(path: Path | None = None) -> ValidationPolicy:
    policy_path = (path or DEFAULT_VALIDATION_POLICY_PATH).resolve()
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    directory = raw.get("directory")
    membership = raw.get("membership_integrity")
    if not all(isinstance(section, dict) for section in (directory, membership)):
        raise ValueError("validation policy is missing a required object section")
    maximum_issues = membership.get("maximum_issue_rows")
    if not isinstance(maximum_issues, int) or isinstance(maximum_issues, bool) or maximum_issues < 0:
        raise ValueError("membership_integrity.maximum_issue_rows must be a non-negative integer")
    return ValidationPolicy(
        version=_string(raw.get("version"), "version"),
        directory_congresses=_congresses(directory.get("congresses"), "directory.congresses"),
        directory_chambers=_values(directory.get("chambers"), "directory.chambers", {"H", "S"}),
        directory_committee_scopes=_values(
            directory.get("committee_scopes"),
            "directory.committee_scopes",
            {"standing", "select_special", "joint"},
        ),
        minimum_directory_coverage=_ratio(
            directory.get("minimum_directory_coverage"),
            "directory.minimum_directory_coverage",
        ),
        minimum_member_resolution=_ratio(
            directory.get("minimum_member_resolution"),
            "directory.minimum_member_resolution",
        ),
        maximum_membership_issue_rows=maximum_issues,
    )
