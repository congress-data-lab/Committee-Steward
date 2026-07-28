import io
import json
import zipfile
from pathlib import Path

import pytest

from core.govinfo.bundle import (
    BUNDLE_METADATA_NAME,
    SOURCE_BUNDLE_INDEX_VERSION,
    SOURCE_BUNDLE_SCHEMA_VERSION,
    SourceClassification,
    build_source_bundle,
    hydrate_source_bundle,
    hydrate_source_bundle_from_index,
    load_source_classification,
    read_manifest_csv,
    update_source_bundle_index,
    verify_required_manifest_inputs,
)
from core.govinfo.manifest import ManifestRow, write_manifest_csv


def _manifest_row(
    *,
    congress_no: int,
    expected_type: str,
    identifier: str,
    local_path: str,
    size_bytes: int | str,
    sha256: str,
    status: str = "RETRIEVED",
) -> ManifestRow:
    return ManifestRow(
        ruleset_id="govinfo_manifest_v1",
        congress_no=congress_no,
        collection_code="BILLS",
        chamber="H",
        artifact_level="package",
        identifier=identifier,
        package_id=identifier,
        granule_id="",
        expected_type=expected_type,
        candidate_class="",
        title=identifier,
        date_issued="2017-01-03",
        last_modified="",
        source_url_or_api_call="GET /published",
        details_url=f"https://example.test/{identifier}",
        local_path=local_path,
        size_bytes=size_bytes,
        sha256=sha256,
        status=status,
        error_text="" if status == "RETRIEVED" else "missing",
    )


def _tree_stats(path: Path) -> tuple[int, str]:
    import hashlib

    digest = hashlib.sha256()
    total_size = 0
    files = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        rel = item.name if path.is_file() else item.relative_to(path).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        chunk = item.read_bytes()
        total_size += len(chunk)
        digest.update(chunk)
        digest.update(b"\0")
    return total_size, digest.hexdigest()


def test_release_classification_bundles_all_retrieved_resolutions() -> None:
    classification = load_source_classification(
        config_path=Path("config/source-classification.json")
    )

    assert classification.category_for("house_resolution") == "optional"
    assert classification.category_for("senate_resolution") == "optional"
    assert classification.category_for("congressional_record_package") == "required"
    assert classification.category_for("house_journal") == "validation_only"
    assert classification.category_for("crec_normalization_provenance") == "optional"
    assert (
        classification.category_for("house_resolution", "committee_assignment")
        == "required"
    )


def test_build_source_bundle_is_deterministic_and_sorted(tmp_path: Path) -> None:
    senate = tmp_path / "data/resolutions/115th/bills/sres/BILLS-115sres7ats.xml"
    house = tmp_path / "data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml"
    senate.parent.mkdir(parents=True, exist_ok=True)
    house.parent.mkdir(parents=True, exist_ok=True)
    senate.write_text("senate", encoding="utf-8")
    house.write_text("house", encoding="utf-8")

    senate_size, senate_sha = _tree_stats(senate)
    house_size, house_sha = _tree_stats(house)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres51eh",
                local_path="data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml",
                size_bytes=house_size,
                sha256=house_sha,
            ),
            _manifest_row(
                congress_no=115,
                expected_type="senate_resolution",
                identifier="BILLS-115sres7ats",
                local_path="data/resolutions/115th/bills/sres/BILLS-115sres7ats.xml",
                size_bytes=senate_size,
                sha256=senate_sha,
            ),
        ],
    )

    classification = SourceClassification(required=("senate_resolution", "house_resolution"))
    first_archive = tmp_path / "bundles/first.zip"
    second_archive = tmp_path / "bundles/second.zip"
    build_source_bundle(manifest, first_archive, classification, base_root=tmp_path)
    build_source_bundle(manifest, second_archive, classification, base_root=tmp_path)

    assert first_archive.read_bytes() == second_archive.read_bytes()
    with zipfile.ZipFile(first_archive) as bundle:
        assert bundle.namelist() == [
            BUNDLE_METADATA_NAME,
            "data/manifests/manifest_115.csv",
            "data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml",
            "data/resolutions/115th/bills/sres/BILLS-115sres7ats.xml",
        ]


