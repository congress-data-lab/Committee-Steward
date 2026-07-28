import hashlib
from pathlib import Path

import pytest

from scripts.compare_release_directories import compare_release_directories


def _write_release(path: Path, files: dict[str, bytes]) -> None:
    path.mkdir(parents=True)
    lines = []
    for filename, data in sorted(files.items()):
        target = path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {filename}")
    (path / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_compare_release_directories_accepts_identical_artifacts(tmp_path: Path) -> None:
    files = {"committee.csv": b"a,b\n1,2\n", "workbook.xlsx": b"xlsx"}
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_release(first, files)
    _write_release(second, files)

    assert compare_release_directories(first, second) == {
        filename: hashlib.sha256(data).hexdigest()
        for filename, data in files.items()
    }


def test_compare_release_directories_rejects_cross_run_mismatch(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_release(first, {"committee.csv": b"first"})
    _write_release(second, {"committee.csv": b"second"})

    with pytest.raises(ValueError, match="not byte-identical"):
        compare_release_directories(first, second)
