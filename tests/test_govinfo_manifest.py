import csv
import io
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import core.govinfo.manifest as manifest_module
from core.govinfo.client import GovInfoClient
from core.govinfo.manifest import (
    build_committee_manifest,
    classify_crec_title,
    classify_resolution_title,
    congress_date_range,
    retrieve_missing_packages,
    refresh_manifest_local_state,
    validate_manifest,
    write_manifest_csv,
)


class _FakeClient:
    def __init__(self) -> None:
        self.granule_calls: list[str] = []

    def iter_published(self, _start: str, _end: str, **kwargs):
        collection = kwargs["collection"]
        doc_class = kwargs.get("doc_class")
        if collection == "BILLS" and doc_class == "hres":
            yield {
                "packageId": "BILLS-115hres51eh",
                "title": "Electing Members to certain standing committees",
                "dateIssued": "2017-01-13",
                "collectionCode": "BILLS",
            }
        elif collection == "BILLS" and doc_class == "sres":
            yield {
                "packageId": "BILLS-115sres7ats",
                "title": "To constitute membership on certain committees",
                "dateIssued": "2017-01-05",
                "collectionCode": "BILLS",
            }
        elif collection == "CREC":
            yield {
                "packageId": "CREC-2017-07-18",
                "title": "Congressional Record",
                "dateIssued": "2017-07-18",
                "collectionCode": "CREC",
            }

    def iter_granules(self, package_id: str, **_kwargs):
        self.granule_calls.append(package_id)
        yield {
            "granuleId": "CREC-2017-07-18-pt1-PgH5943",
            "title": "RESIGNATION AS MEMBER OF THE COMMITTEE ON OVERSIGHT AND GOVERNMENT REFORM",
            "dateIssued": "2017-07-18",
        }

    def iter_search(self, _query: str, **_kwargs):
        return iter(())

    def download_package(self, _package_id: str, _rendition: str, _destination: Path):
        raise AssertionError("download should not be called")