@pytest.mark.parametrize("failure_point", ["write", "validate"])
def test_build_failure_preserves_existing_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import core.govinfo.bundle as bundle_module

    source = tmp_path / "data/resolutions/115th/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres1ih",
                local_path=source.relative_to(tmp_path).as_posix(),
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )
    archive = tmp_path / "bundles/source-bundle-115.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"existing verified archive")

    def fail_build(*args, **kwargs) -> None:
        raise RuntimeError(f"simulated archive {failure_point} failure")

    monkeypatch.setattr(
        bundle_module,
        "_write_zip_file" if failure_point == "write" else "_validate_built_archive",
        fail_build,
    )

    with pytest.raises(RuntimeError, match=f"simulated archive {failure_point} failure"):
        build_source_bundle(
            manifest,
            archive,
            SourceClassification(required=("house_resolution",)),
            base_root=tmp_path,
        )

    assert archive.read_bytes() == b"existing verified archive"
    assert list(archive.parent.glob(f".{archive.name}.*.tmp")) == []


@pytest.mark.parametrize("failure_point", ["write", "replace"])
def test_index_failure_preserves_existing_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import core.govinfo.bundle as bundle_module

    index = tmp_path / "manifests/source-bundles.json"
    index.parent.mkdir(parents=True)
    existing_payload = {
        "bundles": [{"congress_no": 114, "archive": {"path": "old.zip"}}],
        "schema_version": SOURCE_BUNDLE_INDEX_VERSION,
    }
    existing_bytes = (
        json.dumps(existing_payload, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    index.write_bytes(existing_bytes)

    def fail(*args, **kwargs) -> None:
        raise RuntimeError(f"simulated index {failure_point} failure")

    monkeypatch.setattr(
        bundle_module.json if failure_point == "write" else bundle_module.os,
        "dump" if failure_point == "write" else "replace",
        fail,
    )

    with pytest.raises(RuntimeError, match=f"simulated index {failure_point} failure"):
        update_source_bundle_index(
            index,
            {
                "archive": {"path": "new.zip"},
                "congress_no": 115,
                "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
            },
        )

    assert index.read_bytes() == existing_bytes
    assert list(index.parent.glob(f".{index.name}.*.tmp")) == []


def test_loader_ready_bundle_excludes_non_loader_crec_and_journal_files(
    tmp_path: Path,
) -> None:
    from scripts.build_source_bundle import build_loader_ready_source_bundle

    crec_package = tmp_path / "data/crec/2023/CREC-2023-01-04"
    crec_json = crec_package / "json/CREC-2023-01-04-pt1-PgH17.json"
    crec_json.parent.mkdir(parents=True)
    crec_json.write_text('{"content": []}', encoding="utf-8")
    (crec_package / "pdf").mkdir()
    (crec_package / "pdf/CREC-2023-01-04.pdf").write_bytes(b"large pdf")
    (crec_package / "html").mkdir()
    (crec_package / "html/CREC-2023-01-04.htm").write_text(
        "<html></html>", encoding="utf-8"
    )
    (crec_package / "mods.xml").write_text("<mods/>", encoding="utf-8")

    house = tmp_path / "data/resolutions/118th/bills/hres/BILLS-118hres1eh.xml"
    senate = tmp_path / "data/resolutions/118th/bills/sres/BILLS-118sres1ats.xml"
    directory = (
        tmp_path
        / "data/congressional_directories/118th/118th_committee_memberships_output.json"
    )
    provenance = tmp_path / "data/manifests/crec_provenance_118.json"
    journal_zip = tmp_path / "data/primary/118/hjournal/GPO-HJOURNAL-2023.zip"
    for path, content in (
        (house, "<bill/>"),
        (senate, "<bill/>"),
        (directory, "{}"),
        (provenance, "{}"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    journal_zip.parent.mkdir(parents=True)
    journal_zip.write_bytes(b"journal pdf archive")

    paths_by_type = {
        "congressional_record_package": crec_package,
        "house_resolution": house,
        "senate_resolution": senate,
        "directory_snapshot_normalized": directory,
        "crec_normalization_provenance": provenance,
        "house_journal_package": journal_zip,
    }
    rows = []
    for expected_type, path in paths_by_type.items():
        size_bytes, sha256 = _tree_stats(path)
        rows.append(
            _manifest_row(
                congress_no=118,
                expected_type=expected_type,
                identifier=path.stem,
                local_path=path.relative_to(tmp_path).as_posix(),
                size_bytes=size_bytes,
                sha256=sha256,
            )
        )
    manifest = tmp_path / "data/manifests/manifest_118.csv"
    write_manifest_csv(manifest, rows)

    archive = tmp_path / "bundles/source-bundle-118.zip"
    build_loader_ready_source_bundle(
        manifest.relative_to(tmp_path),
        archive,
        SourceClassification(
            required=(
                "congressional_record_package",
                "directory_snapshot_normalized",
            ),
            optional=(
                "crec_normalization_provenance",
                "house_journal_package",
                "house_resolution",
                "senate_resolution",
            ),
        ),
        base_root=tmp_path,
    )

    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == [
            BUNDLE_METADATA_NAME,
            "data/congressional_directories/118th/118th_committee_memberships_output.json",
            "data/crec/2023/CREC-2023-01-04/json/CREC-2023-01-04-pt1-PgH17.json",
            "data/manifests/crec_provenance_118.json",
            "data/manifests/manifest_118.csv",
            "data/resolutions/118th/bills/hres/BILLS-118hres1eh.xml",
            "data/resolutions/118th/bills/sres/BILLS-118sres1ats.xml",
        ]
        metadata = json.loads(bundle.read(BUNDLE_METADATA_NAME))
        assert metadata["entries"][1]["local_path"] == (
            "data/crec/2023/CREC-2023-01-04/json"
        )
        assert metadata["skipped_optional"] == [
            {
                "expected_type": "house_journal_package",
                "identifier": "GPO-HJOURNAL-2023",
                "local_path": "data/primary/118/hjournal/GPO-HJOURNAL-2023.zip",
                "reason": "EXCLUDED_NOT_LOADER_READY",
            }
        ]

    hydrated = tmp_path / "hydrated"
    hydrate_source_bundle(archive, hydrated)
    assert not (hydrated / "data/crec/2023/CREC-2023-01-04/pdf").exists()
    projected_rows = read_manifest_csv(
        hydrated / "data/manifests/manifest_118.csv"
    )
    projected_crec = next(
        row
        for row in projected_rows
        if row.expected_type == "congressional_record_package"
    )
    assert projected_crec.local_path == "data/crec/2023/CREC-2023-01-04/json"


def test_loader_ready_bundle_projects_stale_crec_zip_row_to_normalized_json(
    tmp_path: Path,
) -> None:
    from scripts.build_source_bundle import build_loader_ready_source_bundle

    package_id = "CREC-2018-01-02"
    raw_archive = tmp_path / "data/primary/115/crec" / f"{package_id}.zip"
    raw_archive.parent.mkdir(parents=True)
    raw_archive.write_bytes(b"preserved raw package")
    normalized_json = (
        tmp_path
        / "data/crec/2018"
        / package_id
        / "json"
        / f"{package_id}-pt1-PgH1.json"
    )
    normalized_json.parent.mkdir(parents=True)
    normalized_json.write_text('{"content": []}', encoding="utf-8")

    raw_size, raw_sha256 = _tree_stats(raw_archive)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="congressional_record_package",
                identifier=package_id,
                local_path=raw_archive.relative_to(tmp_path).as_posix(),
                size_bytes=raw_size,
                sha256=raw_sha256,
            )
        ],
    )

    archive = tmp_path / "bundles/source-bundle-115.zip"
    build_loader_ready_source_bundle(
        manifest.relative_to(tmp_path),
        archive,
        SourceClassification(required=("congressional_record_package",)),
        base_root=tmp_path,
    )

    hydrated = tmp_path / "hydrated"
    hydrate_source_bundle(archive, hydrated)
    projected = read_manifest_csv(
        hydrated / "data/manifests/manifest_115.csv"
    )[0]
    assert projected.local_path == f"data/crec/2018/{package_id}/json"
    assert (
        hydrated
        / "data/crec/2018"
        / package_id
        / "json"
        / normalized_json.name
    ).is_file()
    assert not (hydrated / raw_archive.relative_to(tmp_path)).exists()


def test_hydrate_source_bundle_rejects_checksum_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres51eh",
                local_path="data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml",
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )

    archive = tmp_path / "bundles/source-bundle-115.zip"
    build_source_bundle(manifest, archive, SourceClassification(required=("house_resolution",)), base_root=tmp_path)
    tampered = tmp_path / "bundles/tampered.zip"
    with zipfile.ZipFile(archive) as source_bundle, zipfile.ZipFile(tampered, "w") as target_bundle:
        for name in source_bundle.namelist():
            data = source_bundle.read(name)
            if name.endswith(".xml"):
                data = b"changed"
            target_bundle.writestr(name, data)

    destination = tmp_path / "hydrated"
    existing = destination / source.relative_to(tmp_path)
    existing.parent.mkdir(parents=True)
    existing.write_text("existing destination bytes", encoding="utf-8")

    with pytest.raises(ValueError, match="Checksum mismatch after hydrate"):
        hydrate_source_bundle(tampered, destination)

    assert existing.read_text(encoding="utf-8") == "existing destination bytes"


