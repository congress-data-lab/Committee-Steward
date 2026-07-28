"""Deterministic source-bundle build and hydration helpers for GovInfo manifests."""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from core.govinfo.manifest import MANIFEST_FIELDS, ManifestRow


SOURCE_BUNDLE_SCHEMA_VERSION = "govinfo_source_bundle_v1"
SOURCE_BUNDLE_INDEX_VERSION = "govinfo_source_bundles_v1"
SOURCE_BUNDLE_TOOL_VERSION = "1"
BUNDLE_METADATA_NAME = "bundle-metadata.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class SourceClassification:
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    validation_only: tuple[str, ...] = ()
    required_candidate_classes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = tuple(_normalize_type_name(value) for value in self.required)
        optional = tuple(_normalize_type_name(value) for value in self.optional)
        validation_only = tuple(_normalize_type_name(value) for value in self.validation_only)
        required_candidate_classes = tuple(
            _normalize_type_name(value) for value in self.required_candidate_classes
        )
        object.__setattr__(self, "required", required)
        object.__setattr__(self, "optional", optional)
        object.__setattr__(self, "validation_only", validation_only)
        object.__setattr__(
            self, "required_candidate_classes", required_candidate_classes
        )

        duplicates = (
            set(required) & set(optional)
            or set(required) & set(validation_only)
            or set(optional) & set(validation_only)
        )
        if duplicates:
            duplicate_list = ", ".join(sorted(duplicates))
            raise ValueError(f"Expected types cannot appear in multiple classes: {duplicate_list}")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SourceClassification":
        return cls(
            required=tuple(data.get("required", ()) or ()),
            optional=tuple(data.get("optional", ()) or ()),
            validation_only=tuple(data.get("validation_only", ()) or ()),
            required_candidate_classes=tuple(
                data.get("required_candidate_classes", ()) or ()
            ),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "required": sorted(self.required),
            "optional": sorted(self.optional),
            "validation_only": sorted(self.validation_only),
            "required_candidate_classes": sorted(
                self.required_candidate_classes
            ),
        }

    def category_for(
        self, expected_type: str, candidate_class: str = ""
    ) -> str | None:
        if _normalize_optional_name(candidate_class) in set(
            self.required_candidate_classes
        ):
            return "required"
        normalized = _normalize_type_name(expected_type)
        if normalized in self.required:
            return "required"
        if normalized in self.optional:
            return "optional"
        if normalized in self.validation_only:
            return "validation_only"
        return None


@dataclass(frozen=True)
class BundleEntry:
    category: str
    expected_type: str
    identifier: str
    local_path: str
    path_type: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class SkippedOptionalEntry:
    expected_type: str
    identifier: str
    local_path: str
    reason: str


def load_source_classification(
    *,
    config_path: Path | None = None,
    required_types: Iterable[str] = (),
    optional_types: Iterable[str] = (),
    validation_only_types: Iterable[str] = (),
) -> SourceClassification:
    payload: dict[str, Any] = {}
    if config_path is not None:
        payload = json.loads(config_path.read_text(encoding="utf-8"))

    merged_required = list(payload.get("required", ()) or ())
    merged_required.extend(required_types)
    merged_optional = list(payload.get("optional", ()) or ())
    merged_optional.extend(optional_types)
    merged_validation_only = list(payload.get("validation_only", ()) or ())
    merged_validation_only.extend(validation_only_types)
    required_candidate_classes = list(
        payload.get("required_candidate_classes", ()) or ()
    )

    classification = SourceClassification(
        required=tuple(merged_required),
        optional=tuple(merged_optional),
        validation_only=tuple(merged_validation_only),
        required_candidate_classes=tuple(required_candidate_classes),
    )
    if not any((classification.required, classification.optional, classification.validation_only)):
        raise ValueError("At least one required, optional, or validation-only expected_type must be configured")
    return classification


