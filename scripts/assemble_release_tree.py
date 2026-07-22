#!/usr/bin/env python3
"""Assemble a clean Committee Steward release from an explicit file policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.check_release_tree import collect_issues, format_issues


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = ROOT / "config" / "release-files.json"
DEFAULT_RELEASE_VERSION = "v0.1.0"
DEFAULT_CONGRESS_FROM = 113
DEFAULT_CONGRESS_TO = 118


@dataclass(frozen=True)
class CopyItem:
    source: Path
    destination: PurePosixPath


@dataclass(frozen=True)
class AssemblyResult:
    destination: Path
    file_count: int
    byte_count: int
    tree_sha256: str
    complete: bool


def _relative_path(value: object, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty relative path")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} must stay within its declared root: {value}")
    return path


def _load_policy(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("release policy schema_version must equal 1")
    if not isinstance(raw.get("files"), list) or not isinstance(raw.get("trees"), list):
        raise ValueError("release policy must contain files and trees arrays")
    return raw


def _ensure_regular_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"missing {label}: {path}")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file, not a symlink: {path}")


def _source_path(root: Path, relative: PurePosixPath, label: str) -> Path:
    path = root / Path(relative.as_posix())
    _ensure_regular_file(path, label)
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside source root: {path}") from exc
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError(f"{label} traverses a symlink: {path}")
        current = current.parent
    return path


def _policy_items(source_root: Path, policy: dict[str, Any]) -> list[CopyItem]:
    items: list[CopyItem] = []
    for index, entry in enumerate(policy["files"]):
        if not isinstance(entry, dict):
            raise ValueError(f"files[{index}] must be an object")
        source_rel = _relative_path(entry.get("source"), f"files[{index}].source")
        destination = _relative_path(
            entry.get("destination"), f"files[{index}].destination"
        )
        items.append(
            CopyItem(
                source=_source_path(source_root, source_rel, f"files[{index}].source"),
                destination=destination,
            )
        )

    for index, entry in enumerate(policy["trees"]):
        if not isinstance(entry, dict):
            raise ValueError(f"trees[{index}] must be an object")
        source_rel = _relative_path(entry.get("source"), f"trees[{index}].source")
        destination_root = _relative_path(
            entry.get("destination"), f"trees[{index}].destination"
        )
        suffixes = entry.get("suffixes")
        if not isinstance(suffixes, list) or not suffixes or not all(
            isinstance(suffix, str) and suffix.startswith(".") for suffix in suffixes
        ):
            raise ValueError(f"trees[{index}].suffixes must be a non-empty suffix list")
        excluded_values = entry.get("exclude", [])
        if not isinstance(excluded_values, list):
            raise ValueError(f"trees[{index}].exclude must be a list")
        excluded = {
            _relative_path(value, f"trees[{index}].exclude")
            for value in excluded_values
        }
        tree_root = source_root / Path(source_rel.as_posix())
        if not tree_root.exists() or not tree_root.is_dir() or tree_root.is_symlink():
            raise ValueError(f"trees[{index}].source must be a real directory: {tree_root}")
        for path in sorted(tree_root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"trees[{index}].source contains a symlink: {path}")
            if path.is_file() and path.suffix in suffixes:
                relative = PurePosixPath(path.relative_to(tree_root).as_posix())
                if relative in excluded:
                    continue
                items.append(
                    CopyItem(
                        source=path,
                        destination=destination_root / relative,
                    )
                )
    return items


def _external_item(path: Path, destination: str, label: str) -> CopyItem:
    path = path.resolve()
    _ensure_regular_file(path, label)
    return CopyItem(path, _relative_path(destination, label))


def _release_artifact_names(congress_from: int, congress_to: int) -> tuple[str, ...]:
    range_label = f"{congress_from}_{congress_to}"
    return (
        f"committee_assignments_{range_label}.csv",
        f"committee_rankings_{range_label}.csv",
        f"committee_events_{range_label}.csv",
        f"committee_members_{range_label}.csv",
        f"committee_committees_{range_label}.csv",
        f"committee_sources_{range_label}.csv",
        f"validation_summary_{range_label}.csv",
        f"directory_mismatches_{range_label}.csv",
        f"committee_membership_{range_label}.xlsx",
        "release_metadata.json",
        "SHA256SUMS",
    )


def _artifact_items(
    artifacts_dir: Path,
    *,
    release_version: str,
    congress_from: int,
    congress_to: int,
) -> list[CopyItem]:
    artifacts_dir = artifacts_dir.resolve()
    if not artifacts_dir.is_dir() or artifacts_dir.is_symlink():
        raise ValueError(f"release artifacts path must be a real directory: {artifacts_dir}")
    destination_root = _relative_path(
        f"data/releases/{release_version}", "release artifact destination"
    )
    items: list[CopyItem] = []
    for name in _release_artifact_names(congress_from, congress_to):
        source = artifacts_dir / name
        _ensure_regular_file(source, f"release artifact {name}")
        items.append(CopyItem(source.resolve(), destination_root / name))
    return items


def _validate_items(items: Iterable[CopyItem]) -> list[CopyItem]:
    ordered = sorted(items, key=lambda item: item.destination.as_posix())
    seen: set[PurePosixPath] = set()
    for item in ordered:
        if item.destination in seen:
            raise ValueError(f"duplicate release destination: {item.destination}")
        seen.add(item.destination)
    return ordered


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path, destinations: Iterable[PurePosixPath]) -> str:
    digest = hashlib.sha256()
    for destination in sorted(destinations, key=PurePosixPath.as_posix):
        digest.update(destination.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(root / Path(destination.as_posix())).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def assemble_release_tree(
    *,
    source_root: Path,
    destination: Path,
    policy_path: Path,
    license_file: Path | None = None,
    source_bundle_index: Path | None = None,
    release_artifacts_dir: Path | None = None,
    release_version: str = DEFAULT_RELEASE_VERSION,
    congress_from: int = DEFAULT_CONGRESS_FROM,
    congress_to: int = DEFAULT_CONGRESS_TO,
    complete: bool = False,
) -> AssemblyResult:
    source_root = source_root.resolve()
    destination = destination.resolve()
    policy_path = policy_path.resolve()
    if not source_root.is_dir():
        raise ValueError(f"source root must be a directory: {source_root}")
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    if congress_from > congress_to:
        raise ValueError("congress_from must be less than or equal to congress_to")

    try:
        destination.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("destination must not be inside the source working tree")

    if complete:
        missing_options = [
            name
            for name, value in (
                ("--license-file", license_file),
                ("--source-bundle-index", source_bundle_index),
                ("--release-artifacts-dir", release_artifacts_dir),
            )
            if value is None
        ]
        if missing_options:
            raise ValueError(
                "--complete requires " + ", ".join(missing_options)
            )

    items = _policy_items(source_root, _load_policy(policy_path))
    if license_file is not None:
        items.append(_external_item(license_file, "LICENSE", "license file"))
    if source_bundle_index is not None:
        items.append(
            _external_item(
                source_bundle_index,
                "manifests/source-bundles.json",
                "source bundle index",
            )
        )
    if release_artifacts_dir is not None:
        items.extend(
            _artifact_items(
                release_artifacts_dir,
                release_version=release_version,
                congress_from=congress_from,
                congress_to=congress_to,
            )
        )
    ordered = _validate_items(items)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.assembling-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for item in ordered:
            target = staging / Path(item.destination.as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item.source, target)

        issues = collect_issues(staging)
        if issues:
            raise ValueError("assembled tree failed safety checks:\n" + format_issues(issues))

        file_count = len(ordered)
        byte_count = sum(
            (staging / Path(item.destination.as_posix())).stat().st_size
            for item in ordered
        )
        tree_sha256 = _tree_digest(
            staging, (item.destination for item in ordered)
        )
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return AssemblyResult(
        destination=destination,
        file_count=file_count,
        byte_count=byte_count,
        tree_sha256=tree_sha256,
        complete=complete,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--license-file", type=Path)
    parser.add_argument("--source-bundle-index", type=Path)
    parser.add_argument("--release-artifacts-dir", type=Path)
    parser.add_argument("--release-version", default=DEFAULT_RELEASE_VERSION)
    parser.add_argument("--congress-from", type=int, default=DEFAULT_CONGRESS_FROM)
    parser.add_argument("--congress-to", type=int, default=DEFAULT_CONGRESS_TO)
    parser.add_argument(
        "--complete",
        action="store_true",
        help="Require the license, source-bundle index, and all canonical release artifacts.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = assemble_release_tree(
            source_root=args.source_root,
            destination=args.destination,
            policy_path=args.policy,
            license_file=args.license_file,
            source_bundle_index=args.source_bundle_index,
            release_artifacts_dir=args.release_artifacts_dir,
            release_version=args.release_version,
            congress_from=args.congress_from,
            congress_to=args.congress_to,
            complete=args.complete,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    mode = "complete" if result.complete else "code-only"
    print(
        f"PASS assembled {mode} release tree: files={result.file_count} "
        f"bytes={result.byte_count} tree_sha256={result.tree_sha256} "
        f"destination={result.destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
