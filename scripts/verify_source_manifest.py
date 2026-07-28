#!/usr/bin/env python3
"""Verify bounded required source files in one GovInfo manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.govinfo import load_source_classification, verify_required_manifest_inputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--classification-config", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()

    classification = load_source_classification(
        config_path=args.classification_config
    )
    result = verify_required_manifest_inputs(
        args.manifest,
        classification,
        base_root=args.root,
    )
    print(f"Required source rows verified: {result['verified_rows']}")
    print(f"Required source bytes verified: {result['verified_bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
