import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from core.govinfo.crec import (
    CrecArchiveError,
    external_crec_parser_runner,
    extract_crec_archive,
    generate_crec_json,
    write_crec_provenance,
)
from core.govinfo.manifest import build_committee_manifest


PACKAGE_ID = "CREC-2021-01-03"


def _clean_parser_checkout(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    source = path / "congressionalrecord/__init__.py"
    source.parent.mkdir()
    source.write_text("\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_archive(path: Path, members: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _valid_archive(path: Path) -> None:
    _write_archive(
        path,
        {
            f"{PACKAGE_ID}/mods.xml": b"<mods />\n",
            f"{PACKAGE_ID}/html/{PACKAGE_ID}-pt1-PgH1.htm": b"<p>Record</p>\n",
        },
    )


def test_extract_crec_archive_is_atomic_idempotent_and_preserves_derived_json(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "data/primary/117/crec" / f"{PACKAGE_ID}.zip"
    _valid_archive(archive)

    first = extract_crec_archive(archive, root=tmp_path)

    assert first.status == "extracted"
    assert first.destination == tmp_path / "data/crec/2021" / PACKAGE_ID
    assert first.member_count == 2
    assert first.json_file_count == 0
    assert (first.destination / "mods.xml").read_bytes() == b"<mods />\n"
    assert stat.S_IMODE((first.destination / "mods.xml").stat().st_mode) == 0o644
    assert not list(first.destination.parent.glob(f".{PACKAGE_ID}.*"))

    derived = first.destination / "json/example.json"
    derived.parent.mkdir()
    derived.write_text("{}\n", encoding="utf-8")

    second = extract_crec_archive(archive, root=tmp_path)

    assert second.status == "already_extracted"
    assert second.tree_sha256 == first.tree_sha256
    assert second.json_file_count == 1
    assert derived.read_text(encoding="utf-8") == "{}\n"


@pytest.mark.parametrize(
    "member_name",
    [
        "../escape.txt",
        f"{PACKAGE_ID}/../../escape.txt",
        "/absolute.txt",
        f"wrong-package/{PACKAGE_ID}.txt",
        f"{PACKAGE_ID}\\escape.txt",
    ],
)
def test_extract_crec_archive_rejects_unsafe_or_wrong_root_members(
    tmp_path: Path, member_name: str
) -> None:
    archive = tmp_path / "data/primary/117/crec" / f"{PACKAGE_ID}.zip"
    _write_archive(archive, {member_name: b"unsafe"})

    with pytest.raises(CrecArchiveError):
        extract_crec_archive(archive, root=tmp_path)

    assert not (tmp_path / "data/crec/2021" / PACKAGE_ID).exists()
    assert not (tmp_path / "escape.txt").exists()


def test_extract_crec_archive_rejects_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "data/primary/117/crec" / f"{PACKAGE_ID}.zip"
    archive.parent.mkdir(parents=True)
    link = zipfile.ZipInfo(f"{PACKAGE_ID}/html/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(link, "../../escape")

    with pytest.raises(CrecArchiveError, match="symlink"):
        extract_crec_archive(archive, root=tmp_path)


def test_extract_crec_archive_refuses_conflicting_existing_tree(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "data/primary/117/crec" / f"{PACKAGE_ID}.zip"
    _valid_archive(archive)
    destination = tmp_path / "data/crec/2021" / PACKAGE_ID
    conflicting = destination / "mods.xml"
    conflicting.parent.mkdir(parents=True)
    conflicting.write_bytes(b"changed")

    with pytest.raises(CrecArchiveError, match="does not match"):
        extract_crec_archive(archive, root=tmp_path)

    assert conflicting.read_bytes() == b"changed"


def test_generate_crec_json_is_atomic_idempotent_and_validates_documents(
    tmp_path: Path,
) -> None:
    package = tmp_path / "data/crec/2021" / PACKAGE_ID
    (package / "html").mkdir(parents=True)
    (package / "html/input.htm").write_text("record", encoding="utf-8")
    calls = 0

    def runner(_package: Path, output: Path) -> None:
        nonlocal calls
        calls += 1
        output.mkdir()
        (output / f"{PACKAGE_ID}-pt1-PgH1.json").write_text(
            json.dumps({"header": {"chamber": "House"}, "content": []}) + "\n",
            encoding="utf-8",
        )

    first = generate_crec_json(package, parser_runner=runner)
    second = generate_crec_json(package, parser_runner=runner)

    assert first.status == "generated"
    assert second.status == "already_generated"
    assert first.sha256 == second.sha256
    assert first.file_count == second.file_count == 1
    assert calls == 2
    assert not list(package.glob(".json.*"))


def test_generate_crec_json_rejects_invalid_parser_output_without_publishing(
    tmp_path: Path,
) -> None:
    package = tmp_path / "data/crec/2021" / PACKAGE_ID
    (package / "html").mkdir(parents=True)

    def runner(_package: Path, output: Path) -> None:
        output.mkdir()
        (output / "bad.json").write_text("[]\n", encoding="utf-8")

    with pytest.raises(CrecArchiveError, match="JSON object"):
        generate_crec_json(package, parser_runner=runner)

    assert not (package / "json").exists()
    assert not list(package.glob(".json.*"))


def test_generate_crec_json_can_retry_after_parser_failure(tmp_path: Path) -> None:
    package = tmp_path / "data/crec/2021" / PACKAGE_ID
    (package / "html").mkdir(parents=True)
    attempts = 0

    def runner(_package: Path, output: Path) -> None:
        nonlocal attempts
        attempts += 1
        output.mkdir()
        if attempts == 1:
            (output / "partial.json").write_text("{", encoding="utf-8")
            raise RuntimeError("parser interrupted")
        (output / "complete.json").write_text(
            '{"header":{},"content":[]}\n', encoding="utf-8"
        )

    with pytest.raises(RuntimeError, match="interrupted"):
        generate_crec_json(package, parser_runner=runner)
    result = generate_crec_json(package, parser_runner=runner)

    assert result.status == "generated"
    assert result.file_count == 1
    assert not (package / "json/partial.json").exists()
    assert (package / "json/complete.json").is_file()
    assert not list(package.glob(".json.*"))


def test_crec_provenance_preserves_raw_and_normalized_hashes(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "data/primary/117/crec" / f"{PACKAGE_ID}.zip"
    _valid_archive(archive)
    extraction = extract_crec_archive(archive, root=tmp_path)

    def runner(_package: Path, output: Path) -> None:
        output.mkdir()
        (output / f"{PACKAGE_ID}-pt1-PgH1.json").write_text(
            '{"header":{},"content":[]}\n', encoding="utf-8"
        )

    parsed = generate_crec_json(extraction.destination, parser_runner=runner)
    revision = "a" * 40
    path = write_crec_provenance(
        117,
        [(extraction, parsed)],
        root=tmp_path,
        parser_revision=revision,
    )
    first_bytes = path.read_bytes()
    payload = json.loads(first_bytes)

    assert payload["schema_version"] == "crec_normalization_provenance_v1"
    assert payload["congress_no"] == 117
    assert payload["parser"]["revision"] == revision
    assert len(payload["packages"]) == 1
    package = payload["packages"][0]
    assert package["package_id"] == PACKAGE_ID
    assert package["raw_archive"]["sha256"]
    assert package["raw_archive"]["size_bytes"] == archive.stat().st_size
    assert package["raw_tree"]["sha256"] == extraction.tree_sha256
    assert package["normalized_tree"]["sha256"]
    assert package["json"]["sha256"] == parsed.sha256
    assert package["json"]["file_count"] == 1

    write_crec_provenance(
        117,
        [(extraction, parsed)],
        root=tmp_path,
        parser_revision=revision,
    )
    assert path.read_bytes() == first_bytes


@pytest.mark.parametrize("dirty_kind", ["tracked", "staged", "untracked"])
def test_external_parser_rejects_dirty_checkout(
    tmp_path: Path, dirty_kind: str
) -> None:
    parser_root = tmp_path / "parser"
    revision = _clean_parser_checkout(parser_root)
    if dirty_kind == "tracked":
        (parser_root / "congressionalrecord/__init__.py").write_text(
            "changed\n", encoding="utf-8"
        )
    elif dirty_kind == "staged":
        staged = parser_root / "congressionalrecord/staged.py"
        staged.write_text("\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(parser_root), "add", str(staged)], check=True)
    else:
        (parser_root / "json.py").write_text("# import shadow\n", encoding="utf-8")

    with pytest.raises(CrecArchiveError, match="must be clean"):
        external_crec_parser_runner(
            parser_root,
            expected_revision=revision,
            python_executable=Path(sys.executable),
        )


def test_external_parser_rechecks_checkout_before_execution(tmp_path: Path) -> None:
    parser_root = tmp_path / "parser"
    revision = _clean_parser_checkout(parser_root)
    runner = external_crec_parser_runner(
        parser_root,
        expected_revision=revision,
        python_executable=Path(sys.executable),
    )
    (parser_root / "congressionalrecord/shadow.py").write_text(
        "# changed after runner creation\n", encoding="utf-8"
    )

    with pytest.raises(CrecArchiveError, match="must be clean"):
        runner(tmp_path / "package", tmp_path / "output")


def test_manifest_prefers_loader_ready_crec_tree_over_downloaded_zip(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "data/primary/117/crec" / f"{PACKAGE_ID}.zip"
    _valid_archive(archive)
    extract_crec_archive(archive, root=tmp_path)

    class Client:
        def iter_published(self, _start: str, _end: str, **kwargs):
            if kwargs["collection"] == "CREC":
                yield {
                    "packageId": PACKAGE_ID,
                    "title": "Congressional Record",
                    "dateIssued": "2021-01-03",
                    "collectionCode": "CREC",
                }

        def iter_search(self, _query: str, **_kwargs):
            return iter(())

        def iter_granules(self, _package_id: str, **_kwargs):
            return iter(())

        def download_package(self, *_args):
            raise AssertionError("download not expected")

    rows = build_committee_manifest(
        Client(),
        117,
        root=tmp_path,
        include_crec_granules=False,
        include_journals=False,
        include_directory=False,
    )
    crec = next(row for row in rows if row.expected_type == "congressional_record_package")

    destination = tmp_path / "data/crec/2021" / PACKAGE_ID
    assert Path(crec.local_path) == destination
    assert crec.size_bytes != archive.stat().st_size