def read_manifest_csv(path: Path) -> list[ManifestRow]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != MANIFEST_FIELDS:
            raise ValueError(f"{path} does not match the expected GovInfo manifest header")

        rows: list[ManifestRow] = []
        for record in reader:
            rows.append(
                ManifestRow(
                    ruleset_id=record["ruleset_id"],
                    congress_no=int(record["congress_no"]),
                    collection_code=record["collection_code"],
                    chamber=record["chamber"],
                    artifact_level=record["artifact_level"],
                    identifier=record["identifier"],
                    package_id=record["package_id"],
                    granule_id=record["granule_id"],
                    expected_type=record["expected_type"],
                    candidate_class=record["candidate_class"],
                    title=record["title"],
                    date_issued=record["date_issued"],
                    last_modified=record["last_modified"],
                    source_url_or_api_call=record["source_url_or_api_call"],
                    details_url=record["details_url"],
                    local_path=record["local_path"],
                    size_bytes=int(record["size_bytes"]) if record["size_bytes"] else "",
                    sha256=record["sha256"],
                    status=record["status"],
                    error_text=record["error_text"],
                )
            )
    return rows


def verify_required_manifest_inputs(
    manifest_path: Path,
    classification: SourceClassification,
    *,
    base_root: Path = Path("."),
) -> dict[str, int]:
    """Verify required file inputs without walking optional corpus directories."""
    resolved_manifest = (
        manifest_path if manifest_path.is_absolute() else base_root / manifest_path
    )
    rows = read_manifest_csv(resolved_manifest)
    if not rows:
        raise ValueError(f"{resolved_manifest} is empty")
    expected_types = {row.expected_type for row in rows if row.expected_type}
    missing_types = sorted(set(classification.required) - expected_types)
    if missing_types:
        raise ValueError(
            "Manifest is missing required expected_type values: "
            + ", ".join(missing_types)
        )
    candidate_classes = {row.candidate_class for row in rows if row.candidate_class}
    missing_candidate_classes = sorted(
        set(classification.required_candidate_classes) - candidate_classes
    )
    if missing_candidate_classes:
        raise ValueError(
            "Manifest is missing required candidate_class values: "
            + ", ".join(missing_candidate_classes)
        )

    verified_rows = 0
    verified_bytes = 0
    failures: list[str] = []
    for row in sorted(rows, key=_manifest_sort_key):
        if (
            classification.category_for(row.expected_type, row.candidate_class)
            != "required"
        ):
            continue
        if row.status != "RETRIEVED" or not row.local_path:
            failures.append(f"{row.identifier}: {row.status or 'missing local path'}")
            continue
        relative_path = _normalize_relative_path(row.local_path)
        source_path = base_root / relative_path
        if source_path.is_dir():
            failures.append(
                f"{row.identifier}: required directory verification is disabled in the bounded lane"
            )
            continue
        if not source_path.is_file():
            failures.append(f"{row.identifier}: {source_path} is missing")
            continue
        actual_size, actual_hash = _tree_stats(source_path)
        expected_size = _coerce_int(row.size_bytes)
        if expected_size != actual_size or row.sha256 != actual_hash:
            failures.append(
                f"{row.identifier}: expected {expected_size}/{row.sha256}, "
                f"got {actual_size}/{actual_hash}"
            )
            continue
        verified_rows += 1
        verified_bytes += actual_size

    if failures:
        raise ValueError(
            "Required manifest verification failed:\n" + "\n".join(failures)
        )
    return {"verified_rows": verified_rows, "verified_bytes": verified_bytes}


