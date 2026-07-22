#!/usr/bin/env python3
"""Score reconstructed memberships against Congressional Directory snapshots."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_connection
from validate.directory_overlap import score_database


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--congress", type=int, nargs="+", default=[113, 114, 115])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mismatches-output", type=Path)
    parser.add_argument("--directory-min-coverage", type=float, default=0.95)
    parser.add_argument("--member-min-resolution", type=float, default=0.99)
    parser.add_argument(
        "--scope",
        nargs="+",
        choices=["standing", "select_special", "joint"],
        default=["standing"],
        help="Committee scopes to score (default: standing)",
    )
    parser.add_argument("--fail-on-gate", action="store_true")
    args = parser.parse_args()

    congresses = sorted(set(args.congress))
    suffix = "_".join(str(value) for value in congresses)
    output = args.output or Path("output/validation") / f"directory_overlap_{suffix}.csv"
    mismatches_output = args.mismatches_output or Path("output/validation") / f"directory_mismatches_{suffix}.csv"
    with get_connection() as conn:
        scores, mismatches = score_database(
            conn,
            congresses,
            minimum_directory_coverage=args.directory_min_coverage,
            minimum_member_resolution=args.member_min_resolution,
            committee_scopes=set(args.scope),
        )

    _write_csv(output, [score.as_row() for score in scores])
    _write_csv(mismatches_output, [mismatch.as_row() for mismatch in mismatches])
    print(f"Wrote {output} ({len(scores)} score rows)")
    print(f"Wrote {mismatches_output} ({len(mismatches)} mismatch rows)")
    for score in scores:
        coverage = "n/a" if score.directory_coverage_pct is None else f"{score.directory_coverage_pct:.3f}%"
        print(
            f"{score.congress_no}{score.chamber} {score.snapshot_date}: "
            f"scope={score.committee_scope} "
            f"directory={score.directory_member_entries} resolved={score.resolved_directory_assignments} "
            f"observed={score.observed_assignments} overlap={score.overlap_assignments} "
            f"coverage={coverage} gate={score.gate_status}"
        )
    failed = [score for score in scores if score.gate_status == "FAIL"]
    if args.fail_on_gate and failed:
        print(f"VALIDATION_FAILURE: {len(failed)} snapshot cells failed thresholds", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
