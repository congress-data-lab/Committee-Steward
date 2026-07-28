#!/usr/bin/env python3
"""Hydrate source files from a local source bundle archive or source-bundles index."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.govinfo import hydrate_source_bundle, hydrate_source_bundle_from_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path, help="Local source bundle archive to extract")
    source.add_argument("--index", type=Path, help="Provider-neutral source-bundles.json index")
    parser.add_argument("--congress", type=int, help="Congress number to hydrate when using --index")
    parser.add_argument("--root", type=Path, default=Path("."), help="Destination root for hydrated files")
    args = parser.parse_args()

    if args.index and args.congress is None:
        parser.error("--congress is required when using --index")

    if args.archive:
        result = hydrate_source_bundle(args.archive, args.root)
    else:
        result = hydrate_source_bundle_from_index(args.index, args.congress, args.root)

    print(f"Hydrated Congress {result['congress_no']} into {args.root}")
    print(f"Verified {result['entries']} logical bundle entries")
    print(f"Embedded manifest: {result['manifest_path']} ({result['manifest_sha256']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
