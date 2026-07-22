#!/usr/bin/env python3
"""Build a deterministic, loader-ready source bundle from a GovInfo manifest."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.govinfo import build_source_bundle, load_source_classification, read_manifest_csv
from core.govinfo.bundle import SourceClassification
from core.govinfo.crec import CREC_PACKAGE_RE
from core.govinfo.manifest import ManifestRow, write_manifest_csv


LOADER_READY_FILE_SUFFIXES = {
    "crec_normalization_provenance": ".json",
    "directory_snapshot_normalized": ".json",
    "house_resolution": ".xml",
    "senate_resolution": ".xml",
}
LOADER_READY_EXPECTED_TYPES = frozenset(
    {"congressional_record_package", *LOADER_READY_FILE_SUFFIXES}
)
EXCLUDED_STATUS = "EXCLUDED_NOT_LOADER_READY"


def build_loader_ready_source_bundle(
    manifest_path: Path,
    archive_path: Path,
    classification: SourceClassification,
    *,
    base_root: Path = Path("."),
    index_path: Path | None = None,
    archive_url: str | None = None,
) -> dict[str, Any]:
    """Build a bundle from only the files consumed by canonical loaders."""
    resolved_root = base_root.resolve()
    resolved_manifest = _resolve_manifest(manifest_path, resolved_root)
    manifest_relpath = resolved_manifest.relative_to(resolved_root)
    rows = read_manifest_csv(resolved_manifest)

    resolved_archive = _resolve_output(archive_path, resolved_root)
    resolved_index = (
        _resolve_output(index_path, resolved_root) if index_path is not None else None
    )

    with tempfile.TemporaryDirectory(prefix="committee-source-bundle-") as temporary:
        staging_root = Path(temporary)
        staged_rows = [
            _stage_loader_ready_row(row, classification, resolved_root, staging_root)
            for row in rows
        ]
        write_manifest_csv(staging_root / manifest_relpath, staged_rows)
        entry = build_source_bundle(
            manifest_relpath,
            resolved_archive,
            classification,
            base_root=staging_root,
            index_path=resolved_index,
            archive_url=archive_url,
        )

    if resolved_index is None:
        entry["archive"]["path"] = Path(
            os.path.relpath(resolved_archive, resolved_root)
        ).as_posix()
    return entry


def _stage_loader_ready_row(
    row: ManifestRow,
    classification: SourceClassification,
    source_root: Path,
    staging_root: Path,
) -> ManifestRow:
    category = classification.category_for(row.expected_type, row.candidate_class)
    if row.expected_type not in LOADER_READY_EXPECTED_TYPES:
        if category == "required":
            raise ValueError(
                f"Required source type {row.expected_type!r} is not loader-ready"
            )
        if category == "optional" and row.status == "RETRIEVED":
            return replace(
                row,
                status=EXCLUDED_STATUS,
                error_text="Excluded from frozen bundle because no canonical loader consumes it",
            )
        return row

    if row.status != "RETRIEVED" or not row.local_path:
        return row

    row_relpath = _normalize_relative_path(row.local_path)
    source_path = _resolve_under_root(source_root, row_relpath)
    if row.expected_type == "congressional_record_package":
        if source_path.is_file() and source_path.suffix.lower() == ".zip":
            package_id = row.package_id or row.identifier
            match = CREC_PACKAGE_RE.fullmatch(package_id)
            if match is None:
                raise ValueError(
                    f"Cannot locate normalized CREC JSON for {row.identifier}: "
                    f"invalid package id {package_id!r}"
                )
            row_relpath = PurePosixPath(
                "data", "crec", match.group("year"), package_id
            )
            source_path = _resolve_under_root(source_root, row_relpath)
        return _stage_crec_json(row, row_relpath, source_path, staging_root)

    expected_suffix = LOADER_READY_FILE_SUFFIXES[row.expected_type]
    if not source_path.is_file() or source_path.suffix.lower() != expected_suffix:
        raise ValueError(
            f"{row.identifier} must reference a {expected_suffix} loader input: "
            f"{source_path}"
        )
    _stage_symlink(source_path, staging_root / Path(row_relpath.as_posix()))
    return row


def _stage_crec_json(
    row: ManifestRow,
    row_relpath: PurePosixPath,
    source_path: Path,
    staging_root: Path,
) -> ManifestRow:
    json_root = source_path if source_path.name == "json" else source_path / "json"
    if not json_root.is_dir():
        raise ValueError(
            f"{row.identifier} is missing its loader-ready JSON directory: {json_root}"
        )

    json_files = sorted(
        path for path in json_root.rglob("*") if path.is_file() and path.suffix.lower() == ".json"
    )
    if not json_files:
        raise ValueError(f"{row.identifier} has no loader-ready JSON files in {json_root}")

    projected_relpath = (
        row_relpath if row_relpath.name == "json" else row_relpath / "json"
    )
    for source_file in json_files:
        relative_file = source_file.relative_to(json_root)
        _stage_symlink(
            source_file.resolve(),
            staging_root / Path(projected_relpath.as_posix()) / relative_file,
        )

    size_bytes, sha256 = _selected_tree_stats(json_root, json_files)
    return replace(
        row,
        local_path=projected_relpath.as_posix(),
        size_bytes=size_bytes,
        sha256=sha256,
    )


def _resolve_manifest(manifest_path: Path, root: Path) -> Path:
    resolved = (
        manifest_path.resolve()
        if manifest_path.is_absolute()
        else (root / manifest_path).resolve()
    )
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Manifest {resolved} is outside bundle root {root}") from exc
    return resolved


def _resolve_output(path: Path, root: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _normalize_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ValueError(f"Invalid bundle source path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Invalid bundle source path: {value!r}")
    return path


def _resolve_under_root(root: Path, relative_path: PurePosixPath) -> Path:
    resolved = (root / Path(relative_path.as_posix())).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Bundle source path escapes root: {relative_path}") from exc
    return resolved


def _stage_symlink(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Duplicate staged bundle path: {destination}")
    destination.symlink_to(source)


def _selected_tree_stats(root: Path, files: list[Path]) -> tuple[int, str]:
    digest = hashlib.sha256()
    total_size = 0
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                total_size += len(chunk)
                digest.update(chunk)
        digest.update(b"\0")
    return total_size, digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Existing GovInfo manifest CSV")
    parser.add_argument("--archive", type=Path, help="Output archive path (.zip)")
    parser.add_argument("--index", type=Path, help="Optional source-bundles.json path to update")
    parser.add_argument("--archive-url", help="Optional provider-neutral published archive URL")
    parser.add_argument("--root", type=Path, default=Path("."), help="Repository root used to resolve manifest paths")
    parser.add_argument("--classification-config", type=Path, help="JSON file with required/optional/validation_only arrays")
    parser.add_argument("--required-type", action="append", default=[], help="expected_type to mark required")
    parser.add_argument("--optional-type", action="append", default=[], help="expected_type to mark optional")
    parser.add_argument(
        "--validation-only-type",
        action="append",
        default=[],
        help="expected_type to mark validation-only and exclude from the bundle archive",
    )
    args = parser.parse_args()

    classification = load_source_classification(
        config_path=args.classification_config,
        required_types=args.required_type,
        optional_types=args.optional_type,
        validation_only_types=args.validation_only_type,
    )
    manifest_path = args.manifest if args.manifest.is_absolute() else args.root / args.manifest
    manifest_rows = read_manifest_csv(manifest_path)
    if not manifest_rows:
        raise SystemExit(f"{manifest_path} is empty")

    congress_no = manifest_rows[0].congress_no
    archive = args.archive or Path("bundles") / f"source-bundle-{congress_no}.zip"
    bundle_entry = build_loader_ready_source_bundle(
        args.manifest,
        archive,
        classification,
        base_root=args.root,
        index_path=args.index,
        archive_url=args.archive_url,
    )

    archive_info = bundle_entry["archive"]
    print(f"Wrote {archive} for Congress {bundle_entry['congress_no']}")
    print(f"Archive SHA-256: {archive_info['sha256']}")
    print(f"Archive bytes: {archive_info['size_bytes']}")
    if args.index:
        print(f"Updated {args.index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
