from __future__ import annotations

import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from core.govinfo.house_journal import (
    HouseJournalArchiveError,
    prepare_house_journal_archives,
)
from core.govinfo.manifest import (
    build_committee_manifest,
    refresh_house_journal_local_state,
    retrieve_missing_packages,
)


class _EmptyClient:
    def iter_published(self, _start: str, _end: str, **_kwargs):
        return iter(())

    def iter_granules(self, _package_id: str, **_kwargs):
        return iter(())

    def iter_search(self, _query: str, **_kwargs):
        return iter(())

    def download_package(
        self, _package_id: str, _rendition: str, _destination: Path
    ) -> None:
        raise AssertionError("download should not be called")


def _archive_path(root: Path, congress_no: int, year: int) -> Path:
    return (
        root
        / "data"
        / "primary"
        / str(congress_no)
        / "hjournal"
        / f"GPO-HJOURNAL-{year}.zip"
    )


def _write_archive(
    path: Path,
    year: int,
    *,
    member_name: str | None = None,
    symlink: bool = False,
) -> None:
    package_id = f"GPO-HJOURNAL-{year}"
    path.parent.mkdir(parents=True, exist_ok=True)
    name = member_name or f"{package_id}/pdf/{package_id}-2-1.pdf"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if symlink:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
        else:
            archive.writestr(name, b"journal-pdf")
            archive.writestr(f"{package_id}/mods.xml", b"<mods />")


def test_prepare_house_journal_extracts_atomically_and_resumes(tmp_path: Path) -> None:
    archive = _archive_path(tmp_path, 117, 2022)
    _write_archive(archive, 2022)

    [first] = prepare_house_journal_archives(
        117, years=(2022,), root=tmp_path
    )

    destination = tmp_path / "data/journals/GPO-HJOURNAL-2022"
    assert first.status == "extracted"
    assert first.archive == archive.resolve()
    assert first.destination == destination.resolve()
    assert first.member_count == 2
    assert first.sha256
    assert archive.is_file()
    assert (
        destination / "pdf/GPO-HJOURNAL-2022-2-1.pdf"
    ).read_bytes() == b"journal-pdf"
    assert not list(destination.parent.glob(".GPO-HJOURNAL-2022.*"))

    [second] = prepare_house_journal_archives(
        117, years=(2022,), root=tmp_path
    )
    assert second.status == "already_extracted"
    assert second.sha256 == first.sha256


@pytest.mark.parametrize(
    ("member_name", "symlink", "message"),
    [
        ("../escape.txt", False, "Unsafe ZIP member path"),
        ("GPO-HJOURNAL-2021/file.txt", False, "outside expected package root"),
        ("GPO-HJOURNAL-2022/link", True, "symlink ZIP member"),
    ],
)
def test_prepare_house_journal_rejects_unsafe_archives(
    tmp_path: Path,
    member_name: str,
    symlink: bool,
    message: str,
) -> None:
    archive = _archive_path(tmp_path, 117, 2022)
    _write_archive(archive, 2022, member_name=member_name, symlink=symlink)

    with pytest.raises(HouseJournalArchiveError, match=message):
        prepare_house_journal_archives(117, years=(2022,), root=tmp_path)

    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "data/journals/GPO-HJOURNAL-2022").exists()


def test_prepare_house_journal_rejects_conflicting_existing_tree(
    tmp_path: Path,
) -> None:
    archive = _archive_path(tmp_path, 117, 2022)
    _write_archive(archive, 2022)
    prepare_house_journal_archives(117, years=(2022,), root=tmp_path)
    target = (
        tmp_path
        / "data/journals/GPO-HJOURNAL-2022/pdf/GPO-HJOURNAL-2022-2-1.pdf"
    )
    target.write_bytes(b"different")

    with pytest.raises(HouseJournalArchiveError, match="does not match"):
        prepare_house_journal_archives(117, years=(2022,), root=tmp_path)


def test_prepare_house_journal_requires_year_in_congress(tmp_path: Path) -> None:
    archive = _archive_path(tmp_path, 117, 2023)
    _write_archive(archive, 2023)

    with pytest.raises(HouseJournalArchiveError, match="does not belong"):
        prepare_house_journal_archives(117, years=(2023,), root=tmp_path)


def test_manifest_preserves_raw_and_extracted_house_journal_provenance(
    tmp_path: Path,
) -> None:
    archive = _archive_path(tmp_path, 117, 2022)
    _write_archive(archive, 2022)
    rows_before = build_committee_manifest(
        _EmptyClient(),
        117,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=True,
        include_directory=False,
    )
    before_by_type = {
        row.expected_type: row
        for row in rows_before
        if row.package_id == "GPO-HJOURNAL-2022"
    }
    assert before_by_type["house_journal_package"].status == "RETRIEVED"
    assert before_by_type["house_journal"].status == "MISSING_LOCAL"

    prepare_house_journal_archives(117, years=(2022,), root=tmp_path)
    rows = refresh_house_journal_local_state(rows_before, root=tmp_path)
    package_rows = [row for row in rows if row.package_id == "GPO-HJOURNAL-2022"]
    by_type = {row.expected_type: row for row in package_rows}

    assert set(by_type) == {"house_journal", "house_journal_package"}
    raw = by_type["house_journal_package"]
    extracted = by_type["house_journal"]
    assert raw.artifact_level == "package"
    assert raw.local_path.endswith(
        "data/primary/117/hjournal/GPO-HJOURNAL-2022.zip"
    )
    assert raw.status == "RETRIEVED"
    assert raw.sha256
    assert extracted.artifact_level == "derived_tree"
    assert extracted.local_path.endswith("data/journals/GPO-HJOURNAL-2022")
    assert extracted.status == "RETRIEVED"
    assert extracted.sha256
    assert extracted.sha256 != raw.sha256


def test_house_journal_download_targets_authoritative_primary_zip(
    tmp_path: Path,
) -> None:
    class _DownloadClient(_EmptyClient):
        def __init__(self) -> None:
            self.downloads: list[tuple[str, str, Path]] = []

        def download_package(
            self, package_id: str, rendition: str, destination: Path
        ) -> None:
            self.downloads.append((package_id, rendition, destination))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"zip placeholder")

    client = _DownloadClient()
    rows = build_committee_manifest(
        client,
        117,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=True,
        include_directory=False,
    )

    refreshed = retrieve_missing_packages(
        client,
        rows,
        expected_types={"house_journal_package"},
        max_downloads=1,
        download_batch_size=1,
    )

    [(package_id, rendition, destination)] = client.downloads
    assert package_id == "GPO-HJOURNAL-2021"
    assert rendition == "zip"
    assert destination == _archive_path(tmp_path, 117, 2021)
    downloaded = next(
        row
        for row in refreshed
        if row.expected_type == "house_journal_package"
        and row.package_id == package_id
    )
    assert downloaded.status == "RETRIEVED"


def test_manifest_cli_exposes_explicit_house_journal_prepare_option() -> None:
    root = Path(__file__).resolve().parent.parent
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/build_govinfo_committee_manifest.py"),
            "--help",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--prepare-house-journal-year" in completed.stdout
    assert "--complete-crec" in completed.stdout