def test_hydrate_source_bundle_handles_cross_device_destination_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    import core.govinfo.bundle as bundle_module

    source = tmp_path / "data/resolutions/115th/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres1ih",
                local_path=source.relative_to(tmp_path).as_posix(),
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )
    archive = tmp_path / "bundles/source-bundle-115.zip"
    build_source_bundle(
        manifest,
        archive,
        SourceClassification(required=("house_resolution",)),
        base_root=tmp_path,
    )

    destination = tmp_path / "mounted-workspace"
    destination.mkdir()
    original_replace = bundle_module.os.replace

    def cross_device_replace(source_path: Path, target_path: Path) -> None:
        if ".hydrate-" in str(source_path) and not str(source_path).startswith(
            str(destination)
        ):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        original_replace(source_path, target_path)

    monkeypatch.setattr(bundle_module.os, "replace", cross_device_replace)

    hydrate_source_bundle(archive, destination)

    assert (destination / source.relative_to(tmp_path)).read_text(
        encoding="utf-8"
    ) == "house"
    assert list(destination.rglob("*.tmp")) == []


def test_hydrate_source_bundle_rejects_undeclared_member_before_merge(
    tmp_path: Path,
) -> None:
    source = tmp_path / "data/resolutions/115th/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres1ih",
                local_path=source.relative_to(tmp_path).as_posix(),
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )
    archive = tmp_path / "bundles/source-bundle-115.zip"
    build_source_bundle(
        manifest,
        archive,
        SourceClassification(required=("house_resolution",)),
        base_root=tmp_path,
    )
    tampered = tmp_path / "bundles/undeclared.zip"
    with zipfile.ZipFile(archive) as source_bundle, zipfile.ZipFile(
        tampered, "w"
    ) as target_bundle:
        for name in source_bundle.namelist():
            target_bundle.writestr(name, source_bundle.read(name))
        target_bundle.writestr("undeclared.txt", b"not in bundle metadata")

    destination = tmp_path / "hydrated"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="undeclared member"):
        hydrate_source_bundle(tampered, destination)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (destination / "undeclared.txt").exists()
    assert not (destination / source.relative_to(tmp_path)).exists()


