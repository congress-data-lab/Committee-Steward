#!/usr/bin/env python3
"""Check a release tree or supplied file list for unsafe repository contents."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


RAW_CORPUS_PREFIXES = (
    "data/resolutions",
    "data/crec",
    "data/journals",
    "data/congressional_directories",
    "data/primary",
)

FORBIDDEN_PREFIXES: tuple[tuple[str, str, str], ...] = (
    ("logs", "LOG_TREE", "Remove runtime or diagnostic logs from the release tree."),
    ("output", "GENERATED_OUTPUT", "Remove regenerated exports and validation outputs from the release tree."),
    (".omx", "AGENT_STATE", "Remove OMX state before packaging a public release."),
    (".claude", "AGENT_STATE", "Remove local Claude state before packaging a public release."),
    (".cursor", "AGENT_STATE", "Remove local Cursor state before packaging a public release."),
    (".agents", "AGENT_STATE", "Remove local agent definitions and state before packaging a public release."),
    (".pytest_cache", "CACHE_STATE", "Remove pytest cache directories from the release tree."),
    (".ruff_cache", "CACHE_STATE", "Remove Ruff cache directories from the release tree."),
    ("__pycache__", "CACHE_STATE", "Remove Python bytecode caches from the release tree."),
    (".venv", "ENVIRONMENT_STATE", "Do not ship local virtual environments in the release tree."),
    ("node_modules", "ENVIRONMENT_STATE", "Do not ship local dependency vendor trees in the release tree."),
    ("tmp", "SCRATCH_STATE", "Remove scratch directories from the release tree."),
)

ANYWHERE_COMPONENT_PREFIXES: tuple[tuple[str, str, str], ...] = (
    ("__pycache__", "CACHE_STATE", "Remove Python bytecode caches from the release tree."),
)

FORBIDDEN_SUFFIXES: tuple[tuple[str, str, str], ...] = (
    (".dump", "DATABASE_DUMP", "Remove database dumps from the release tree."),
    (".log", "LOG_FILE", "Remove runtime or diagnostic logs from the release tree."),
)

RESTRICTED_REFERENCE_BASENAMES = {
    "load_stewart_reference.py",
    "stewart_overlap.py",
    "flag_stewart_conflicts.py",
    "score_stewart_overlap.py",
    "generate_validation_reports.py",
    "test_stewart_overlap.py",
    "seed_committee_stewart_codes.sql",
    "0002_add_stewart_appointment_citation.sql",
    "0003_add_stewart_reconciliation_columns.sql",
    "0005_stewart_committee_code_mapping.sql",
    "0009_committee_stewart_code_senate.sql",
}

RESTRICTED_REFERENCE_SCHEMA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bstewart_(?:house|senate)\b", re.IGNORECASE),
    re.compile(r"\bcommittee_" r"stewart_code\b", re.IGNORECASE),
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "PRIVATE_KEY",
        re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "Remove private keys from the release tree and rotate them if they were ever active.",
    ),
    (
        "DATABASE_URL",
        re.compile(r"postgres(?:ql)?://[^/\s:@]+:[^/\s@]+@[^/\s]+", re.IGNORECASE),
        "Remove embedded database credentials from releasable files.",
    ),
    (
        "AWS_ACCESS_KEY",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "Remove AWS credentials from the release tree and rotate them if they were ever active.",
    ),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
        "Remove GitHub tokens from the release tree and rotate them if they were ever active.",
    ),
    (
        "PROVIDER_TOKEN",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
        "Remove API tokens from the release tree and rotate them if they were ever active.",
    ),
    (
        "GENERIC_SECRET_ASSIGNMENT",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_.=-]{16,}"
        ),
        "Remove credential-like assignments from releasable files.",
    ),
)

ABSOLUTE_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "UNIX_HOME_PATH",
        re.compile(r"(?<![A-Za-z0-9_./-])/(?:home|Users)/[A-Za-z0-9_.-]+(?:/[^\s\"'`]+)+"),
        "Replace developer-specific absolute paths with repository-relative paths.",
    ),
    (
        "WINDOWS_HOME_PATH",
        re.compile(r"(?<![A-Za-z0-9_./-])[A-Za-z]:\\\\Users\\\\[^\\/\s\"']+(?:\\\\[^\s\"']+)+"),
        "Replace developer-specific absolute paths with repository-relative paths.",
    ),
)

TEXT_SCAN_BYTES = 1_000_000
DATABASE_URL_PLACEHOLDER_CREDENTIALS = {
    ("user", "password"),
    ("username", "password"),
}


@dataclass(frozen=True)
class Issue:
    kind: str
    path: str
    line: int | None
    detail: str
    action: str


def _normalize_relative(path_text: str) -> PurePosixPath:
    cleaned = path_text.replace("\\", "/").strip()
    if not cleaned:
        raise ValueError("empty path")
    candidate = PurePosixPath(cleaned)
    if candidate.is_absolute():
        raise ValueError(f"path must be relative: {path_text}")
    if any(part in ("", ".", "..") for part in candidate.parts):
        raise ValueError(f"path must stay inside the target root: {path_text}")
    return candidate


def _looks_text(sample: bytes) -> bool:
    return b"\0" not in sample


def _is_placeholder_database_url(value: str) -> bool:
    """Allow explicit documentation/env placeholders without hiding credentials."""
    if "${" in value:
        return True
    authority = value.split("://", 1)[-1].split("@", 1)[0]
    if ":" not in authority:
        return False
    username, password = authority.split(":", 1)
    return (username.lower(), password.lower()) in DATABASE_URL_PLACEHOLDER_CREDENTIALS


def _iter_tree_entries(root: Path) -> list[PurePosixPath]:
    entries: set[PurePosixPath] = set()
    for current in sorted(root.rglob("*")):
        rel = PurePosixPath(current.relative_to(root).as_posix())
        entries.add(rel)
    return sorted(entries, key=lambda item: item.as_posix())


def _iter_listed_entries(root: Path, listed_paths: Sequence[str]) -> tuple[list[PurePosixPath], list[Issue]]:
    issues: list[Issue] = []
    collected: set[PurePosixPath] = set()
    for raw_path in listed_paths:
        if not raw_path.strip():
            continue
        try:
            rel = _normalize_relative(raw_path)
        except ValueError as exc:
            issues.append(
                Issue(
                    kind="INVALID_LIST_PATH",
                    path=raw_path.strip() or "<blank>",
                    line=None,
                    detail=str(exc),
                    action="Supply repository-relative paths, typically from git ls-files.",
                )
            )
            continue
        resolved = root / Path(rel.as_posix())
        if not resolved.exists():
            issues.append(
                Issue(
                    kind="MISSING_LISTED_PATH",
                    path=rel.as_posix(),
                    line=None,
                    detail="Path from supplied file list does not exist under the target root.",
                    action="Regenerate the file list from the intended tree before release validation.",
                )
            )
            continue
        if resolved.is_dir():
            collected.add(rel)
            for child in sorted(resolved.rglob("*")):
                collected.add(PurePosixPath(child.relative_to(root).as_posix()))
        else:
            collected.add(rel)
    return sorted(collected, key=lambda item: item.as_posix()), issues


def _path_issue(rel: PurePosixPath) -> Issue | None:
    path_text = rel.as_posix()

    if rel.name.lower() in RESTRICTED_REFERENCE_BASENAMES:
        return Issue(
            kind="RESTRICTED_REFERENCE_TOOLING",
            path=path_text,
            line=None,
            detail="Internal comparison tooling is present in the public candidate.",
            action="Keep separately licensed reference-data tooling in the private maintainer repository.",
        )

    for prefix in RAW_CORPUS_PREFIXES:
        if path_text == prefix or path_text.startswith(f"{prefix}/"):
            return Issue(
                kind="RAW_CORPUS_PATH",
                path=prefix,
                line=None,
                detail="Raw primary-source corpora are present in the candidate release tree.",
                action="Ship source manifests and frozen bundles instead of repository raw corpora.",
            )

    parts = rel.parts
    for prefix, kind, action in FORBIDDEN_PREFIXES:
        prefix_parts = PurePosixPath(prefix).parts
        if parts[: len(prefix_parts)] == prefix_parts:
            return Issue(
                kind=kind,
                path=prefix,
                line=None,
                detail=f"Forbidden development or generated path is present: {path_text}",
                action=action,
            )

    for component, kind, action in ANYWHERE_COMPONENT_PREFIXES:
        if component in parts:
            return Issue(
                kind=kind,
                path=component,
                line=None,
                detail=f"Forbidden development or generated path is present: {path_text}",
                action=action,
            )

    if rel.name.startswith(".env") and rel.name != ".env.example":
        return Issue(
            kind="SECRET_FILE",
            path=path_text,
            line=None,
            detail=f"Forbidden environment filename is present: {rel.name}",
            action="Remove environment files; publish .env.example only.",
        )

    lowered = rel.name.lower()
    for suffix, kind, action in FORBIDDEN_SUFFIXES:
        if lowered.endswith(suffix):
            return Issue(
                kind=kind,
                path=path_text,
                line=None,
                detail=f"Forbidden generated artifact is present: {rel.name}",
                action=action,
            )

    return None


def _scan_file(root: Path, rel: PurePosixPath) -> list[Issue]:
    path = root / Path(rel.as_posix())
    if not path.is_file():
        return []

    sample = path.read_bytes()[:TEXT_SCAN_BYTES]
    if not sample or not _looks_text(sample):
        return []

    text = sample.decode("utf-8", errors="ignore")
    issues: list[Issue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if any(pattern.search(line) for pattern in RESTRICTED_REFERENCE_SCHEMA_PATTERNS):
            issues.append(
                Issue(
                    kind="RESTRICTED_REFERENCE_SCHEMA",
                    path=rel.as_posix(),
                    line=line_number,
                    detail="Internal comparison-table schema is present in the public candidate.",
                    action="Remove private comparison schema and crosswalks from the public release surface.",
                )
            )
        specific_secret_matched = False
        for kind, pattern, action in SECRET_PATTERNS:
            if kind == "GENERIC_SECRET_ASSIGNMENT" and specific_secret_matched:
                continue
            match = pattern.search(line)
            if match:
                if kind == "DATABASE_URL" and _is_placeholder_database_url(match.group(0)):
                    continue
                issues.append(
                    Issue(
                        kind=kind,
                        path=rel.as_posix(),
                        line=line_number,
                        detail=f"Secret-like content matched: {match.group(0)[:80]}",
                        action=action,
                    )
                )
                if kind != "GENERIC_SECRET_ASSIGNMENT":
                    specific_secret_matched = True
        for kind, pattern, action in ABSOLUTE_PATH_PATTERNS:
            match = pattern.search(line)
            if match:
                issues.append(
                    Issue(
                        kind=kind,
                        path=rel.as_posix(),
                        line=line_number,
                        detail=f"Absolute development path matched: {match.group(0)[:120]}",
                        action=action,
                    )
                )
    return issues


def collect_issues(root: Path, listed_paths: Sequence[str] | None = None) -> list[Issue]:
    root = root.resolve()
    if listed_paths is None:
        entries = _iter_tree_entries(root)
        issues: list[Issue] = []
    else:
        entries, issues = _iter_listed_entries(root, listed_paths)

    seen_path_issues: set[tuple[str, str]] = set()
    for rel in entries:
        resolved = root / Path(rel.as_posix())
        if resolved.is_symlink():
            issues.append(
                Issue(
                    kind="SYMLINK",
                    path=rel.as_posix(),
                    line=None,
                    detail="Symbolic links are not allowed in the release tree.",
                    action="Replace the link with an allowlisted regular file inside the release tree.",
                )
            )
            continue
        path_issue = _path_issue(rel)
        if path_issue is not None:
            dedupe_key = (path_issue.kind, path_issue.path)
            if dedupe_key not in seen_path_issues:
                issues.append(path_issue)
                seen_path_issues.add(dedupe_key)
            continue
        issues.extend(_scan_file(root, rel))

    return sorted(
        issues,
        key=lambda issue: (
            issue.path,
            -1 if issue.line is None else issue.line,
            issue.kind,
            issue.detail,
        ),
    )


def format_issues(issues: Sequence[Issue]) -> str:
    if not issues:
        return "PASS release tree check"

    lines = [f"FAIL release tree check: {len(issues)} issue(s)"]
    for issue in issues:
        location = issue.path if issue.line is None else f"{issue.path}:{issue.line}"
        lines.append(f"{issue.kind}\t{location}\t{issue.detail}\tACTION: {issue.action}")
    return "\n".join(lines)


def _read_listed_paths(args: argparse.Namespace) -> Sequence[str] | None:
    if args.paths_file is not None:
        return args.paths_file.read_text(encoding="utf-8").splitlines()
    if args.paths_stdin:
        data = sys.stdin.buffer.read()
        if args.null_delimited:
            return [chunk.decode("utf-8") for chunk in data.split(b"\0") if chunk]
        return data.decode("utf-8").splitlines()
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        type=Path,
        help="Target tree root to inspect (default: current directory).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--paths-file",
        type=Path,
        help="Newline-delimited repository-relative file list, such as output from git ls-files.",
    )
    group.add_argument(
        "--paths-stdin",
        action="store_true",
        help="Read repository-relative paths from standard input.",
    )
    parser.add_argument(
        "--null-delimited",
        action="store_true",
        help="Interpret --paths-stdin input as NUL-delimited paths.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    listed_paths = _read_listed_paths(args)
    issues = collect_issues(args.target, listed_paths)
    print(format_issues(issues))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
