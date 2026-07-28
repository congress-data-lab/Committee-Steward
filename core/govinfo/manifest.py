"""Build a stable, auditable primary-source manifest from GovInfo metadata."""

from __future__ import annotations

import csv
import hashlib
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from core.govinfo.house_journal import house_journal_archive_path


RULESET_ID = "govinfo_manifest_v1"

MANIFEST_FIELDS = [
    "ruleset_id",
    "congress_no",
    "collection_code",
    "chamber",
    "artifact_level",
    "identifier",
    "package_id",
    "granule_id",
    "expected_type",
    "candidate_class",
    "title",
    "date_issued",
    "last_modified",
    "source_url_or_api_call",
    "details_url",
    "local_path",
    "size_bytes",
    "sha256",
    "status",
    "error_text",
]


class ManifestClient(Protocol):
    def iter_published(self, start_date: str, end_date: str, **kwargs: Any): ...

    def iter_granules(self, package_id: str, **kwargs: Any): ...

    def iter_search(self, query: str, **kwargs: Any): ...

    def download_package(self, package_id: str, rendition: str, destination: Path): ...


@dataclass(frozen=True)
class ManifestRow:
    ruleset_id: str
    congress_no: int
    collection_code: str
    chamber: str
    artifact_level: str
    identifier: str
    package_id: str
    granule_id: str
    expected_type: str
    candidate_class: str
    title: str
    date_issued: str
    last_modified: str
    source_url_or_api_call: str
    details_url: str
    local_path: str
    size_bytes: int | str
    sha256: str
    status: str
    error_text: str


@dataclass(frozen=True)
class _SourceSpec:
    collection: str
    expected_type: str
    chamber: str = ""
    doc_class: str | None = None
    discovery: str = "published"


SOURCE_SPECS = (
    _SourceSpec("BILLS", "house_resolution", "H", "hres"),
    _SourceSpec("BILLS", "senate_resolution", "S", "sres"),
    _SourceSpec("CREC", "congressional_record_package"),
    _SourceSpec("CDIR", "directory_snapshot", discovery="search"),
)


def congress_date_range(congress_no: int) -> tuple[date, date]:
    if congress_no < 1:
        raise ValueError("Congress number must be positive")
    start_year = 1787 + (2 * congress_no)
    return date(start_year, 1, 3), date(start_year + 2, 1, 2)


def classify_crec_title(title: str) -> str:
    normalized = re.sub(r"\s+", " ", title.upper()).strip()
    if re.search(r"\bRESIGNATIONS? AS (?:A )?MEMBER", normalized):
        return "committee_removal"
    if "RESIGNATION FROM THE HOUSE" in normalized or "RESIGNATION FROM THE SENATE" in normalized:
        return "chamber_exit"
    if re.search(r"\b(APPOINTMENT|ELECTION) OF MEMBERS? TO .+COMMITTEE", normalized):
        return "committee_appointment"
    if re.search(r"\bELECTING MEMBERS? TO .+COMMITTEE", normalized):
        return "committee_appointment"
    if "DEATH OF" in normalized or "PASSING OF" in normalized:
        return "member_death"
    if "ELECTION OF SPEAKER" in normalized:
        return "speaker_election"
    if "RESIGNATION AS CHAIR" in normalized or "RESIGNATION AS CHAIRMAN" in normalized:
        return "role_change_only"
    return "other"


def classify_resolution_title(title: str) -> str:
    """Classify resolution titles that can change committee membership."""
    normalized = re.sub(r"\s+", " ", title.upper()).strip()
    patterns = (
        r"\bELECTING (?:A |AN |CERTAIN |MEMBERS? ).*\bCOMMITTEES?\b",
        r"\bTO CONSTITUTE .*\bMEMBERSHIP (?:ON|OF) .*\bCOMMITTEES?\b",
        r"\bAPPOINT(?:ING|MENT OF) .*\bMEMBERS? .*\bCOMMITTEES?\b",
        r"\bREMOVING .*\bMEMBER FROM .*\bCOMMITTEES?\b",
    )
    return (
        "committee_assignment"
        if any(re.search(pattern, normalized) for pattern in patterns)
        else "other"
    )