def test_hydrate_source_bundle_rejects_destination_symlink(tmp_path: Path) -> None:
    source = tmp_path / "data/resolutions/115th/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres1ih",
                local_path=source.relative_to(tmp_path).as_posix(),
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )
    archive = tmp_path / "bundles/source-bundle-115.zip"
    build_source_bundle(
        manifest,
        archive,
        SourceClassification(required=("house_resolution",)),
        base_root=tmp_path,
    )

    destination = tmp_path / "hydrated"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "data").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        hydrate_source_bundle(archive, destination)

    assert list(outside.rglob("*")) == []


def test_build_source_bundle_fails_when_required_input_is_missing(tmp_path: Path) -> None:
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres51eh",
                local_path="data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml",
                size_bytes="",
                sha256="",
                status="MISSING_LOCAL",
            )
        ],
    )

    with pytest.raises(ValueError, match="Required bundle inputs are missing or unavailable"):
        build_source_bundle(
            manifest,
            tmp_path / "bundles/source-bundle-115.zip",
            SourceClassification(required=("house_resolution",)),
            base_root=tmp_path,
        )


def test_bundle_rejects_manifest_missing_required_source_class(tmp_path: Path) -> None:
    source = tmp_path / "data/resolutions/115th/bills/hres/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres1ih",
                local_path=source.relative_to(tmp_path).as_posix(),
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )

    with pytest.raises(ValueError, match="missing required expected_type"):
        build_source_bundle(
            manifest,
            tmp_path / "bundles/source-bundle-115.zip",
            SourceClassification(
                required=("house_resolution", "senate_resolution")
            ),
            base_root=tmp_path,
        )


