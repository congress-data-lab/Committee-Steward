#!/usr/bin/env python3
"""Export canonical standing-committee release artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.export.release import export_release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--congress-from", type=int, required=True, help="Inclusive first Congress number.")
    parser.add_argument("--congress-to", type=int, required=True, help="Inclusive last Congress number.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/exports/release"),
        help="Directory that receives CSVs, workbook, metadata, and SHA256SUMS.",
    )
    parser.add_argument(
        "--release-version",
        default="v0.1.0",
        help="Release version label written into artifacts (default: v0.1.0).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Optional PostgreSQL connection URL. Falls back to NEON_DATABASE_URL when omitted.",
    )
    parser.add_argument(
        "--git-commit",
        default=None,
        help="Optional git commit override for release metadata.",
    )
    args = parser.parse_args()

    metadata = export_release(
        congress_from=args.congress_from,
        congress_to=args.congress_to,
        output_dir=args.output_dir,
        release_version=args.release_version,
        database_url=args.database_url,
        git_commit=args.git_commit,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