def build_source_bundle(
    manifest_path: Path,
    archive_path: Path,
    classification: SourceClassification,
    *,
    base_root: Path = Path("."),
    index_path: Path | None = None,
    archive_url: str | None = None,
) -> dict[str, Any]:
    resolved_root = base_root.resolve()
    resolved_manifest_path = (
        manifest_path.resolve()
        if manifest_path.is_absolute()
        else resolved_root / manifest_path
    )
    resolved_archive_path = (
        archive_path.resolve()
        if archive_path.is_absolute()
        else resolved_root / archive_path
    )
    resolved_index_path = None
    if index_path is not None:
        resolved_index_path = (
            index_path.resolve()
            if index_path.is_absolute()
            else resolved_root / index_path
        )

    manifest_rows = read_manifest_csv(resolved_manifest_path)
    if not manifest_rows:
        raise ValueError(f"{resolved_manifest_path} is empty")

    congresses = {row.congress_no for row in manifest_rows}
    if len(congresses) != 1:
        raise ValueError(f"{resolved_manifest_path} must contain exactly one Congress")
    congress_no = next(iter(congresses))

    manifest_relpath = _normalize_relative_path(
        _path_relative_to_root(resolved_manifest_path, resolved_root)
    )
    manifest_bytes = resolved_manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    expected_types = {row.expected_type for row in manifest_rows if row.expected_type}
    missing_required_types = sorted(set(classification.required) - expected_types)
    if missing_required_types:
        raise ValueError(
            "Manifest is missing required expected_type values: "
            + ", ".join(missing_required_types)
        )
    candidate_classes = {row.candidate_class for row in manifest_rows if row.candidate_class}
    missing_required_candidates = sorted(
        set(classification.required_candidate_classes) - candidate_classes
    )
    if missing_required_candidates:
        raise ValueError(
            "Manifest is missing required candidate_class values: "
            + ", ".join(missing_required_candidates)
        )
    unclassified = sorted(expected_type for expected_type in expected_types if classification.category_for(expected_type) is None)
    if unclassified:
        raise ValueError(f"Manifest expected_type values are not fully classified: {', '.join(unclassified)}")

    logical_entries: list[BundleEntry] = []
    skipped_optional: list[SkippedOptionalEntry] = []
    required_failures: list[str] = []
    archive_members: dict[str, Path] = {}

    for row in sorted(manifest_rows, key=_manifest_sort_key):
        category = classification.category_for(
            row.expected_type, row.candidate_class
        )
        if category == "validation_only":
            continue
        if category is None:
            continue

        if row.status != "RETRIEVED" or not row.local_path:
            if category == "required":
                required_failures.append(f"{row.identifier}: {row.status or 'missing local path'}")
            else:
                skipped_optional.append(
                    SkippedOptionalEntry(
                        expected_type=row.expected_type,
                        identifier=row.identifier,
                        local_path=row.local_path,
                        reason=row.status or "missing local path",
                    )
                )
            continue

        row_relpath = _normalize_relative_path(row.local_path)
        resolved_path = base_root / row_relpath
        if not resolved_path.exists():
            if category == "required":
                required_failures.append(f"{row.identifier}: {resolved_path} is missing")
            else:
                skipped_optional.append(
                    SkippedOptionalEntry(
                        expected_type=row.expected_type,
                        identifier=row.identifier,
                        local_path=row.local_path,
                        reason="missing on disk",
                    )
                )
            continue

        actual_size, actual_sha256 = _tree_stats(resolved_path)
        expected_size = _coerce_int(row.size_bytes)
        if expected_size is None or not row.sha256:
            raise ValueError(f"{row.identifier} is missing manifest size/hash fields")
        if actual_size != expected_size or actual_sha256 != row.sha256:
            raise ValueError(
                f"{row.identifier} does not match manifest bytes: expected {expected_size}/{row.sha256}, "
                f"got {actual_size}/{actual_sha256}"
            )

        logical_entries.append(
            BundleEntry(
                category=category,
                expected_type=row.expected_type,
                identifier=row.identifier,
                local_path=row_relpath.as_posix(),
                path_type="file" if resolved_path.is_file() else "directory",
                sha256=row.sha256,
                size_bytes=expected_size,
            )
        )
        for member_path, member_source in _expanded_files(resolved_path, row_relpath):
            existing = archive_members.get(member_path)
            if existing is not None and existing != member_source:
                raise ValueError(f"Archive member collision for {member_path}")
            archive_members[member_path] = member_source

    if required_failures:
        failures = "\n".join(required_failures)
        raise ValueError(f"Required bundle inputs are missing or unavailable:\n{failures}")

    logical_entries.sort(key=lambda entry: (entry.local_path, entry.identifier, entry.expected_type))
    skipped_optional.sort(key=lambda entry: (entry.local_path, entry.identifier, entry.expected_type))

    metadata = {
        "classifications": classification.to_dict(),
        "congress_no": congress_no,
        "entries": [asdict(entry) for entry in logical_entries],
        "manifest": {
            "path": manifest_relpath.as_posix(),
            "sha256": manifest_sha256,
        },
        "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
        "skipped_optional": [asdict(entry) for entry in skipped_optional],
        "tool_version": SOURCE_BUNDLE_TOOL_VERSION,
    }

    byte_members: dict[str, bytes] = {
        BUNDLE_METADATA_NAME: _json_bytes(metadata),
        manifest_relpath.as_posix(): manifest_bytes,
    }

    collisions = set(byte_members) & set(archive_members)
    if collisions:
        raise ValueError(
            "Source paths collide with reserved bundle members: "
            + ", ".join(sorted(collisions))
        )

    expected_members = sorted(set(byte_members) | set(archive_members))
    resolved_archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_archive: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=resolved_archive_path.parent,
            prefix=f".{resolved_archive_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_archive = Path(handle.name)
        with zipfile.ZipFile(
            temporary_archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as bundle:
            for member_name in expected_members:
                if member_name in byte_members:
                    _write_zip_bytes(bundle, member_name, byte_members[member_name])
                else:
                    _write_zip_file(bundle, member_name, archive_members[member_name])

        _validate_built_archive(temporary_archive, expected_members)
        archive_size = temporary_archive.stat().st_size
        archive_sha256 = _file_sha256(temporary_archive)
        temporary_archive.chmod(0o644)
        os.replace(temporary_archive, resolved_archive_path)
        temporary_archive = None
    finally:
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)
    entry = {
        "archive": {
            "path": _portable_path(
                resolved_archive_path,
                relative_to=resolved_index_path.parent if resolved_index_path else base_root,
            ),
            "sha256": archive_sha256,
            "size_bytes": archive_size,
            "url": archive_url or "",
        },
        "classifications": classification.to_dict(),
        "congress_no": congress_no,
        "manifest": {
            "path": manifest_relpath.as_posix(),
            "sha256": manifest_sha256,
        },
        "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
        "tool_version": SOURCE_BUNDLE_TOOL_VERSION,
    }

    if resolved_index_path is not None:
        update_source_bundle_index(resolved_index_path, entry)
    return entry


