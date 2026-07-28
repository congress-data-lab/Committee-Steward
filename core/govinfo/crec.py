"""Safely prepare GovInfo CREC packages for the JSON event loader.

GovInfo package ZIPs are the authoritative downloaded artifacts.  Their HTML
members are extracted into ``data/crec/YYYY/CREC-YYYY-MM-DD`` and then converted
to derived JSON by an explicitly pinned ``unitedstates/congressional-record``
checkout.  Extraction alone does not manufacture JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


CREC_PACKAGE_RE = re.compile(r"CREC-(?P<year>\d{4})-\d{2}-\d{2}")
CREC_PROVENANCE_SCHEMA_VERSION = "crec_normalization_provenance_v1"
CREC_PARSER_REPOSITORY = "https://github.com/unitedstates/congressional-record"
DEFAULT_MAX_MEMBERS = 100_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024


class CrecArchiveError(ValueError):
    """Raised when a CREC package cannot be prepared without ambiguity."""


@dataclass(frozen=True)
class CrecExtractionResult:
    archive: Path
    destination: Path
    package_id: str
    status: str
    member_count: int
    total_bytes: int
    tree_sha256: str
    json_file_count: int


@dataclass(frozen=True)
class CrecJsonResult:
    package: Path
    status: str
    file_count: int
    total_bytes: int
    sha256: str


ParserRunner = Callable[[Path, Path], None]


def _package_id(archive: Path) -> tuple[str, str]:
    package_id = archive.stem
    match = CREC_PACKAGE_RE.fullmatch(package_id)
    if match is None:
        raise CrecArchiveError(
            f"CREC archive filename must be CREC-YYYY-MM-DD.zip: {archive}"
        )
    return package_id, match.group("year")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def _validated_members(
    archive: zipfile.ZipFile,
    package_id: str,
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> tuple[list[tuple[zipfile.ZipInfo, Path]], int]:
    members: list[tuple[zipfile.ZipInfo, Path]] = []
    seen: set[str] = set()
    total_bytes = 0

    for info in archive.infolist():
        name = info.filename
        if not name or "\x00" in name or "\\" in name or name.startswith("/"):
            raise CrecArchiveError(f"Unsafe ZIP member path: {name!r}")
        raw_parts = name.split("/")
        if raw_parts[-1] == "":
            raw_parts = raw_parts[:-1]
        if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
            raise CrecArchiveError(f"Unsafe ZIP member path: {name!r}")
        normalized = PurePosixPath(*raw_parts)
        if normalized.parts[0] != package_id:
            raise CrecArchiveError(
                f"ZIP member is outside expected package root {package_id}: {name!r}"
            )
        normalized_name = normalized.as_posix()
        if normalized_name in seen:
            raise CrecArchiveError(f"Duplicate ZIP member path: {normalized_name}")
        seen.add(normalized_name)
        if _is_symlink(info):
            raise CrecArchiveError(f"Refusing symlink ZIP member: {name!r}")
        unix_type = stat.S_IFMT(info.external_attr >> 16)
        if unix_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise CrecArchiveError(f"Refusing special ZIP member: {name!r}")
        if info.file_size < 0:
            raise CrecArchiveError(f"Invalid ZIP member size: {name!r}")
        total_bytes += info.file_size
        if total_bytes > max_uncompressed_bytes:
            raise CrecArchiveError(
                f"CREC archive exceeds {max_uncompressed_bytes} uncompressed bytes"
            )
        members.append((info, Path(*normalized.parts[1:])))

    if len(members) > max_members:
        raise CrecArchiveError(f"CREC archive exceeds {max_members} members")
    if not any(not info.is_dir() for info, _ in members):
        raise CrecArchiveError("CREC archive contains no files")
    return members, total_bytes


def _same_bytes(source: zipfile.ZipFile, info: zipfile.ZipInfo, path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != info.file_size:
        return False
    with source.open(info) as expected, path.open("rb") as observed:
        while True:
            expected_chunk = expected.read(1024 * 1024)
            observed_chunk = observed.read(1024 * 1024)
            if expected_chunk != observed_chunk:
                return False
            if not expected_chunk:
                return True


def _raw_tree_hash(
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


def _json_count(destination: Path) -> int:
    json_dir = destination / "json"
    return len(list(json_dir.glob("*.json"))) if json_dir.is_dir() else 0


def extract_crec_archive(
    archive_path: Path,
    *,
    root: Path = Path("."),
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> CrecExtractionResult:
    """Extract one package atomically, refusing traversal and conflicting trees.

    Re-running against a matching destination is a no-op.  Extra derived files,
    notably ``json/*.json``, are preserved and are not part of the raw-tree hash.
    """
    archive_path = archive_path.resolve()
    package_id, year = _package_id(archive_path)
    destination = (root / "data" / "crec" / year / package_id).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        source = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise CrecArchiveError(f"Invalid CREC ZIP {archive_path}: {exc}") from exc

    with source:
        members, total_bytes = _validated_members(
            source,
            package_id,
            max_members=max_members,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
        raw_hash = _raw_tree_hash(source, members)

        if destination.exists():
            if not destination.is_dir():
                raise CrecArchiveError(
                    f"CREC destination exists but is not a directory: {destination}"
                )
            for info, relative in members:
                target = destination / relative
                matches = target.is_dir() if info.is_dir() else _same_bytes(source, info, target)
                if not matches:
                    raise CrecArchiveError(
                        f"Existing CREC tree does not match {archive_path.name}: {target}"
                    )
            return CrecExtractionResult(
                archive=archive_path,
                destination=destination,
                package_id=package_id,
                status="already_extracted",
                member_count=sum(not info.is_dir() for info, _ in members),
                total_bytes=total_bytes,
                tree_sha256=raw_hash,
                json_file_count=_json_count(destination),
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
                with source.open(info) as input_handle, target.open("xb") as output_handle:
                    shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
                target.chmod(0o644)
            os.replace(staged_package, destination)
        except Exception:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise
        else:
            shutil.rmtree(temporary_root, ignore_errors=True)

    return CrecExtractionResult(
        archive=archive_path,
        destination=destination,
        package_id=package_id,
        status="extracted",
        member_count=sum(not info.is_dir() for info, _ in members),
        total_bytes=total_bytes,
        tree_sha256=raw_hash,
        json_file_count=0,
    )


def _json_tree_stats(directory: Path) -> tuple[int, int, str]:
    files = sorted(directory.glob("*.json"))
    if not files:
        raise CrecArchiveError(f"CREC parser produced no JSON files in {directory}")
    digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CrecArchiveError(f"Invalid CREC JSON {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise CrecArchiveError(f"CREC parser output must be a JSON object: {path}")
        if not isinstance(document.get("content"), list):
            raise CrecArchiveError(f"CREC parser output has no content list: {path}")
        size = path.stat().st_size
        total_bytes += size
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return len(files), total_bytes, digest.hexdigest()


def _file_stats(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _tree_stats(path: Path) -> tuple[int, int, str]:
    files = sorted(item for item in path.rglob("*") if item.is_file())
    digest = hashlib.sha256()
    size = 0
    for item in files:
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        digest.update(b"\0")
    return len(files), size, digest.hexdigest()


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise CrecArchiveError(f"CREC provenance path is outside the source root: {path}") from exc


def write_crec_provenance(
    congress_no: int,
    results: Sequence[tuple[CrecExtractionResult, CrecJsonResult]],
    *,
    root: Path = Path("."),
    parser_revision: str,
    destination: Path | None = None,
) -> Path:
    """Atomically merge raw-package and derived-tree hashes into a durable ledger."""
    if not re.fullmatch(r"[0-9a-f]{40}", parser_revision):
        raise CrecArchiveError("Parser revision must be a full 40-character Git SHA")
    root = root.resolve()
    destination = destination or (
        root / "data" / "manifests" / f"crec_provenance_{congress_no}.json"
    )
    destination = destination.resolve()
    existing_packages: dict[str, dict[str, object]] = {}
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CrecArchiveError(f"Invalid CREC provenance ledger {destination}: {exc}") from exc
        if (
            existing.get("schema_version") != CREC_PROVENANCE_SCHEMA_VERSION
            or existing.get("congress_no") != congress_no
            or existing.get("parser", {}).get("revision") != parser_revision
        ):
            raise CrecArchiveError(
                f"CREC provenance ledger does not match Congress/parser pin: {destination}"
            )
        existing_packages = {
            str(item["package_id"]): item for item in existing.get("packages", [])
        }

    for extraction, parsed in results:
        if extraction.package_id != parsed.package.name:
            raise CrecArchiveError(
                f"CREC extraction/JSON package mismatch: {extraction.package_id} / {parsed.package.name}"
            )
        raw_size, raw_sha256 = _file_stats(extraction.archive)
        tree_file_count, tree_size, tree_sha256 = _tree_stats(extraction.destination)
        existing_packages[extraction.package_id] = {
            "package_id": extraction.package_id,
            "raw_archive": {
                "path": _relative_to_root(extraction.archive, root),
                "size_bytes": raw_size,
                "sha256": raw_sha256,
            },
            "raw_tree": {
                "member_count": extraction.member_count,
                "size_bytes": extraction.total_bytes,
                "sha256": extraction.tree_sha256,
            },
            "normalized_tree": {
                "path": _relative_to_root(extraction.destination, root),
                "file_count": tree_file_count,
                "size_bytes": tree_size,
                "sha256": tree_sha256,
            },
            "json": {
                "file_count": parsed.file_count,
                "size_bytes": parsed.total_bytes,
                "sha256": parsed.sha256,
            },
        }

    payload = {
        "schema_version": CREC_PROVENANCE_SCHEMA_VERSION,
        "congress_no": congress_no,
        "parser": {
            "repository": CREC_PARSER_REPOSITORY,
            "revision": parser_revision,
        },
        "packages": [existing_packages[key] for key in sorted(existing_packages)],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, destination)
    return destination


def generate_crec_json(
    package: Path,
    *,
    parser_runner: ParserRunner,
) -> CrecJsonResult:
    """Generate and atomically publish validated JSON for an extracted package."""
    package = package.resolve()
    if not (package / "html").is_dir():
        raise CrecArchiveError(f"Extracted CREC package has no HTML directory: {package}")
    temporary = Path(tempfile.mkdtemp(prefix=".json.", dir=package))
    generated = temporary / "json"
    try:
        parser_runner(package, generated)
        file_count, total_bytes, digest = _json_tree_stats(generated)
        destination = package / "json"
        if destination.exists():
            existing_count, existing_bytes, existing_digest = _json_tree_stats(destination)
            if (existing_count, existing_bytes, existing_digest) != (
                file_count,
                total_bytes,
                digest,
            ):
                raise CrecArchiveError(
                    f"Existing CREC JSON does not match pinned parser output: {destination}"
                )
            status = "already_generated"
        else:
            for path in generated.glob("*.json"):
                path.chmod(0o644)
            os.replace(generated, destination)
            status = "generated"
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return CrecJsonResult(
        package=package,
        status=status,
        file_count=file_count,
        total_bytes=total_bytes,
        sha256=digest,
    )


_PARSER_SCRIPT = r"""
import json
import sys
from pathlib import Path

from congressionalrecord.govinfo.cr_parser import ParseCRDir, ParseCRFile

package = Path(sys.argv[1])
output = Path(sys.argv[2])
output.mkdir()
crdir = ParseCRDir(str(package))
for html_path in sorted((package / "html").glob("*.htm")):
    name = html_path.name
    if "-PgD" in name or "FrontMatter" in name or "-Pgnull" in name:
        continue
    parsed = ParseCRFile(str(html_path), crdir).crdoc
    target = output / (html_path.stem + ".json")
    target.write_text(
        json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
"""


def _verify_parser_checkout(parser_root: Path, expected_revision: str) -> None:
    """Require the exact commit and a clean tracked/staged/untracked worktree."""
    try:
        revision = subprocess.run(
            ["git", "-C", str(parser_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status_output = subprocess.run(
            [
                "git",
                "-C",
                str(parser_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CrecArchiveError(
            f"Cannot verify Congressional Record parser checkout {parser_root}: {exc}"
        ) from exc
    if revision != expected_revision:
        raise CrecArchiveError(
            f"Congressional Record parser revision is {revision}, expected {expected_revision}"
        )
    dirty_entries = [line for line in status_output.splitlines() if line]
    if dirty_entries:
        preview = ", ".join(dirty_entries[:5])
        if len(dirty_entries) > 5:
            preview += f", ... ({len(dirty_entries) - 5} more)"
        raise CrecArchiveError(
            "Congressional Record parser checkout must be clean, including "
            f"staged and untracked files: {preview}"
        )


def external_crec_parser_runner(
    parser_root: Path,
    *,
    expected_revision: str,
    python_executable: Path | None = None,
) -> ParserRunner:
    """Create a runner only after verifying the parser's exact Git revision."""
    parser_root = parser_root.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise CrecArchiveError("Parser revision must be a full 40-character Git SHA")
    _verify_parser_checkout(parser_root, expected_revision)
    if python_executable is None:
        candidates = (parser_root / ".venv/bin/python", parser_root / "venv/bin/python")
        python_executable = next((path for path in candidates if path.is_file()), None)
    if python_executable is None or not python_executable.is_file():
        raise CrecArchiveError(
            "Parser Python was not found; pass --crec-parser-python explicitly"
        )

    def run(package: Path, output: Path) -> None:
        # Recheck immediately before every parse so a checkout modified after
        # runner construction cannot execute code outside the asserted commit.
        _verify_parser_checkout(parser_root, expected_revision)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(parser_root)
        completed = subprocess.run(
            [str(python_executable), "-c", _PARSER_SCRIPT, str(package), str(output)],
            cwd=parser_root,
            env=environment,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise CrecArchiveError(
                f"Congressional Record parser failed for {package.name}: {detail}"
            )

    return run


def prepare_crec_archives(
    congress_no: int,
    *,
    root: Path = Path("."),
    parser_root: Path,
    parser_revision: str,
    parser_python: Path | None = None,
) -> list[tuple[CrecExtractionResult, CrecJsonResult]]:
    """Extract and parse every downloaded CREC ZIP for one Congress."""
    runner = external_crec_parser_runner(
        parser_root,
        expected_revision=parser_revision,
        python_executable=parser_python,
    )
    archives = sorted((root / "data" / "primary" / str(congress_no) / "crec").glob("*.zip"))
    if not archives:
        raise CrecArchiveError(f"No downloaded CREC ZIPs found for Congress {congress_no}")
    results: list[tuple[CrecExtractionResult, CrecJsonResult]] = []
    for archive in archives:
        extraction = extract_crec_archive(archive, root=root)
        parsed = generate_crec_json(extraction.destination, parser_runner=runner)
        results.append((extraction, parsed))
        write_crec_provenance(
            congress_no,
            [(extraction, parsed)],
            root=root,
            parser_revision=parser_revision,
        )
    return results


__all__ = [
    "CrecArchiveError",
    "CrecExtractionResult",
    "CrecJsonResult",
    "CREC_PARSER_REPOSITORY",
    "CREC_PROVENANCE_SCHEMA_VERSION",
    "external_crec_parser_runner",
    "extract_crec_archive",
    "generate_crec_json",
    "prepare_crec_archives",
    "write_crec_provenance",
]