def test_bounded_verifier_ignores_optional_corpus_directory(tmp_path: Path) -> None:
    required = tmp_path / "data/resolutions/115th/example.xml"
    required.parent.mkdir(parents=True)
    required.write_text("house", encoding="utf-8")
    required_size, required_hash = _tree_stats(required)
    optional = tmp_path / "data/crec/2017/package"
    optional.mkdir(parents=True)
    (optional / "large.json").write_text("not touched", encoding="utf-8")
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="required",
                local_path=required.relative_to(tmp_path).as_posix(),
                size_bytes=required_size,
                sha256=required_hash,
            ),
            _manifest_row(
                congress_no=115,
                expected_type="congressional_record_package",
                identifier="optional",
                local_path=optional.relative_to(tmp_path).as_posix(),
                size_bytes=999,
                sha256="wrong",
            ),
        ],
    )

    result = verify_required_manifest_inputs(
        manifest,
        SourceClassification(
            required=("house_resolution",),
            optional=("congressional_record_package",),
        ),
        base_root=tmp_path,
    )

    assert result == {"verified_rows": 1, "verified_bytes": required_size}


def test_missing_validation_only_journal_does_not_block_required_verification(
    tmp_path: Path,
) -> None:
    required = tmp_path / "data/primary/118/crec/CREC-example.zip"
    required.parent.mkdir(parents=True)
    required.write_bytes(b"crec")
    required_size, required_hash = _tree_stats(required)
    manifest = tmp_path / "data/manifests/manifest_118.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=118,
                expected_type="congressional_record_package",
                identifier="CREC-example",
                local_path=required.relative_to(tmp_path).as_posix(),
                size_bytes=required_size,
                sha256=required_hash,
            ),
            _manifest_row(
                congress_no=118,
                expected_type="house_journal",
                identifier="GPO-HJOURNAL-2024",
                local_path="data/journals/GPO-HJOURNAL-2024",
                size_bytes="",
                sha256="",
                status="MISSING_LOCAL",
            ),
        ],
    )

    result = verify_required_manifest_inputs(
        manifest,
        SourceClassification(
            required=("congressional_record_package",),
            validation_only=("house_journal",),
        ),
        base_root=tmp_path,
    )

    assert result == {"verified_rows": 1, "verified_bytes": required_size}


def test_required_candidate_class_overrides_validation_only_source_type(
    tmp_path: Path,
) -> None:
    source = tmp_path / "data/resolutions/115th/committee.xml"
    source.parent.mkdir(parents=True)
    source.write_text("committee", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    row = _manifest_row(
        congress_no=115,
        expected_type="house_resolution",
        identifier="BILLS-115hres51eh",
        local_path=source.relative_to(tmp_path).as_posix(),
        size_bytes=size_bytes,
        sha256=sha256,
    )
    write_manifest_csv(
        manifest,
        [
            row.__class__(
                **{**row.__dict__, "candidate_class": "committee_assignment"}
            )
        ],
    )

    result = verify_required_manifest_inputs(
        manifest,
        SourceClassification(
            required=(),
            validation_only=("house_resolution",),
            required_candidate_classes=("committee_assignment",),
        ),
        base_root=tmp_path,
    )

    assert result == {"verified_rows": 1, "verified_bytes": size_bytes}


def test_hydrate_source_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    manifest_bytes = b"ruleset_id,congress_no,collection_code,chamber,artifact_level,identifier,package_id,granule_id,expected_type,candidate_class,title,date_issued,last_modified,source_url_or_api_call,details_url,local_path,size_bytes,sha256,status,error_text\n"
    manifest_sha256 = __import__("hashlib").sha256(manifest_bytes).hexdigest()
    metadata = {
        "classifications": {"optional": [], "required": ["house_resolution"], "validation_only": []},
        "congress_no": 115,
        "entries": [],
        "manifest": {"path": "data/manifests/manifest_115.csv", "sha256": manifest_sha256},
        "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
        "skipped_optional": [],
        "tool_version": "1",
    }
    archive = tmp_path / "bundles/traversal.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(BUNDLE_METADATA_NAME, json.dumps(metadata))
        bundle.writestr("data/manifests/manifest_115.csv", manifest_bytes)
        bundle.writestr("../escape.txt", b"nope")

    with pytest.raises(ValueError, match="Path traversal"):
        hydrate_source_bundle(archive, tmp_path / "hydrated")


def test_hydrate_source_bundle_from_index_uses_local_archive_reference(tmp_path: Path) -> None:
    source = tmp_path / "data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres51eh",
                local_path="data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml",
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )

    archive = tmp_path / "bundles/source-bundle-115.zip"
    index = tmp_path / "manifests/source-bundles.json"
    build_source_bundle(
        manifest,
        archive,
        SourceClassification(required=("house_resolution",)),
        base_root=tmp_path,
        index_path=index,
        archive_url="https://example.test/source-bundle-115.zip",
    )

    hydrated_root = tmp_path / "hydrated"
    hydrated_root.mkdir()
    marker = hydrated_root / "existing.txt"
    marker.write_text("preserved", encoding="utf-8")
    result = hydrate_source_bundle_from_index(index, 115, hydrated_root)

    assert result["congress_no"] == 115
    assert marker.read_text(encoding="utf-8") == "preserved"
    assert (hydrated_root / "data/resolutions/115th/bills/hres/BILLS-115hres51eh.xml").read_text(encoding="utf-8") == "house"


