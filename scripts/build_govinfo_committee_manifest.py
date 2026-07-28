#!/usr/bin/env python3
"""Build a deterministic GovInfo primary-source manifest for one Congress."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.govinfo import (
    GovInfoClient,
    build_committee_manifest,
    prepare_crec_archives,
    prepare_house_journal_archives,
    retrieve_missing_packages,
    read_manifest_csv,
    refresh_house_journal_local_state,
    refresh_manifest_local_state,
    validate_manifest,
    write_manifest_csv,
)


def _api_key(args: argparse.Namespace) -> str:
    if args.demo_key:
        return "DEMO_KEY"
    key = os.environ.get("GOVINFO_API_KEY")
    if key:
        return key
    raise SystemExit("Set GOVINFO_API_KEY or use --demo-key for a tiny probe")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("congress", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-crec-granules", action="store_true")
    parser.add_argument("--exclude-journals", action="store_true")
    parser.add_argument("--exclude-directory", action="store_true")
    parser.add_argument("--require-local", action="store_true")
    parser.add_argument("--download-missing", action="store_true")
    parser.add_argument(
        "--prepare-crec",
        action="store_true",
        help=(
            "Safely extract downloaded CREC ZIPs and generate loader-ready JSON "
            "with an explicitly pinned congressional-record parser checkout."
        ),
    )
    parser.add_argument(
        "--complete-crec",
        action="store_true",
        help=(
            "Repeat bounded download/normalize cycles until this Congress's "
            "CREC package inventory is complete."
        ),
    )
    parser.add_argument("--crec-parser-root", type=Path)
    parser.add_argument(
        "--crec-parser-revision",
        help="Required full 40-character Git SHA for --prepare-crec.",
    )
    parser.add_argument("--crec-parser-python", type=Path)
    parser.add_argument(
        "--crec-raw-staging",
        type=Path,
        help="Move provenance-recorded raw CREC ZIPs here after each normalized batch.",
    )
    parser.add_argument(
        "--complete-crec-max-batches",
        type=int,
        default=0,
        help="Stop after N complete cycles; 0 means continue until complete.",
    )
    parser.add_argument(
        "--prepare-house-journal-year",
        action="append",
        type=int,
        default=[],
        help=(
            "Safely extract one already-downloaded authoritative House Journal "
            "ZIP; repeat for multiple years. Performs no network access."
        ),
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Rehash an existing output manifest instead of repeating discovery.",
    )
    parser.add_argument(
        "--refresh-local-type",
        action="append",
        default=[],
        help=(
            "Local file expected_type to rehash in --refresh-existing mode. "
            "Defaults to the selected download types, or House/Senate resolutions."
        ),
    )
    parser.add_argument(
        "--rehash-retrieved",
        action="store_true",
        help=(
            "Rehash already-retrieved selected files. Disabled by default to "
            "keep refreshes metadata-only and bounded."
        ),
    )
    parser.add_argument(
        "--rehash-source-trees",
        action="store_true",
        help="Allow deliberate recursive hashing of normalized CREC/journal directories.",
    )
    parser.add_argument(
        "--download-missing-type",
        action="append",
        default=[],
        help=(
            "Limit downloads to one expected_type; repeat as needed. "
            "Implies --download-missing."
        ),
    )
    parser.add_argument(
        "--download-missing-candidate-class",
        action="append",
        default=[],
        help="Limit downloads to one manifest candidate_class; repeat as needed.",
    )
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=25,
        help="Hard cap for one invocation's package downloads (default: 25).",
    )
    parser.add_argument(
        "--download-batch-size",
        type=int,
        help="Download only the first N eligible missing packages in manifest order.",
    )
    parser.add_argument(
        "--download-workers",
        type=int,
        default=1,
        choices=range(1, 5),
        metavar="N",
        help="Concurrent package downloads, from 1 to 4 (default: 1).",
    )
    parser.add_argument("--demo-key", action="store_true")
    args = parser.parse_args()

    output = args.output or Path("data/manifests") / f"manifest_{args.congress}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_suffix(output.suffix + ".lock")
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit(
            f"Another manifest acquisition run already holds {lock_path}"
        ) from exc
    if args.refresh_existing:
        if not output.exists():
            raise SystemExit(f"Cannot refresh missing manifest: {output}")
        refresh_types = set(args.refresh_local_type)
        if not refresh_types:
            refresh_types = set(args.download_missing_type) or {
                "house_resolution",
                "senate_resolution",
            }
        rows = refresh_manifest_local_state(
            read_manifest_csv(output),
            expected_types=refresh_types,
            rehash_retrieved=args.rehash_retrieved,
            rehash_directories=args.rehash_source_trees,
        )
        if {row.congress_no for row in rows} != {args.congress}:
            raise SystemExit(f"{output} does not describe Congress {args.congress}")
    else:
        client = GovInfoClient(_api_key(args))
        rows = build_committee_manifest(
            client,
            args.congress,
            include_crec_granules=args.include_crec_granules,
            include_journals=not args.exclude_journals,
            include_directory=not args.exclude_directory,
        )
    if args.complete_crec:
        if (
            args.crec_parser_root is None
            or args.crec_parser_revision is None
            or args.crec_raw_staging is None
        ):
            raise SystemExit(
                "--complete-crec requires --crec-parser-root, "
                "--crec-parser-revision, and --crec-raw-staging"
            )
        client = GovInfoClient(_api_key(args))
        batch_size = args.download_batch_size or 48
        if batch_size > args.max_downloads:
            raise SystemExit(
                "--max-downloads must be at least the CREC batch size"
            )
        completed_batches = 0
        while True:
            rows = refresh_manifest_local_state(
                rows,
                expected_types={"congressional_record_package"},
            )
            missing_before = sum(
                row.expected_type == "congressional_record_package"
                and row.status != "RETRIEVED"
                for row in rows
            )
            if missing_before == 0:
                break
            if (
                args.complete_crec_max_batches
                and completed_batches >= args.complete_crec_max_batches
            ):
                break
            rows = retrieve_missing_packages(
                client,
                rows,
                expected_types={"congressional_record_package"},
                max_downloads=batch_size,
                download_batch_size=batch_size,
                download_workers=args.download_workers,
            )
            write_manifest_csv(output, rows)
            prepared = prepare_crec_archives(
                args.congress,
                parser_root=args.crec_parser_root,
                parser_revision=args.crec_parser_revision,
                parser_python=args.crec_parser_python,
            )
            prepared_ids = {extraction.package_id for extraction, _ in prepared}
            rows = refresh_manifest_local_state(
                rows,
                expected_types={"congressional_record_package"},
                rehash_retrieved=True,
                rehash_directories=True,
                identifiers=prepared_ids,
            )
            write_manifest_csv(output, rows)
            staging = args.crec_raw_staging / str(args.congress)
            staging.mkdir(parents=True, exist_ok=True)
            for extraction, _ in prepared:
                target = staging / extraction.archive.name
                if target.exists():
                    raise SystemExit(
                        f"Refusing to replace staged authoritative ZIP: {target}"
                    )
                shutil.move(str(extraction.archive), target)
            completed_batches += 1
            missing_after = sum(
                row.expected_type == "congressional_record_package"
                and row.status != "RETRIEVED"
                for row in rows
            )
            print(
                f"CREC batch {completed_batches}: prepared {len(prepared)} packages; "
                f"{missing_after} of {missing_before} remain",
                flush=True,
            )
            if missing_after >= missing_before:
                raise SystemExit(
                    "CREC completion made no manifest progress; inspect retrieval failures"
                )
        write_manifest_csv(output, rows)
        statuses = Counter(row.status for row in rows)
        types = Counter(row.expected_type for row in rows)
        print(f"Wrote {output} ({len(rows)} rows)")
        print("Status counts:", dict(sorted(statuses.items())))
        print("Type counts:", dict(sorted(types.items())))
        return 0
    if (
        args.prepare_house_journal_year
        or "house_journal_package" in set(args.download_missing_type)
    ):
        # Older manifests predate authoritative House Journal ZIP rows. Add
        # both raw and extracted provenance before retrieval/preparation so
        # --refresh-existing can migrate them without live rediscovery.
        rows = refresh_house_journal_local_state(rows)
    if (
        args.download_missing
        or args.download_missing_type
        or args.download_missing_candidate_class
    ):
        client = GovInfoClient(_api_key(args))
        selected_types = set(args.download_missing_type) or None
        selected_candidates = set(args.download_missing_candidate_class) or None
        rows = retrieve_missing_packages(
            client,
            rows,
            expected_types=selected_types,
            candidate_classes=selected_candidates,
            max_downloads=args.max_downloads,
            download_batch_size=args.download_batch_size,
            download_workers=args.download_workers,
        )
    if args.prepare_crec:
        if args.crec_parser_root is None or args.crec_parser_revision is None:
            raise SystemExit(
                "--prepare-crec requires --crec-parser-root and --crec-parser-revision"
            )
        prepared = prepare_crec_archives(
            args.congress,
            parser_root=args.crec_parser_root,
            parser_revision=args.crec_parser_revision,
            parser_python=args.crec_parser_python,
        )
        rows = refresh_manifest_local_state(
            rows,
            expected_types={"congressional_record_package"},
            rehash_retrieved=True,
            rehash_directories=True,
            identifiers={extraction.package_id for extraction, _ in prepared},
        )
        print(
            "Prepared CREC packages:",
            len(prepared),
            "ZIPs;",
            sum(parsed.file_count for _, parsed in prepared),
            "JSON files",
        )
    if args.prepare_house_journal_year:
        prepared_journals = prepare_house_journal_archives(
            args.congress,
            years=args.prepare_house_journal_year,
        )
        rows = refresh_house_journal_local_state(rows)
        print(
            "Prepared House Journal packages:",
            ", ".join(result.package_id for result in prepared_journals),
        )
    write_manifest_csv(output, rows)

    statuses = Counter(row.status for row in rows)
    types = Counter(row.expected_type for row in rows)
    print(f"Wrote {output} ({len(rows)} rows)")
    print("Status counts:", dict(sorted(statuses.items())))
    print("Type counts:", dict(sorted(types.items())))

    failures = validate_manifest(rows, require_local=args.require_local)
    if failures:
        print(f"RETRIEVAL_FAILURE: {len(failures)} required artifacts are missing locally", file=sys.stderr)
        for row in failures[:20]:
            print(f"  {row.identifier}: {row.error_text} ({row.local_path})", file=sys.stderr)
        if len(failures) > 20:
            print(f"  ... {len(failures) - 20} more; see {output}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
