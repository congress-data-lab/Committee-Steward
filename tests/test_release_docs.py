from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

from core.export.schema import DATASET_SPECS
from scripts.assemble_release_tree import assemble_release_tree


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DATASETS = (
    "assignments",
    "rankings",
    "events",
    "members",
    "committees",
    "sources",
    "validation",
    "directory_mismatches",
)
EXPECTED_SCOPE_CODES = {
    "H": {
        "HSAG",
        "HSAP",
        "HSAS",
        "HSBA",
        "HSBU",
        "HSED",
        "HSFA",
        "HSGO",
        "HSHA",
        "HSHM",
        "HSIF",
        "HSII",
        "HSJU",
        "HSPW",
        "HSRU",
        "HSSM",
        "HSSO",
        "HSSY",
        "HSVR",
        "HSWM",
    },
    "S": {
        "SLIA",
        "SSAF",
        "SSAP",
        "SSAS",
        "SSBK",
        "SSBU",
        "SSCM",
        "SSEG",
        "SSEV",
        "SSFI",
        "SSFR",
        "SSGA",
        "SSHR",
        "SSJU",
        "SSRA",
        "SSSB",
        "SSVA",
    },
}


def _documented_columns(markdown: str, filename_stem: str) -> list[str]:
    heading = re.search(
        rf"^### `{re.escape(filename_stem)}_<range>\.csv`\s*$",
        markdown,
        flags=re.MULTILINE,
    )
    assert heading, f"missing schema section for {filename_stem}"
    section = markdown[heading.end() :]
    next_heading = re.search(r"^###?\s", section, flags=re.MULTILINE)
    if next_heading:
        section = section[: next_heading.start()]
    return re.findall(r"^\| `([^`]+)` \|", section, flags=re.MULTILINE)


def test_public_data_dictionary_matches_export_schema() -> None:
    markdown = (ROOT / "docs/DATA_DICTIONARY.md").read_text(encoding="utf-8")

    for key in PUBLIC_DATASETS:
        spec = DATASET_SPECS[key]
        assert spec.filename_stem is not None
        assert _documented_columns(markdown, spec.filename_stem) == [
            column.name for column in spec.columns
        ]


def test_public_scope_matches_v1_1_release_artifacts() -> None:
    markdown = (ROOT / "docs/METHODS.md").read_text(encoding="utf-8")
    section = markdown.split("## Released committee coverage", 1)[1]
    section = section.split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(H|S)` \| `([A-Z]+)` \|", section, flags=re.MULTILINE)
    observed = {
        chamber: {code for row_chamber, code in rows if row_chamber == chamber}
        for chamber in ("H", "S")
    }

    assert observed == EXPECTED_SCOPE_CODES


def test_release_policy_has_one_schema_guide() -> None:
    policy = json.loads(
        (ROOT / "config/release-files.json").read_text(encoding="utf-8")
    )
    destinations = {entry["destination"] for entry in policy["files"]}

    assert "docs/DATA_DICTIONARY.md" in destinations
    assert "docs/export-schema.md" not in destinations
    assert "docs/canonical_files.md" not in destinations


def test_assembled_release_markdown_links_resolve(tmp_path: Path) -> None:
    policy_path = ROOT / "config/release-files.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    source_layout_available = all(
        (ROOT / entry["source"]).is_file() for entry in policy["files"]
    )
    if source_layout_available:
        release_tree = tmp_path / "release-tree"
        assemble_release_tree(
            source_root=ROOT,
            destination=release_tree,
            policy_path=policy_path,
        )
    else:
        # The public repository is itself an assembled tree, so its policy
        # source paths intentionally differ from several shipped destinations.
        release_tree = ROOT
    broken: list[str] = []

    for markdown_path in sorted(release_tree.rglob("*.md")):
        markdown = markdown_path.read_text(encoding="utf-8")
        for raw_target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", markdown):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            relative_target = unquote(target.split("#", 1)[0])
            resolved = (markdown_path.parent / relative_target).resolve()
            try:
                resolved.relative_to(release_tree.resolve())
            except ValueError:
                broken.append(f"{markdown_path.relative_to(release_tree)} -> {target}")
                continue
            if not resolved.exists():
                broken.append(f"{markdown_path.relative_to(release_tree)} -> {target}")

    assert broken == []