def test_hydrate_source_bundle_from_index_downloads_and_verifies_remote_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "data/resolutions/115th/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres1ih",
                local_path="data/resolutions/115th/example.xml",
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )
    archive = tmp_path / "bundles/source-bundle-115.zip"
    index = tmp_path / "manifests/source-bundles.json"
    build_source_bundle(
        manifest,
        archive,
        SourceClassification(required=("house_resolution",)),
        base_root=tmp_path,
        index_path=index,
        archive_url="https://example.test/source-bundle-115.zip",
    )
    archive_bytes = archive.read_bytes()
    archive.unlink()
    monkeypatch.setattr(
        "core.govinfo.bundle.urlopen",
        lambda _request, timeout: io.BytesIO(archive_bytes),
    )

    result = hydrate_source_bundle_from_index(index, 115, tmp_path / "hydrated")

    assert result["congress_no"] == 115
    assert archive.read_bytes() == archive_bytes


def test_remote_bundle_hash_failure_does_not_publish_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "data/resolutions/115th/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres1ih",
                local_path="data/resolutions/115th/example.xml",
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )
    archive = tmp_path / "bundles/source-bundle-115.zip"
    index = tmp_path / "manifests/source-bundles.json"
    build_source_bundle(
        manifest,
        archive,
        SourceClassification(required=("house_resolution",)),
        base_root=tmp_path,
        index_path=index,
        archive_url="https://example.test/source-bundle-115.zip",
    )
    corrupt = bytearray(archive.read_bytes())
    corrupt[-1] ^= 1
    archive.unlink()
    monkeypatch.setattr(
        "core.govinfo.bundle.urlopen",
        lambda _request, timeout: io.BytesIO(corrupt),
    )

    with pytest.raises(ValueError, match="Downloaded source bundle has SHA-256"):
        hydrate_source_bundle_from_index(index, 115, tmp_path / "hydrated")

    assert not archive.exists()
    assert not list(archive.parent.glob(f".{archive.name}.download-*"))


def test_relative_root_records_archive_path_relative_to_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "data/resolutions/115th/example.xml"
    source.parent.mkdir(parents=True)
    source.write_text("house", encoding="utf-8")
    size_bytes, sha256 = _tree_stats(source)
    manifest = tmp_path / "data/manifests/manifest_115.csv"
    write_manifest_csv(
        manifest,
        [
            _manifest_row(
                congress_no=115,
                expected_type="house_resolution",
                identifier="BILLS-115hres1ih",
                local_path="data/resolutions/115th/example.xml",
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ],
    )
    monkeypatch.chdir(tmp_path)

    build_source_bundle(
        Path("data/manifests/manifest_115.csv"),
        Path("bundles/source-bundle-115.zip"),
        SourceClassification(required=("house_resolution",)),
        base_root=Path("."),
        index_path=Path("manifests/source-bundles.json"),
    )

    index = json.loads(
        Path("manifests/source-bundles.json").read_text(encoding="utf-8")
    )
    assert index["bundles"][0]["archive"]["path"] == (
        "../bundles/source-bundle-115.zip"
    )
    hydrate_source_bundle_from_index(
        Path("manifests/source-bundles.json"), 115, Path("hydrated")
    )