def _validate_built_archive(archive_path: Path, expected_members: list[str]) -> None:
    with zipfile.ZipFile(archive_path) as bundle:
        member_names = bundle.namelist()
        if member_names != expected_members:
            raise ValueError(
                f"Temporary bundle member inventory differs from the build plan: "
                f"{member_names} != {expected_members}"
            )
        corrupt_member = bundle.testzip()
        if corrupt_member is not None:
            raise ValueError(
                f"Temporary bundle failed CRC validation at {corrupt_member}"
            )
        json.loads(bundle.read(BUNDLE_METADATA_NAME).decode("utf-8"))


def update_source_bundle_index(index_path: Path, bundle_entry: Mapping[str, Any]) -> None:
    bundles: list[dict[str, Any]]
    if index_path.exists():
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != SOURCE_BUNDLE_INDEX_VERSION:
            raise ValueError(f"{index_path} has an unexpected source-bundle index schema")
        bundles = [dict(item) for item in payload.get("bundles", [])]
    else:
        bundles = []

    bundles = [item for item in bundles if int(item["congress_no"]) != int(bundle_entry["congress_no"])]
    bundles.append(dict(bundle_entry))
    bundles.sort(key=lambda item: int(item["congress_no"]))

    payload = {
        "bundles": bundles,
        "schema_version": SOURCE_BUNDLE_INDEX_VERSION,
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_index: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=index_path.parent,
            prefix=f".{index_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_index = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        validated_payload = json.loads(temporary_index.read_text(encoding="utf-8"))
        if validated_payload != payload:
            raise ValueError("Temporary source-bundle index failed JSON validation")
        temporary_index.chmod(0o644)
        os.replace(temporary_index, index_path)
        temporary_index = None
        _fsync_directory(index_path.parent)
    finally:
        if temporary_index is not None:
            temporary_index.unlink(missing_ok=True)


def read_source_bundle_index(index_path: Path) -> dict[str, Any]:
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SOURCE_BUNDLE_INDEX_VERSION:
        raise ValueError(f"{index_path} has an unexpected source-bundle index schema")
    return payload


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def hydrate_source_bundle(
    archive_path: Path,
    destination_root: Path,
    *,
    expected_archive_sha256: str | None = None,
    expected_archive_size_bytes: int | None = None,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    if expected_archive_size_bytes is not None and archive_path.stat().st_size != expected_archive_size_bytes:
        raise ValueError(
            f"{archive_path} has size {archive_path.stat().st_size}, expected {expected_archive_size_bytes}"
        )
    if expected_archive_sha256 is not None:
        actual_archive_sha256 = _file_sha256(archive_path)
        if actual_archive_sha256 != expected_archive_sha256:
            raise ValueError(
                f"{archive_path} has SHA-256 {actual_archive_sha256}, expected {expected_archive_sha256}"
            )

    destination_root = Path(os.path.abspath(destination_root))
    _reject_symlink_components(destination_root)

    with zipfile.ZipFile(archive_path) as bundle:
        member_infos = bundle.infolist()
        member_names = [info.filename for info in member_infos]
        if len(member_names) != len(set(member_names)):
            raise ValueError(f"{archive_path} contains duplicate archive members")
        if BUNDLE_METADATA_NAME not in member_names:
            raise ValueError(f"{archive_path} does not contain {BUNDLE_METADATA_NAME}")

        metadata = json.loads(bundle.read(BUNDLE_METADATA_NAME).decode("utf-8"))
        if metadata.get("schema_version") != SOURCE_BUNDLE_SCHEMA_VERSION:
            raise ValueError(f"{archive_path} has an unexpected source-bundle schema")

        manifest_info = metadata["manifest"]
        manifest_name = _normalize_relative_path(manifest_info["path"]).as_posix()
        if manifest_name not in member_names:
            raise ValueError(f"{archive_path} is missing embedded manifest {manifest_name}")
        manifest_bytes = bundle.read(manifest_name)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_sha256 != manifest_info["sha256"]:
            raise ValueError(
                f"Embedded manifest hash mismatch for {manifest_name}: {manifest_sha256} != {manifest_info['sha256']}"
            )
        if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
            raise ValueError(
                f"{archive_path} contains manifest SHA-256 {manifest_sha256}, expected {expected_manifest_sha256}"
            )

        entries = [BundleEntry(**entry_data) for entry_data in metadata.get("entries", [])]
        _validate_archive_members(
            archive_path,
            member_infos,
            manifest_name=manifest_name,
            entries=entries,
        )

        destination_root.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(destination_root.parent)
        with tempfile.TemporaryDirectory(
            dir=destination_root.parent,
            prefix=f".{destination_root.name}.hydrate-",
        ) as temporary:
            staging_root = Path(temporary) / "payload"
            staging_root.mkdir()
            for info in member_infos:
                if info.filename == BUNDLE_METADATA_NAME:
                    continue
                normalized = _normalize_relative_path(info.filename)
                target = staging_root / Path(normalized.as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)

            _validate_staged_entries(staging_root, entries)
            _promote_staged_payload(staging_root, destination_root)

    return {
        "congress_no": metadata["congress_no"],
        "entries": len(metadata.get("entries", [])),
        "manifest_path": manifest_info["path"],
        "manifest_sha256": manifest_sha256,
    }


def hydrate_source_bundle_from_index(
    index_path: Path,
    congress_no: int,
    destination_root: Path,
) -> dict[str, Any]:
    payload = read_source_bundle_index(index_path)
    bundle_entry = next((entry for entry in payload["bundles"] if int(entry["congress_no"]) == congress_no), None)
    if bundle_entry is None:
        raise ValueError(f"{index_path} does not contain a bundle entry for Congress {congress_no}")

    archive_ref = bundle_entry["archive"]
    archive_path = archive_ref.get("path")
    if not archive_path:
        raise ValueError(f"{index_path} does not provide a local archive path for Congress {congress_no}")

    resolved_archive = (index_path.parent / archive_path).resolve()
    if not resolved_archive.exists():
        url = archive_ref.get("url") or ""
        if url:
            _download_source_bundle(
                url,
                resolved_archive,
                expected_sha256=archive_ref.get("sha256") or "",
                expected_size_bytes=_coerce_int(archive_ref.get("size_bytes")),
            )
        else:
            raise ValueError(f"{resolved_archive} is missing locally for Congress {congress_no}")

    return hydrate_source_bundle(
        resolved_archive,
        destination_root,
        expected_archive_sha256=archive_ref.get("sha256") or None,
        expected_archive_size_bytes=_coerce_int(archive_ref.get("size_bytes")),
        expected_manifest_sha256=bundle_entry["manifest"].get("sha256") or None,
    )


def _download_source_bundle(
    url: str,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int | None,
) -> None:
    """Download one checksummed bundle atomically to its indexed local path."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"Source bundle URL must use HTTPS: {url}")
    if len(expected_sha256) != 64:
        raise ValueError("Remote source bundles require an expected SHA-256")
    if expected_size_bytes is None or expected_size_bytes < 1:
        raise ValueError("Remote source bundles require a positive expected size")

    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(destination.parent)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.download-",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            digest = hashlib.sha256()
            received = 0
            request = Request(url, headers={"User-Agent": "Committee-Steward-source-bundle/1"})
            with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > expected_size_bytes:
                        raise ValueError(
                            f"Downloaded source bundle exceeds expected size {expected_size_bytes}"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if received != expected_size_bytes:
            raise ValueError(
                f"Downloaded source bundle has size {received}, expected {expected_size_bytes}"
            )
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"Downloaded source bundle has SHA-256 {actual_sha256}, expected {expected_sha256}"
            )
        temporary.chmod(0o644)
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_archive_members(
    archive_path: Path,
    member_infos: list[zipfile.ZipInfo],
    *,
    manifest_name: str,
    entries: list[BundleEntry],
) -> None:
    entry_paths = [
        (entry, _normalize_relative_path(entry.local_path)) for entry in entries
    ]
    for entry, _ in entry_paths:
        if entry.path_type not in {"file", "directory"}:
            raise ValueError(
                f"{archive_path} has invalid path_type {entry.path_type!r} "
                f"for {entry.identifier}"
            )

    for info in member_infos:
        member_name = _normalize_relative_path(info.filename)
        unix_mode = info.external_attr >> 16
        if info.is_dir():
            raise ValueError(
                f"{archive_path} contains unsupported directory member {info.filename}"
            )
        if stat.S_ISLNK(unix_mode):
            raise ValueError(
                f"{archive_path} contains unsupported symlink member {info.filename}"
            )
        if info.filename in {BUNDLE_METADATA_NAME, manifest_name}:
            continue

        declared = False
        for entry, entry_path in entry_paths:
            if entry.path_type == "file" and member_name == entry_path:
                declared = True
                break
            if (
                entry.path_type == "directory"
                and member_name != entry_path
                and member_name.is_relative_to(entry_path)
            ):
                declared = True
                break
        if not declared:
            raise ValueError(
                f"{archive_path} contains undeclared member {info.filename}"
            )


def _validate_staged_entries(
    staging_root: Path, entries: list[BundleEntry]
) -> None:
    for entry in entries:
        target_path = staging_root / Path(
            _normalize_relative_path(entry.local_path).as_posix()
        )
        try:
            actual_size, actual_sha256 = _tree_stats(target_path)
        except ValueError:
            actual_size, actual_sha256 = -1, "missing"
        if actual_size != entry.size_bytes or actual_sha256 != entry.sha256:
            raise ValueError(
                f"Checksum mismatch after hydrate for {entry.identifier}: "
                f"size {actual_size} != {entry.size_bytes} or SHA-256 "
                f"{actual_sha256} != {entry.sha256}"
            )


def _promote_staged_payload(staging_root: Path, destination_root: Path) -> None:
    _reject_symlink_components(destination_root)
    if not destination_root.exists():
        os.replace(staging_root, destination_root)
        return
    if not destination_root.is_dir():
        raise ValueError(f"Hydration destination is not a directory: {destination_root}")

    staged_files = sorted(path for path in staging_root.rglob("*") if path.is_file())
    _preflight_destination_merge(staging_root, destination_root, staged_files)
    for source_path in staged_files:
        relative_path = source_path.relative_to(staging_root)
        target_path = destination_root / relative_path
        _prepare_destination_parent(destination_root, relative_path.parent)
        if target_path.is_symlink():
            raise ValueError(
                f"Hydration destination contains symlink: {target_path}"
            )
        if target_path.exists() and not target_path.is_file():
            raise ValueError(
                f"Hydration destination target is not a file: {target_path}"
            )
        try:
            os.replace(source_path, target_path)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            _copy_across_filesystems(source_path, target_path)


def _copy_across_filesystems(source_path: Path, target_path: Path) -> None:
    """Copy to a sibling temporary file before atomically replacing the target."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target_path.parent,
            prefix=f".{target_path.name}.hydrate-",
            suffix=".tmp",
            delete=False,
        ) as destination:
            temporary_path = Path(destination.name)
            with source_path.open("rb") as source:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        temporary_path.chmod(stat.S_IMODE(source_path.stat().st_mode))
        os.replace(temporary_path, target_path)
        temporary_path = None
        _fsync_directory(target_path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _preflight_destination_merge(
    staging_root: Path, destination_root: Path, staged_files: list[Path]
) -> None:
    for source_path in staged_files:
        relative_path = source_path.relative_to(staging_root)
        current = destination_root
        for part in relative_path.parent.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(
                    f"Hydration destination contains symlink: {current}"
                )
            if current.exists() and not current.is_dir():
                raise ValueError(
                    f"Hydration destination component is not a directory: {current}"
                )
        target_path = destination_root / relative_path
        if target_path.is_symlink():
            raise ValueError(
                f"Hydration destination contains symlink: {target_path}"
            )
        if target_path.exists() and not target_path.is_file():
            raise ValueError(
                f"Hydration destination target is not a file: {target_path}"
            )


def _prepare_destination_parent(root: Path, relative_parent: Path) -> None:
    current = root
    for part in relative_parent.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Hydration destination contains symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise ValueError(
                    f"Hydration destination component is not a directory: {current}"
                )
        else:
            current.mkdir()


def _reject_symlink_components(path: Path) -> None:
    absolute_path = Path(os.path.abspath(path))
    current = Path(absolute_path.anchor)
    for part in absolute_path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Hydration destination contains symlink: {current}")


def _manifest_sort_key(row: ManifestRow) -> tuple[str, str, str, str]:
    return (row.local_path, row.identifier, row.expected_type, row.artifact_level)


def _normalize_type_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("Expected type names must be non-empty")
    return normalized


def _normalize_optional_name(value: str) -> str:
    return str(value or "").strip()


def _normalize_relative_path(value: str | Path) -> PurePosixPath:
    path_text = str(value)
    if not path_text:
        raise ValueError("Relative paths must be non-empty")
    if "\\" in path_text:
        raise ValueError(f"Backslash paths are not allowed in bundle members: {path_text}")
    normalized = PurePosixPath(path_text)
    if normalized.is_absolute():
        raise ValueError(f"Absolute paths are not allowed in bundle members: {path_text}")
    if any(part in ("", ".", "..") for part in normalized.parts):
        raise ValueError(f"Path traversal is not allowed in bundle members: {path_text}")
    return normalized


def _path_relative_to_root(path: Path, base_root: Path) -> Path:
    resolved_path = path.resolve()
    resolved_root = base_root.resolve()
    try:
        return resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{resolved_path} is outside the configured bundle root {resolved_root}") from exc


def _expanded_files(path: Path, archive_root: PurePosixPath) -> list[tuple[str, Path]]:
    if path.is_file():
        return [(archive_root.as_posix(), path)]

    files: list[tuple[str, Path]] = []
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative_child = child.relative_to(path).as_posix()
        child_archive = archive_root / _normalize_relative_path(relative_child)
        files.append((child_archive.as_posix(), child))
    return files


def _tree_stats(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total_size = 0
    if path.is_file():
        files = [path]
        base = path.parent
    elif path.is_dir():
        files = sorted(item for item in path.rglob("*") if item.is_file())
        base = path
    else:
        raise ValueError(f"{path} is not a file or directory")

    for item in files:
        rel = item.name if path.is_file() else item.relative_to(base).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                total_size += len(chunk)
                digest.update(chunk)
        digest.update(b"\0")
    return total_size, digest.hexdigest()


def _write_zip_bytes(bundle: zipfile.ZipFile, member_name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(member_name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    bundle.writestr(info, data, compresslevel=9)


def _write_zip_file(
    bundle: zipfile.ZipFile, member_name: str, source_path: Path
) -> None:
    """Stream a source file into a deterministic ZIP member."""
    info = zipfile.ZipInfo(member_name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    with source_path.open("rb") as source, bundle.open(
        info, "w", force_zip64=True
    ) as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _coerce_int(value: Any) -> int | None:
    if value in ("", None):
        return None
    return int(value)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_path(path: Path, *, relative_to: Path) -> str:
    if not path.is_absolute():
        path = (relative_to / path).resolve()
    try:
        return path.resolve().relative_to(relative_to.resolve()).as_posix()
    except ValueError:
        return Path(os.path.relpath(path.resolve(), relative_to.resolve())).as_posix()