def _resolution_group(package_id: str) -> str | None:
    match = re.match(r"(BILLS-\d+(?:hres|sres)\d+)", package_id, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _propagate_resolution_candidate_classes(
    rows: list[ManifestRow],
) -> list[ManifestRow]:
    candidate_groups = {
        group
        for row in rows
        if row.expected_type in {"house_resolution", "senate_resolution"}
        and row.candidate_class == "committee_assignment"
        if (group := _resolution_group(row.package_id)) is not None
    }
    return [
        replace(row, candidate_class="committee_assignment")
        if _resolution_group(row.package_id) in candidate_groups
        else row
        for row in rows
    ]


def _granule_chamber(record: dict[str, Any]) -> str:
    for key in ("docClass", "granuleClass", "category"):
        value = str(record.get(key) or "").upper()
        if "HOUSE" in value:
            return "H"
        if "SENATE" in value:
            return "S"
    granule_id = str(record.get("granuleId") or "")
    page_match = re.search(r"-Pg([HSE])\d", granule_id)
    if page_match:
        return {"H": "H", "S": "S", "E": "H"}[page_match.group(1)]
    return ""


def _first(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None:
            return str(value)
    return ""


def _tree_stats(path: Path) -> tuple[int | str, str, str]:
    if not path.exists():
        return "", "", "MISSING_LOCAL"
    digest = hashlib.sha256()
    total_size = 0
    files = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        rel = item.name if path.is_file() else item.relative_to(path).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                total_size += len(chunk)
                digest.update(chunk)
        digest.update(b"\0")
    return total_size, digest.hexdigest(), "RETRIEVED"


def _local_path(
    root: Path,
    congress_no: int,
    collection: str,
    package_id: str,
    granule_id: str = "",
    doc_class: str | None = None,
) -> Path:
    if collection == "BILLS" and doc_class:
        return root / "data" / "resolutions" / f"{congress_no}th" / "bills" / doc_class / f"{package_id}.xml"
    if collection == "CREC":
        year_match = re.match(r"CREC-(\d{4})-", package_id)
        year = year_match.group(1) if year_match else "unknown"
        base = root / "data" / "crec" / year / package_id
        if granule_id:
            return base / "json" / f"{granule_id}.json"
        if base.exists():
            return base
        return root / "data" / "primary" / str(congress_no) / "crec" / f"{package_id}.zip"
    if collection == "CDIR":
        return root / "data" / "primary" / str(congress_no) / "cdir" / f"{package_id}.zip"
    suffix = ".pdf" if collection == "HJOURNAL" else ".json"
    return root / "data" / "primary" / str(congress_no) / collection.lower() / f"{package_id}{suffix}"


def _local_directory_snapshot_rows(
    root: Path, congress_no: int
) -> list[ManifestRow]:
    directory = root / "data" / "congressional_directories" / f"{congress_no}th"
    if not directory.exists():
        return []
    rows: list[ManifestRow] = []
    for path in sorted(directory.glob("*.json")):
        size, digest, status = _tree_stats(path)
        rows.append(
            ManifestRow(
                ruleset_id=RULESET_ID,
                congress_no=congress_no,
                collection_code="CDIR-DERIVED",
                chamber="",
                artifact_level="file",
                identifier=f"CDIR-DERIVED-{congress_no}-{path.stem}",
                package_id="",
                granule_id="",
                expected_type="directory_snapshot_normalized",
                candidate_class="validation_reference",
                title="Normalized Congressional Directory committee membership snapshot",
                date_issued="",
                last_modified="",
                source_url_or_api_call="Derived from the cited Congressional Directory package",
                details_url="",
                local_path=path.relative_to(root).as_posix(),
                size_bytes=size,
                sha256=digest,
                status=status,
                error_text="" if status == "RETRIEVED" else "Normalized snapshot is missing",
            )
        )
    return rows


def _local_crec_provenance_rows(
    root: Path, congress_no: int
) -> list[ManifestRow]:
    path = root / "data" / "manifests" / f"crec_provenance_{congress_no}.json"
    if not path.is_file():
        return []
    size, digest, status = _tree_stats(path)
    return [
        ManifestRow(
            ruleset_id=RULESET_ID,
            congress_no=congress_no,
            collection_code="CREC-DERIVED",
            chamber="",
            artifact_level="file",
            identifier=f"CREC-PROVENANCE-{congress_no}",
            package_id="",
            granule_id="",
            expected_type="crec_normalization_provenance",
            candidate_class="provenance",
            title="Congressional Record raw-package and normalization provenance",
            date_issued="",
            last_modified="",
            source_url_or_api_call="Derived from GovInfo package ZIPs using the pinned parser revision recorded in this file",
            details_url="https://github.com/unitedstates/congressional-record",
            local_path=path.as_posix(),
            size_bytes=size,
            sha256=digest,
            status=status,
            error_text="",
        )
    ]


def _api_call(
    start_date: date,
    end_date: date,
    congress_no: int,
    spec: _SourceSpec,
) -> str:
    if spec.discovery == "search":
        return f"POST /search collection:({spec.collection}) congress:{congress_no} resultLevel=package"
    params = [f"collection={spec.collection}", f"congress={congress_no}", "pageSize=1000", "offsetMark=*"]
    if spec.doc_class:
        params.append(f"docClass={spec.doc_class}")
    return f"GET /published/{start_date}/{end_date}?" + "&".join(params)


def _package_row(
    root: Path,
    congress_no: int,
    spec: _SourceSpec,
    record: dict[str, Any],
    api_call: str,
) -> ManifestRow | None:
    package_id = _first(record, "packageId", "packageid")
    if not package_id:
        return None
    if spec.collection == "CREC" and not re.fullmatch(r"CREC-\d{4}-\d{2}-\d{2}", package_id):
        return None
    path = _local_path(root, congress_no, spec.collection, package_id, doc_class=spec.doc_class)
    size, digest, status = _tree_stats(path)
    return ManifestRow(
        ruleset_id=RULESET_ID,
        congress_no=congress_no,
        collection_code=_first(record, "collectionCode") or spec.collection,
        chamber=spec.chamber,
        artifact_level="package",
        identifier=package_id,
        package_id=package_id,
        granule_id="",
        expected_type=spec.expected_type,
        candidate_class=(
            classify_resolution_title(_first(record, "title", "Title"))
            if spec.collection == "BILLS"
            else ""
        ),
        title=_first(record, "title", "Title"),
        date_issued=_first(record, "dateIssued", "date_issued"),
        last_modified=_first(record, "lastModified", "last_modified"),
        source_url_or_api_call=api_call,
        details_url=_first(record, "detailsLink") or f"https://www.govinfo.gov/app/details/{package_id}",
        local_path=path.as_posix(),
        size_bytes=size,
        sha256=digest,
        status=status,
        error_text="" if status == "RETRIEVED" else "Artifact not present at canonical local path",
    )


def _granule_row(
    root: Path,
    congress_no: int,
    package_id: str,
    record: dict[str, Any],
) -> ManifestRow | None:
    granule_id = _first(record, "granuleId", "granuleid")
    if not granule_id:
        return None
    path = _local_path(root, congress_no, "CREC", package_id, granule_id)
    size, digest, status = _tree_stats(path)
    title = _first(record, "title", "Title")
    return ManifestRow(
        ruleset_id=RULESET_ID,
        congress_no=congress_no,
        collection_code="CREC",
        chamber=_granule_chamber(record),
        artifact_level="granule",
        identifier=granule_id,
        package_id=package_id,
        granule_id=granule_id,
        expected_type="congressional_record_granule",
        candidate_class=classify_crec_title(title),
        title=title,
        date_issued=_first(record, "dateIssued", "date_issued"),
        last_modified=_first(record, "lastModified", "last_modified"),
        source_url_or_api_call=f"GET /packages/{package_id}/granules?offsetMark=*&pageSize=1000",
        details_url=_first(record, "detailsLink")
        or f"https://www.govinfo.gov/app/details/{package_id}/{granule_id}",
        local_path=path.as_posix(),
        size_bytes=size,
        sha256=digest,
        status=status,
        error_text="" if status == "RETRIEVED" else "Artifact not present at canonical local path",
    )


def _local_journal_row(
    *,
    congress_no: int,
    chamber: str,
    expected_type: str,
    identifier: str,
    path: Path,
    date_issued: str,
    artifact_level: str = "package",
    title: str | None = None,
    source_url_or_api_call: str | None = None,
    missing_error: str | None = None,
) -> ManifestRow:
    size, digest, status = _tree_stats(path)
    return ManifestRow(
        ruleset_id=RULESET_ID,
        congress_no=congress_no,
        collection_code="HJOURNAL" if chamber == "H" else "SJOURNAL",
        chamber=chamber,
        artifact_level=artifact_level,
        identifier=identifier,
        package_id=identifier,
        granule_id="",
        expected_type=expected_type,
        candidate_class="",
        title=title or ("House Journal" if chamber == "H" else "Senate Journal"),
        date_issued=date_issued,
        last_modified="",
        source_url_or_api_call=(
            source_url_or_api_call
            or f"https://www.govinfo.gov/content/pkg/{identifier}"
        ),
        details_url=f"https://www.govinfo.gov/app/details/{identifier}",
        local_path=path.as_posix(),
        size_bytes=size,
        sha256=digest,
        status=status,
        error_text=(
            ""
            if status == "RETRIEVED"
            else missing_error
            or "Required canonical journal package is missing locally"
        ),
    )


def _local_journal_rows(root: Path, congress_no: int) -> list[ManifestRow]:
    start_date, _ = congress_date_range(congress_no)
    rows: list[ManifestRow] = []
    for year in (start_date.year, start_date.year + 1):
        identifier = f"GPO-HJOURNAL-{year}"
        raw_archive = house_journal_archive_path(congress_no, year, root=root)
        rows.append(
            _local_journal_row(
                congress_no=congress_no,
                chamber="H",
                expected_type="house_journal_package",
                identifier=identifier,
                path=raw_archive,
                date_issued=f"{year}-12-31",
                title="House Journal official GovInfo package ZIP",
                source_url_or_api_call=f"GET /packages/{identifier}/zip",
                missing_error="Authoritative House Journal ZIP is missing locally",
            )
        )
        rows.append(
            _local_journal_row(
                congress_no=congress_no,
                chamber="H",
                expected_type="house_journal",
                identifier=identifier,
                path=root / "data" / "journals" / identifier,
                date_issued=f"{year}-12-31",
                artifact_level="derived_tree",
                title="Extracted House Journal package tree",
                source_url_or_api_call=f"Derived from {raw_archive.as_posix()}",
                missing_error="Extracted House Journal tree is missing locally",
            )
        )

    senate_dir = root / "data" / "journals" / f"GPO-SJOURNAL-{congress_no}"
    senate_pdfs = sorted(senate_dir.glob("*.pdf")) if senate_dir.exists() else []
    if senate_pdfs:
        for pdf in senate_pdfs:
            rows.append(
                _local_journal_row(
                    congress_no=congress_no,
                    chamber="S",
                    expected_type="senate_journal",
                    identifier=pdf.stem,
                    path=pdf,
                    date_issued="",
                )
            )
    else:
        rows.append(
            _local_journal_row(
                congress_no=congress_no,
                chamber="S",
                expected_type="senate_journal",
                identifier=f"GPO-SJOURNAL-{congress_no}",
                path=senate_dir,
                date_issued="",
            )
        )
    return rows


def build_committee_manifest(
    client: ManifestClient,
    congress_no: int,
    *,
    root: Path = Path("."),
    include_crec_granules: bool = False,
    include_journals: bool = True,
    include_directory: bool = True,
) -> list[ManifestRow]:
    start_date, end_date = congress_date_range(congress_no)
    rows: list[ManifestRow] = []
    seen: set[tuple[str, str, str]] = set()

    for spec in SOURCE_SPECS:
        if spec.collection == "CDIR" and not include_directory:
            continue
        api_call = _api_call(start_date, end_date, congress_no, spec)
        if spec.discovery == "search":
            packages = client.iter_search(
                f"collection:({spec.collection}) congress:{congress_no}",
                page_size=1000,
                result_level="package",
                sort_field="publishdate",
                sort_order="ASC",
            )
        else:
            packages = client.iter_published(
                start_date.isoformat(),
                end_date.isoformat(),
                collection=spec.collection,
                congress=congress_no,
                doc_class=spec.doc_class,
                page_size=1000,
            )
        source_count = 0
        for record in packages:
            source_count += 1
            package_row = _package_row(root, congress_no, spec, record, api_call)
            if package_row is None:
                continue
            key = (package_row.expected_type, package_row.package_id, "")
            if key not in seen:
                seen.add(key)
                rows.append(package_row)

            if include_crec_granules and spec.collection == "CREC":
                for granule in client.iter_granules(package_row.package_id, page_size=1000):
                    granule_row = _granule_row(root, congress_no, package_row.package_id, granule)
                    if granule_row is None:
                        continue
                    granule_key = (granule_row.expected_type, granule_row.package_id, granule_row.granule_id)
                    if granule_key not in seen:
                        seen.add(granule_key)
                        rows.append(granule_row)

        if source_count == 0:
            rows.append(
                ManifestRow(
                    ruleset_id=RULESET_ID,
                    congress_no=congress_no,
                    collection_code=spec.collection,
                    chamber=spec.chamber,
                    artifact_level="collection",
                    identifier=f"{spec.collection}-{congress_no}-DISCOVERY",
                    package_id="",
                    granule_id="",
                    expected_type=spec.expected_type,
                    candidate_class="",
                    title="",
                    date_issued="",
                    last_modified="",
                    source_url_or_api_call=api_call,
                    details_url="",
                    local_path="",
                    size_bytes="",
                    sha256="",
                    status="DISCOVERY_FAILURE",
                    error_text="Required source class returned zero GovInfo records",
                )
            )

    if include_journals:
        for journal_row in _local_journal_rows(root, congress_no):
            key = (journal_row.expected_type, journal_row.package_id, "")
            if key not in seen:
                seen.add(key)
                rows.append(journal_row)

    if include_directory:
        for snapshot_row in _local_directory_snapshot_rows(root, congress_no):
            key = (snapshot_row.expected_type, snapshot_row.identifier, "")
            if key not in seen:
                seen.add(key)
                rows.append(snapshot_row)

    rows.extend(_local_crec_provenance_rows(root, congress_no))

    rows = _propagate_resolution_candidate_classes(rows)
    return sorted(
        rows,
        key=lambda row: (
            row.date_issued,
            row.collection_code,
            0 if row.artifact_level == "package" else 1,
            row.expected_type,
            row.package_id,
            row.granule_id,
        ),
    )


def validate_manifest(rows: list[ManifestRow], *, require_local: bool) -> list[ManifestRow]:
    if not require_local:
        return []
    return [row for row in rows if row.status != "RETRIEVED"]


def refresh_house_journal_local_state(
    rows: list[ManifestRow], *, root: Path = Path(".")
) -> list[ManifestRow]:
    """Refresh authoritative and extracted House Journal provenance rows."""
    if not rows:
        return []
    congresses = {row.congress_no for row in rows}
    if len(congresses) != 1:
        raise ValueError("A refreshed manifest must contain exactly one Congress")
    congress_no = next(iter(congresses))
    house_types = {"house_journal", "house_journal_package"}
    refreshed = [
        row
        for row in rows
        if not (row.chamber == "H" and row.expected_type in house_types)
    ]
    refreshed.extend(
        row for row in _local_journal_rows(root, congress_no) if row.chamber == "H"
    )
    return sorted(
        refreshed,
        key=lambda row: (
            row.date_issued,
            row.collection_code,
            0 if row.artifact_level == "package" else 1,
            row.expected_type,
            row.package_id,
            row.granule_id,
            row.identifier,
        ),
    )


def retrieve_missing_packages(
    client: ManifestClient,
    rows: list[ManifestRow],
    *,
    expected_types: set[str] | None = None,
    candidate_classes: set[str] | None = None,
    max_downloads: int | None = None,
    download_batch_size: int | None = None,
    download_workers: int = 1,
) -> list[ManifestRow]:
    """Retrieve missing package renditions, optionally limited by source type."""
    renditions = {
        "house_resolution": "xml",
        "senate_resolution": "xml",
        "congressional_record_package": "zip",
        "directory_snapshot": "zip",
        "house_journal_package": "zip",
    }
    eligible_indexes = [
        index
        for index, row in enumerate(rows)
        if (expected_types is None or row.expected_type in expected_types)
        and (candidate_classes is None or row.candidate_class in candidate_classes)
        and row.status == "MISSING_LOCAL"
        and row.artifact_level == "package"
        and row.expected_type in renditions
    ]
    if download_batch_size is not None:
        if download_batch_size < 1:
            raise ValueError("download_batch_size must be positive")
        eligible_indexes = eligible_indexes[:download_batch_size]
    if not 1 <= download_workers <= 4:
        raise ValueError("download_workers must be between 1 and 4")
    if max_downloads is not None and len(eligible_indexes) > max_downloads:
        raise ValueError(
            f"Refusing {len(eligible_indexes)} downloads; configured maximum is {max_downloads}"
        )
    def download(index: int) -> tuple[int, ManifestRow]:
        row = rows[index]
        rendition = renditions.get(row.expected_type)
        if rendition is None:
            return index, row
        destination = Path(row.local_path)
        try:
            client.download_package(row.package_id, rendition, destination)
            size, digest, status = _tree_stats(destination)
            return index, replace(
                row, size_bytes=size, sha256=digest, status=status, error_text=""
            )
        except Exception as exc:
            return index, replace(
                row, status="RETRIEVAL_FAILURE", error_text=str(exc)[:500]
            )

    replacements: dict[int, ManifestRow] = {}
    if download_workers == 1:
        for index in eligible_indexes:
            replaced_index, replaced_row = download(index)
            replacements[replaced_index] = replaced_row
    else:
        with ThreadPoolExecutor(max_workers=download_workers) as executor:
            futures = [executor.submit(download, index) for index in eligible_indexes]
            for future in as_completed(futures):
                replaced_index, replaced_row = future.result()
                replacements[replaced_index] = replaced_row
    return [replacements.get(index, row) for index, row in enumerate(rows)]


def refresh_manifest_local_state(
    rows: list[ManifestRow],
    *,
    root: Path = Path("."),
    expected_types: set[str] | None = None,
    rehash_retrieved: bool = False,
    rehash_directories: bool = False,
    identifiers: set[str] | None = None,
) -> list[ManifestRow]:
    """Rehash selected local file classes and refresh derived directory rows."""
    if not rows:
        return []
    congresses = {row.congress_no for row in rows}
    if len(congresses) != 1:
        raise ValueError("A refreshed manifest must contain exactly one Congress")
    congress_no = next(iter(congresses))

    refreshed: list[ManifestRow] = []
    for row in rows:
        if row.expected_type in {
            "directory_snapshot_normalized",
            "crec_normalization_provenance",
        }:
            continue
        if identifiers is not None and row.identifier not in identifiers:
            refreshed.append(row)
            continue
        if expected_types is not None and row.expected_type not in expected_types:
            refreshed.append(row)
            continue
        if row.status == "RETRIEVED" and not rehash_retrieved:
            refreshed.append(row)
            continue
        if not row.local_path:
            refreshed.append(row)
            continue
        local_path = root / row.local_path
        local_path_text = row.local_path
        if row.expected_type == "congressional_record_package" and row.package_id:
            canonical = _local_path(
                root,
                congress_no,
                "CREC",
                row.package_id,
            )
            if Path(row.local_path).is_absolute():
                local_path_text = canonical.resolve().as_posix()
            else:
                local_path_text = canonical.resolve().relative_to(root.resolve()).as_posix()
            local_path = canonical
        if local_path.is_dir() and not rehash_directories:
            raise ValueError(
                f"Refusing to recursively rehash directory input without a dedicated resource run: {local_path}"
            )
        if local_path.exists():
            size, digest, status = _tree_stats(local_path)
            refreshed.append(
                replace(
                    row,
                    local_path=local_path_text,
                    size_bytes=size,
                    sha256=digest,
                    status=status,
                    error_text="",
                )
            )
        else:
            refreshed.append(
                replace(
                    row,
                    local_path=local_path_text,
                    size_bytes="",
                    sha256="",
                    status="MISSING_LOCAL",
                    error_text="Artifact not present at canonical local path",
                )
            )

    refreshed.extend(_local_directory_snapshot_rows(root, congress_no))
    refreshed.extend(_local_crec_provenance_rows(root, congress_no))
    refreshed = _propagate_resolution_candidate_classes(
        [
            replace(row, candidate_class=classify_resolution_title(row.title))
            if row.expected_type in {"house_resolution", "senate_resolution"}
            else row
            for row in refreshed
        ]
    )
    return sorted(
        refreshed,
        key=lambda row: (
            row.date_issued,
            row.collection_code,
            0 if row.artifact_level == "package" else 1,
            row.expected_type,
            row.package_id,
            row.granule_id,
            row.identifier,
        ),
    )


def write_manifest_csv(path: Path, rows: list[ManifestRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
