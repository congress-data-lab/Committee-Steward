from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.assemble_release_tree import (
    _release_artifact_names,
    assemble_release_tree,
)


def _write(path: Path, content: str = "safe\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _policy(path: Path, *, source: str = "README.md") -> Path:
    policy = {
        "schema_version": 1,
        "files": [{"source": source, "destination": "README.md"}],
        "trees": [
            {"source": "core", "destination": "core", "suffixes": [".py"]}
        ],
    }
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_assembly_copies_only_allowlisted_files_deterministically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "README.md", "# Committee Steward\n")
    _write(source / "core/__init__.py", "VALUE = 1\n")
    _write(source / "core/ignore.txt", "not released\n")
    _write(source / ".env", "TOKEN=not-released\n")
    policy = _policy(tmp_path / "policy.json")

    first = assemble_release_tree(
        source_root=source,
        destination=tmp_path / "release-a",
        policy_path=policy,
    )
    second = assemble_release_tree(
        source_root=source,
        destination=tmp_path / "release-b",
        policy_path=policy,
    )

    assert first.tree_sha256 == second.tree_sha256
    assert first.file_count == second.file_count == 2
    assert (first.destination / "README.md").read_text(encoding="utf-8") == "# Committee Steward\n"
    assert (first.destination / "core/__init__.py").is_file()
    assert not (first.destination / "core/ignore.txt").exists()
    assert not (first.destination / ".env").exists()


def test_assembly_tree_exclusions_block_internal_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "README.md")
    _write(source / "core/public.py")
    _write(source / "core/internal.py")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [{"source": "README.md", "destination": "README.md"}],
                "trees": [
                    {
                        "source": "core",
                        "destination": "core",
                        "suffixes": [".py"],
                        "exclude": ["internal.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = assemble_release_tree(
        source_root=source,
        destination=tmp_path / "release",
        policy_path=policy_path,
    )

    assert (result.destination / "core/public.py").is_file()
    assert not (result.destination / "core/internal.py").exists()


def test_assembly_is_atomic_when_safety_check_fails(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "README.md", "See /" + "home/example/private/file.txt\n")
    (source / "core").mkdir()
    policy = _policy(tmp_path / "policy.json")
    destination = tmp_path / "release"

    with pytest.raises(ValueError, match="failed safety checks"):
        assemble_release_tree(
            source_root=source,
            destination=destination,
            policy_path=policy,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".release.assembling-*"))


def test_assembly_rejects_policy_traversal_and_source_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "README.md")
    (source / "core").mkdir()
    outside = tmp_path / "outside.py"
    _write(outside)
    (source / "core/linked.py").symlink_to(outside)
    traversal_policy = _policy(tmp_path / "traversal.json", source="../outside.py")

    with pytest.raises(ValueError, match="must stay within"):
        assemble_release_tree(
            source_root=source,
            destination=tmp_path / "traversal-release",
            policy_path=traversal_policy,
        )

    valid_policy = _policy(tmp_path / "valid.json")
    with pytest.raises(ValueError, match="contains a symlink"):
        assemble_release_tree(
            source_root=source,
            destination=tmp_path / "symlink-release",
            policy_path=valid_policy,
        )


def test_complete_assembly_requires_and_copies_release_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "README.md")
    (source / "core").mkdir()
    _write(source / "data/reference/restricted-house.csv", "restricted fixture\n")
    _write(source / "data/reference/restricted-senate.csv", "restricted fixture\n")
    policy = _policy(tmp_path / "policy.json")

    with pytest.raises(ValueError, match="--complete requires"):
        assemble_release_tree(
            source_root=source,
            destination=tmp_path / "incomplete",
            policy_path=policy,
            complete=True,
        )

    license_file = tmp_path / "LICENSE"
    bundle_index = tmp_path / "source-bundles.json"
    artifacts = tmp_path / "artifacts"
    _write(license_file, "MIT License\n")
    _write(bundle_index, "{}\n")
    for name in _release_artifact_names(113, 118):
        _write(artifacts / name, f"fixture {name}\n")
    _write(
        artifacts / "internal_mismatches_113_118.csv",
        "restricted derived fixture\n",
    )

    result = assemble_release_tree(
        source_root=source,
        destination=tmp_path / "complete",
        policy_path=policy,
        license_file=license_file,
        source_bundle_index=bundle_index,
        release_artifacts_dir=artifacts,
        complete=True,
    )

    assert result.complete is True
    assert (result.destination / "LICENSE").is_file()
    assert (result.destination / "manifests/source-bundles.json").is_file()
    release_dir = result.destination / "data/releases/v0.1.0"
    assert sorted(path.name for path in release_dir.iterdir()) == sorted(
        _release_artifact_names(113, 118)
    )
    assert not (result.destination / "data/reference").exists()
    assert not (release_dir / "internal_mismatches_113_118.csv").exists()


def test_public_release_policy_excludes_internal_assignment_files() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / "config/release-files.json").read_text(encoding="utf-8"))
    released_sources = {entry["source"] for entry in policy["files"]}

    assert not any("assignment" in source and source.endswith(".csv") for source in released_sources)
    assert not any(
        "mismatches" in name and not name.startswith("directory_")
        for name in _release_artifact_names(113, 118)
    )


def test_assembly_refuses_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "README.md")
    (source / "core").mkdir()
    policy = _policy(tmp_path / "policy.json")
    destination = tmp_path / "release"
    destination.mkdir()

    with pytest.raises(FileExistsError, match="destination already exists"):
        assemble_release_tree(
            source_root=source,
            destination=destination,
            policy_path=policy,
        )


def test_release_policy_includes_local_scripts_imported_by_shipped_tests() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / "config/release-files.json").read_text(encoding="utf-8"))
    released_sources = {entry["source"] for entry in policy["files"]}
    required_scripts: set[str] = set()

    for test_path in sorted((root / "tests").glob("test_*.py")):
        for node in ast.walk(ast.parse(test_path.read_text(encoding="utf-8"))):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith("scripts."):
                    relative = Path(*module.split(".")).with_suffix(".py").as_posix()
                    if (root / relative).is_file():
                        required_scripts.add(relative)

    assert required_scripts <= released_sources
