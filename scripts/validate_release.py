#!/usr/bin/env python3
"""Enforce versioned Congressional Directory validation gates for a release."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.validation_policy import DEFAULT_VALIDATION_POLICY_PATH, ValidationPolicy, load_validation_policy
from db.connection import get_connection
from validate.directory_overlap import DirectoryScore, score_database as score_directory_database


@dataclass(frozen=True)
class GateFailure:
    validation_type: str
    congress_no: int
    chamber: str
    event_type: str
    snapshot_date: str
    gate_status: str
    reason: str

    def as_row(self) -> dict:
        return asdict(self)


GATE_FAILURE_FIELDS = tuple(GateFailure.__dataclass_fields__)


def evaluate_validation_gates(
    policy: ValidationPolicy,
    *,
    congress_from: int,
    congress_to: int,
    directory_scores: Iterable[DirectoryScore],
) -> list[GateFailure]:
    failures: list[GateFailure] = []
    directory_by_pair: dict[tuple[int, str], list[DirectoryScore]] = {}
    for score in directory_scores:
        directory_by_pair.setdefault((score.congress_no, score.chamber), []).append(score)
    for congress_no in policy.directory_congresses:
        if not congress_from <= congress_no <= congress_to:
            continue
        for chamber in policy.directory_chambers:
            scores = directory_by_pair.get((congress_no, chamber), [])
            if not scores:
                failures.append(
                    GateFailure(
                        "directory_overlap",
                        congress_no,
                        chamber,
                        "",
                        "",
                        "MISSING_CELL",
                        "No required standing-committee Directory snapshot score was produced.",
                    )
                )
                continue
            for score in sorted(scores, key=lambda item: item.snapshot_date):
                if score.gate_status != "PASS":
                    failures.append(
                        GateFailure(
                            "directory_overlap",
                            congress_no,
                            chamber,
                            "",
                            score.snapshot_date.isoformat(),
                            score.gate_status,
                            "Required Directory snapshot score did not pass the policy threshold.",
                        )
                    )
    return sorted(
        failures,
        key=lambda item: (
            item.validation_type,
            item.congress_no,
            item.chamber,
            item.event_type,
            item.snapshot_date,
        ),
    )


def _write_csv(path: Path, rows: list[dict], fieldnames: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _selected(values: tuple[int, ...], congress_from: int, congress_to: int) -> list[int]:
    return [value for value in values if congress_from <= value <= congress_to]


def run_validation(
    *,
    congress_from: int,
    congress_to: int,
    output_dir: Path,
    policy: ValidationPolicy,
) -> list[GateFailure]:
    directory_congresses = _selected(policy.directory_congresses, congress_from, congress_to)
    with get_connection() as conn:
        directory_scores, directory_mismatches = score_directory_database(
            conn,
            directory_congresses,
            minimum_directory_coverage=policy.minimum_directory_coverage,
            minimum_member_resolution=policy.minimum_member_resolution,
            committee_scopes=set(policy.directory_committee_scopes),
        )

    failures = evaluate_validation_gates(
        policy,
        congress_from=congress_from,
        congress_to=congress_to,
        directory_scores=directory_scores,
    )
    directory_suffix = "_".join(str(value) for value in directory_congresses) or "none"
    _write_csv(
        output_dir / f"directory_overlap_{directory_suffix}.csv",
        [score.as_row() for score in directory_scores],
        DirectoryScore.__dataclass_fields__,
    )
    from validate.directory_overlap import DirectoryMismatch
    _write_csv(
        output_dir / f"directory_mismatches_{directory_suffix}.csv",
        [mismatch.as_row() for mismatch in directory_mismatches],
        DirectoryMismatch.__dataclass_fields__,
    )
    _write_csv(
        output_dir / "validation_gate_failures.csv",
        [failure.as_row() for failure in failures],
        GATE_FAILURE_FIELDS,
    )
    print(f"Directory score rows: {len(directory_scores)}")
    print(f"Directory mismatch rows: {len(directory_mismatches)}")
    print(f"Validation gate failures: {len(failures)}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--congress-from", type=int, required=True)
    parser.add_argument("--congress-to", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_VALIDATION_POLICY_PATH)
    parser.add_argument("--fail-on-gate", action="store_true")
    args = parser.parse_args(argv)
    if args.congress_from > args.congress_to:
        parser.error("--congress-from must be less than or equal to --congress-to")
    failures = run_validation(
        congress_from=args.congress_from,
        congress_to=args.congress_to,
        output_dir=args.output_dir,
        policy=load_validation_policy(args.policy),
    )
    if args.fail_on_gate and failures:
        print(f"VALIDATION_FAILURE: {len(failures)} required cells failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