def test_manifest_writes_use_unique_atomic_temporary_files(
    tmp_path: Path, monkeypatch
) -> None:
    rows = build_committee_manifest(
        _FakeClient(),
        115,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=False,
        include_directory=False,
    )
    destination = tmp_path / "manifest.csv"
    real_replace = manifest_module.os.replace
    barrier = threading.Barrier(2)
    sources: list[Path] = []
    sources_lock = threading.Lock()

    def synchronized_replace(source: Path, target: Path) -> None:
        with sources_lock:
            sources.append(Path(source))
        barrier.wait(timeout=5)
        real_replace(source, target)

    monkeypatch.setattr(manifest_module.os, "replace", synchronized_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(write_manifest_csv, destination, rows) for _ in range(2)
        ]
        for future in futures:
            future.result()

    assert len(set(sources)) == 2
    with destination.open(newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == len(rows)


def test_congress_date_range() -> None:
    assert tuple(str(value) for value in congress_date_range(115)) == ("2017-01-03", "2019-01-02")


def test_crec_title_classification_distinguishes_membership_and_role() -> None:
    assert classify_crec_title("RESIGNATION AS MEMBER OF COMMITTEE ON ETHICS") == "committee_removal"
    assert classify_crec_title("RESIGNATION AS CHAIRMAN OF COMMITTEE ON THE BUDGET") == "role_change_only"
    assert (
        classify_crec_title("APPOINTMENT OF MEMBERS TO PERMANENT SELECT COMMITTEE ON INTELLIGENCE")
        == "committee_appointment"
    )


def test_resolution_title_classification_is_membership_specific() -> None:
    assert (
        classify_resolution_title(
            "Electing Members to certain standing committees of the House"
        )
        == "committee_assignment"
    )
    assert (
        classify_resolution_title("Appointing managers for an impeachment trial")
        == "other"
    )


def test_manifest_is_stable_and_hashes_local_artifacts(tmp_path: Path) -> None:
    house = tmp_path / "data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml"
    senate = tmp_path / "data/resolutions/115th/bills/sres/BILLS-115sres7ats.xml"
    granule = tmp_path / "data/crec/2017/CREC-2017-07-18/json/CREC-2017-07-18-pt1-PgH5943.json"
    for path, content in ((house, "house"), (senate, "senate"), (granule, "record")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    client = _FakeClient()
    rows = build_committee_manifest(
        client,
        115,
        root=tmp_path,
        include_crec_granules=True,
        include_journals=False,
        include_directory=False,
    )

    assert [row.identifier for row in rows] == [
        "BILLS-115sres7ats",
        "BILLS-115hres51eh",
        "CREC-2017-07-18",
        "CREC-2017-07-18-pt1-PgH5943",
    ]
    by_id = {row.identifier: row for row in rows}
    assert by_id["BILLS-115hres51eh"].status == "RETRIEVED"
    assert by_id["BILLS-115hres51eh"].size_bytes == 5
    assert by_id["BILLS-115hres51eh"].sha256
    assert by_id["BILLS-115hres51eh"].candidate_class == "committee_assignment"
    assert by_id["BILLS-115sres7ats"].candidate_class == "committee_assignment"
    assert by_id["CREC-2017-07-18-pt1-PgH5943"].candidate_class == "committee_removal"
    assert by_id["CREC-2017-07-18"].status == "RETRIEVED"
    assert validate_manifest(rows, require_local=True) == []

    output = tmp_path / "manifest.csv"
    write_manifest_csv(output, rows)
    first = output.read_bytes()
    write_manifest_csv(output, rows)
    assert output.read_bytes() == first


def test_manifest_includes_normalized_directory_snapshot(tmp_path: Path) -> None:
    snapshot = (
        tmp_path
        / "data/congressional_directories/115th/committee_memberships_2018.json"
    )
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("[]\n", encoding="utf-8")

    rows = build_committee_manifest(
        _FakeClient(),
        115,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=False,
        include_directory=True,
    )

    normalized = [
        row for row in rows if row.expected_type == "directory_snapshot_normalized"
    ]
    assert len(normalized) == 1
    assert normalized[0].local_path.endswith("committee_memberships_2018.json")
    assert normalized[0].status == "RETRIEVED"


def test_manifest_includes_crec_normalization_provenance(tmp_path: Path) -> None:
    provenance = tmp_path / "data/manifests/crec_provenance_115.json"
    provenance.parent.mkdir(parents=True)
    provenance.write_text(
        '{"schema_version":"crec_normalization_provenance_v1"}\n',
        encoding="utf-8",
    )

    rows = build_committee_manifest(
        _FakeClient(),
        115,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=False,
        include_directory=False,
    )

    row = next(
        item
        for item in rows
        if item.expected_type == "crec_normalization_provenance"
    )
    assert Path(row.local_path) == provenance
    assert row.status == "RETRIEVED"
    assert row.sha256


def test_client_follows_next_page_and_cursor_without_duplicates() -> None:
    client = GovInfoClient("secret", base_url="https://example.test")
    responses = [
        {"packages": [{"packageId": "one"}], "nextPage": "https://example.test/next?offsetMark=a"},
        {"packages": [{"packageId": "two"}], "offsetMark": "b"},
        {"packages": [{"packageId": "three"}], "offsetMark": "b"},
    ]
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request(_method, path, *, params=None, body=None):
        assert body is None
        calls.append((path, dict(params or {})))
        return responses.pop(0)

    client._request_json = fake_request  # type: ignore[method-assign]
    rows = list(
        client.iter_published(
            "2017-01-03",
            "2019-01-02",
            collection="BILLS",
            congress=115,
            doc_class="hres",
        )
    )
    assert [row["packageId"] for row in rows] == ["one", "two", "three"]
    assert calls[1][0].startswith("https://example.test/next")
    assert calls[2][1]["offsetMark"] == "b"


def test_search_follows_offset_cursor() -> None:
    client = GovInfoClient("secret", base_url="https://example.test")
    responses = [
        {"results": [{"packageId": "one"}], "offsetMark": "next"},
        {"results": [{"packageId": "two"}], "offsetMark": "next"},
    ]
    bodies = []

    def fake_request(_method, _path, *, params=None, body=None):
        assert params is None
        bodies.append(dict(body))
        return responses.pop(0)

    client._request_json = fake_request  # type: ignore[method-assign]
    rows = list(client.iter_search("collection:(HJOURNAL) congress:115"))
    assert [row["packageId"] for row in rows] == ["one", "two"]
    assert bodies[0]["offsetMark"] == "*"
    assert bodies[1]["offsetMark"] == "next"


def test_download_package_retries_truncated_zip_before_publish(
    tmp_path: Path, monkeypatch
) -> None:
    complete = io.BytesIO()
    with zipfile.ZipFile(complete, "w") as archive:
        archive.writestr("CREC-2024-03-05/mods.xml", "<mods />")
    complete_bytes = complete.getvalue()
    responses = [complete_bytes[:-12], complete_bytes]
    calls = 0

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(_request, *, timeout):
        nonlocal calls
        assert timeout == 60
        calls += 1
        return Response(responses.pop(0))

    monkeypatch.setattr("core.govinfo.client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("core.govinfo.client.time.sleep", lambda _delay: None)
    destination = tmp_path / "CREC-2024-03-05.zip"

    GovInfoClient("secret", max_retries=1).download_package(
        "CREC-2024-03-05", "zip", destination
    )

    assert calls == 2
    assert zipfile.is_zipfile(destination)
    assert not destination.with_suffix(".zip.tmp").exists()


def test_retrieve_missing_packages_can_limit_download_source_types(
    tmp_path: Path,
) -> None:
    class DownloadClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.downloads: list[tuple[str, str]] = []

        def download_package(
            self, package_id: str, rendition: str, destination: Path
        ) -> None:
            self.downloads.append((package_id, rendition))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(package_id, encoding="utf-8")

    client = DownloadClient()
    rows = build_committee_manifest(
        client,
        115,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=False,
        include_directory=False,
    )

    refreshed = retrieve_missing_packages(
        client, rows, expected_types={"house_resolution"}
    )

    assert client.downloads == [("BILLS-115hres51eh", "xml")]
    by_type = {row.expected_type: row for row in refreshed}
    assert by_type["house_resolution"].status == "RETRIEVED"
    assert by_type["senate_resolution"].status == "MISSING_LOCAL"
    assert by_type["congressional_record_package"].status == "MISSING_LOCAL"


def test_retrieve_missing_packages_enforces_download_cap(tmp_path: Path) -> None:
    rows = build_committee_manifest(
        _FakeClient(),
        115,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=False,
        include_directory=False,
    )

    with pytest.raises(ValueError, match="configured maximum"):
        retrieve_missing_packages(
            _FakeClient(),
            rows,
            expected_types={"house_resolution", "senate_resolution"},
            max_downloads=1,
        )


def test_retrieve_missing_packages_can_download_a_bounded_batch(
    tmp_path: Path,
) -> None:
    class DownloadClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.downloads: list[str] = []

        def download_package(
            self, package_id: str, rendition: str, destination: Path
        ) -> None:
            self.downloads.append(package_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(package_id, encoding="utf-8")

    client = DownloadClient()
    rows = build_committee_manifest(
        client,
        115,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=False,
        include_directory=False,
    )

    refreshed = retrieve_missing_packages(
        client,
        rows,
        expected_types={"house_resolution", "senate_resolution"},
        max_downloads=1,
        download_batch_size=1,
    )

    eligible = [
        row
        for row in rows
        if row.expected_type in {"house_resolution", "senate_resolution"}
    ]
    assert client.downloads == [eligible[0].package_id]
    statuses = {row.expected_type: row.status for row in refreshed}
    assert statuses[eligible[0].expected_type] == "RETRIEVED"
    assert statuses[eligible[1].expected_type] == "MISSING_LOCAL"


def test_retrieve_missing_packages_supports_bounded_parallel_downloads(
    tmp_path: Path,
) -> None:
    class DownloadClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.downloads: list[str] = []

        def download_package(
            self, package_id: str, rendition: str, destination: Path
        ) -> None:
            self.downloads.append(package_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(package_id, encoding="utf-8")

    client = DownloadClient()
    rows = build_committee_manifest(
        client,
        115,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=False,
        include_directory=False,
    )
    eligible = [
        row
        for row in rows
        if row.expected_type in {"house_resolution", "senate_resolution"}
    ]

    refreshed = retrieve_missing_packages(
        client,
        rows,
        expected_types={"house_resolution", "senate_resolution"},
        max_downloads=2,
        download_batch_size=2,
        download_workers=2,
    )

    assert set(client.downloads) == {row.package_id for row in eligible}
    by_identifier = {row.identifier: row for row in refreshed}
    assert all(by_identifier[row.identifier].status == "RETRIEVED" for row in eligible)


def test_refresh_manifest_rehashes_paths_and_replaces_normalized_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "data/resolutions/115th/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("current", encoding="utf-8")
    snapshot = tmp_path / "data/congressional_directories/115th/snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("[]\n", encoding="utf-8")
    row = next(
        item
        for item in build_committee_manifest(
            _FakeClient(),
            115,
            root=tmp_path,
            include_crec_granules=False,
            include_journals=False,
            include_directory=False,
        )
        if item.expected_type == "house_resolution"
    )
    row = row.__class__(
        **{
            **row.__dict__,
            "local_path": source.relative_to(tmp_path).as_posix(),
            "size_bytes": "",
            "sha256": "",
            "status": "MISSING_LOCAL",
        }
    )

    refreshed = refresh_manifest_local_state(
        [row], root=tmp_path, expected_types={"house_resolution"}
    )

    by_type = {item.expected_type: item for item in refreshed}
    assert by_type["house_resolution"].status == "RETRIEVED"
    assert by_type["house_resolution"].sha256
    assert by_type["directory_snapshot_normalized"].status == "RETRIEVED"


def test_refresh_manifest_refuses_recursive_directory_rehash(tmp_path: Path) -> None:
    row = next(
        item
        for item in build_committee_manifest(
            _FakeClient(),
            115,
            root=tmp_path,
            include_crec_granules=False,
            include_journals=False,
            include_directory=False,
        )
        if item.expected_type == "congressional_record_package"
    )
    (tmp_path / row.local_path).mkdir(parents=True)

    with pytest.raises(ValueError, match="Refusing to recursively rehash"):
        refresh_manifest_local_state(
            [row],
            root=tmp_path,
            expected_types={"congressional_record_package"},
        )


def test_refresh_manifest_transitions_crec_zip_to_normalized_tree(
    tmp_path: Path,
) -> None:
    package_id = "CREC-2017-07-18"
    archive = tmp_path / "data/primary/115/crec" / f"{package_id}.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"raw zip placeholder")
    row = next(
        item
        for item in build_committee_manifest(
            _FakeClient(),
            115,
            root=tmp_path,
            include_crec_granules=False,
            include_journals=False,
            include_directory=False,
        )
        if item.expected_type == "congressional_record_package"
    )
    normalized = tmp_path / "data/crec/2017" / package_id / "json"
    normalized.mkdir(parents=True)
    (normalized / "record.json").write_text(
        '{"content":[]}\n', encoding="utf-8"
    )
    archive.unlink()

    refreshed = refresh_manifest_local_state(
        [row],
        root=tmp_path,
        expected_types={"congressional_record_package"},
        rehash_retrieved=True,
        rehash_directories=True,
    )

    assert len(refreshed) == 1
    assert Path(refreshed[0].local_path) == normalized.parent
    assert refreshed[0].status == "RETRIEVED"
    assert refreshed[0].sha256
