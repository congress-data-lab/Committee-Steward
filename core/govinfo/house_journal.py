"""Safely prepare official GovInfo House Journal package ZIPs."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


HOUSE_JOURNAL_PACKAGE_RE = re.compile(r"GPO-HJOURNAL-(?P<year>\d{4})")
DEFAULT_MAX_MEMBERS = 100_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024


class HouseJournalArchiveError(ValueError):
    """Raised when a House Journal archive cannot be prepared safely."""


@dataclass(frozen=True)
class HouseJournalExtractionResult:
    archive: Path
    destination: Path
    package_id: str
    year: int
    status: str
    member_count: int
    total_bytes: int
    sha256: str


def house_journal_archive_path(
    congress_no: int, year: int, *, root: Path = Path(".")
) -> Path:
    """Return the canonical authoritative GovInfo package path."""
    return (
        root
        / "data"
        / "primary"
        / str(congress_no)
        / "hjournal"
        / f"GPO-HJOURNAL-{year}.zip"
    )


def _package_id(archive: Path) -> tuple[str, int]:
    package_id = archive.stem
    match = HOUSE_JOURNAL_PACKAGE_RE.fullmatch(package_id)
    if match is None:
        raise HouseJournalArchiveError(
            "House Journal archive filename must be "
            f"GPO-HJOURNAL-YYYY.zip: {archive}"
        )
    return package_id, int(match.group("year"))


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def _validated_members(
    source: zipfile.ZipFile,
    package_id: str,
    year: int,
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> tuple[list[tuple[zipfile.ZipInfo, Path]], int]:
    members: list[tuple[zipfile.ZipInfo, Path]] = []
    seen: set[str] = set()
    total_bytes = 0
    main_pdf = re.compile(
        rf"pdf/{re.escape(package_id)}-2-\d+\.pdf", re.IGNORECASE
    )
    has_main_pdf = False

    for info in source.infolist():
        name = info.filename
        if not name or "\x00" in name or "\\" in name or name.startswith("/"):
            raise HouseJournalArchiveError(f"Unsafe ZIP member path: {name!r}")
        parts = name.split("/")
        if parts[-1] == "":
            parts = parts[:-1]
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise HouseJournalArchiveError(f"Unsafe ZIP member path: {name!r}")
        normalized = PurePosixPath(*parts)
        if normalized.parts[0] != package_id:
            raise HouseJournalArchiveError(
                f"ZIP member is outside expected package root {package_id}: {name!r}"
            )
        normalized_name = normalized.as_posix()
        if normalized_name in seen:
            raise HouseJournalArchiveError(
                f"Duplicate ZIP member path: {normalized_name}"
            )
        seen.add(normalized_name)
        if _is_symlink(info):
            raise HouseJournalArchiveError(f"Refusing symlink ZIP member: {name!r}")
        unix_type = stat.S_IFMT(info.external_attr >> 16)
        if unix_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise HouseJournalArchiveError(f"Refusing special ZIP member: {name!r}")
        if info.file_size < 0:
            raise HouseJournalArchiveError(f"Invalid ZIP member size: {name!r}")
        total_bytes += info.file_size
        if total_bytes > max_uncompressed_bytes:
            raise HouseJournalArchiveError(
                "House Journal archive exceeds "
                f"{max_uncompressed_bytes} uncompressed bytes"
            )
        relative = Path(*normalized.parts[1:])
        if not info.is_dir() and main_pdf.fullmatch(relative.as_posix()):
            has_main_pdf = True
        members.append((info, relative))

    if len(members) > max_members:
        raise HouseJournalArchiveError(
            f"House Journal archive exceeds {max_members} members"
        )
    if not any(not info.is_dir() for info, _ in members):
        raise HouseJournalArchiveError("House Journal archive contains no files")
    if not has_main_pdf:
        raise HouseJournalArchiveError(
            f"House Journal archive has no main-volume PDF for {year}"
        )
    return members, total_bytes


def _tree_hash(
    source: zipfile.ZipFile,
    members: Sequence[tuple[zipfile.ZipInfo, Path]],
) -> str:
    digest = hashlib.sha256()
    for info, relative in sorted(members, key=lambda item: item[1].as_posix()):
        if info.is_dir():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with source.open(info) as member:
            while chunk := member.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _same_bytes(source: zipfile.ZipFile, info: zipfile.ZipInfo, path: Path) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != info.file_size:
        return False
    with source.open(info) as expected, path.open("rb") as observed:
        while True:
            expected_chunk = expected.read(1024 * 1024)
            observed_chunk = observed.read(1024 * 1024)
            if expected_chunk != observed_chunk:
                return False
            if not expected_chunk:
                return True


def _expected_tree(
    members: Sequence[tuple[zipfile.ZipInfo, Path]],
) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for info, relative in members:
        if not relative.parts:
            continue
        if info.is_dir():
            directories.add(relative.as_posix())
        else:
            files.add(relative.as_posix())
        parent = relative.parent
        while parent.parts:
            directories.add(parent.as_posix())
            parent = parent.parent
    return files, directories


def _verify_existing_tree(
    source: zipfile.ZipFile,
    members: Sequence[tuple[zipfile.ZipInfo, Path]],
    destination: Path,
) -> None:
    expected_files, expected_directories = _expected_tree(members)
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for item in destination.rglob("*"):
        relative = item.relative_to(destination).as_posix()
        if item.is_symlink():
            raise HouseJournalArchiveError(
                f"Existing House Journal tree contains a symlink: {item}"
            )
        if item.is_dir():
            actual_directories.add(relative)
        elif item.is_file():
            actual_files.add(relative)
        else:
            raise HouseJournalArchiveError(
                f"Existing House Journal tree contains a special file: {item}"
            )
    if actual_files != expected_files or actual_directories != expected_directories:
        raise HouseJournalArchiveError(
            f"Existing House Journal tree does not match {destination.name}"
        )
    for info, relative in members:
        if not info.is_dir() and not _same_bytes(source, info, destination / relative):
            raise HouseJournalArchiveError(
                f"Existing House Journal tree does not match {destination / relative}"
            )


def extract_house_journal_archive(
    archive_path: Path,
    *,
    root: Path = Path("."),
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> HouseJournalExtractionResult:
    """Extract one official package atomically without replacing conflicts."""
    archive_path = archive_path.resolve()
    package_id, year = _package_id(archive_path)
    destination = (root / "data" / "journals" / package_id).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        source = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HouseJournalArchiveError(
            f"Invalid House Journal ZIP {archive_path}: {exc}"
        ) from exc

    with source:
        members, total_bytes = _validated_members(
            source,
            package_id,
            year,
            max_members=max_members,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
        digest = _tree_hash(source, members)
        member_count = sum(not info.is_dir() for info, _ in members)

        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise HouseJournalArchiveError(
                    "House Journal destination exists but is not a directory: "
                    f"{destination}"
                )
            _verify_existing_tree(source, members, destination)
            return HouseJournalExtractionResult(
                archive=archive_path,
                destination=destination,
                package_id=package_id,
                year=year,
                status="already_extracted",
                member_count=member_count,
                total_bytes=total_bytes,
                sha256=digest,
            )

        temporary_root = Path(
            tempfile.mkdtemp(prefix=f".{package_id}.", dir=destination.parent)
        )
        staged_package = temporary_root / package_id
        try:
            staged_package.mkdir(mode=0o755)
            for info, relative in members:
                target = staged_package / relative
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with source.open(info) as input_handle, target.open(
                    "xb"
                ) as output_handle:
                    shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
                target.chmod(0o644)
            os.replace(staged_package, destination)
        except Exception:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise
        else:
            shutil.rmtree(temporary_root, ignore_errors=True)

    return HouseJournalExtractionResult(
        archive=archive_path,
        destination=destination,
        package_id=package_id,
        year=year,
        status="extracted",
        member_count=member_count,
        total_bytes=total_bytes,
        sha256=digest,
    )


def prepare_house_journal_archives(
    congress_no: int,
    *,
    years: Iterable[int] | None = None,
    root: Path = Path("."),
) -> list[HouseJournalExtractionResult]:
    """Prepare selected, already-downloaded journals for one Congress."""
    if congress_no < 1:
        raise HouseJournalArchiveError("Congress number must be positive")
    first_year = 1787 + (2 * congress_no)
    allowed_years = {first_year, first_year + 1}
    selected_years = sorted(set(years if years is not None else allowed_years))
    if not selected_years:
        raise HouseJournalArchiveError("At least one House Journal year is required")

    results: list[HouseJournalExtractionResult] = []
    for year in selected_years:
        if year not in allowed_years:
            raise HouseJournalArchiveError(
                f"House Journal year {year} does not belong to Congress {congress_no}"
            )
        archive = house_journal_archive_path(congress_no, year, root=root)
        if not archive.is_file():
            raise HouseJournalArchiveError(
                f"Authoritative House Journal ZIP is missing: {archive}"
            )
        results.append(extract_house_journal_archive(archive, root=root))
    return results
