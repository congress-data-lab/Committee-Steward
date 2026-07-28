#!/usr/bin/env python3
"""Verify two canonical release directories and require byte-identical artifacts."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release_directory(path: Path) -> dict[str, str]:
    checksum_path = path / "SHA256SUMS"
    if not checksum_path.is_file():
        raise ValueError(f"{path} is missing SHA256SUMS")

    checksums: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        expected, separator, filename = line.partition("  ")
        if not separator or not expected or not filename:
            raise ValueError(f"Malformed SHA256SUMS line in {path}: {line!r}")
        relative = PurePosixPath(filename)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"Unsafe SHA256SUMS path in {path}: {filename}")
        artifact = path / relative
        if not artifact.is_file():
            raise ValueError(f"{path} is missing checksummed artifact {filename}")
        actual = sha256_file(artifact)
        if actual != expected:
            raise ValueError(
                f"Checksum mismatch in {path} for {filename}: {actual} != {expected}"
            )
        checksums[filename] = actual

    actual_files = {
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_file() and item != checksum_path
    }
    if actual_files != set(checksums):
        missing = sorted(set(checksums) - actual_files)
        unexpected = sorted(actual_files - set(checksums))
        raise ValueError(
            f"Artifact inventory mismatch in {path}: missing={missing}, unexpected={unexpected}"
        )
    return checksums


def compare_release_directories(first: Path, second: Path) -> dict[str, str]:
    first_checksums = verify_release_directory(first)
    second_checksums = verify_release_directory(second)
    if first_checksums != second_checksums:
        filenames = sorted(set(first_checksums) | set(second_checksums))
        mismatches = [
            filename
            for filename in filenames
            if first_checksums.get(filename) != second_checksums.get(filename)
        ]
        raise ValueError(
            "Release artifacts are not byte-identical: " + ", ".join(mismatches)
        )
    return first_checksums


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    args = parser.parse_args()

    try:
        checksums = compare_release_directories(args.first, args.second)
    except ValueError as exc:
        parser.exit(2, f"ERROR: {exc}\n")
    print(f"Verified {len(checksums)} byte-identical release artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
