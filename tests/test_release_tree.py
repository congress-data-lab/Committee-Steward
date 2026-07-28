from __future__ import annotations

from pathlib import Path

from scripts.check_release_tree import collect_issues, format_issues, main


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_clean_tree_passes(tmp_path: Path, capsys) -> None:
    _write(tmp_path / "docs/REPRODUCING.md", "# Reproducing\n")
    _write(tmp_path / "CITATION.cff", "cff-version: 1.2.0\n")

    exit_code = main([str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == "PASS release tree check"


def test_tree_mode_reports_forbidden_paths_and_content_deterministically(tmp_path: Path) -> None:
    _write(tmp_path / "logs/run.log", "ok\n")
    _write(tmp_path / "notes.txt", "db=postgresql://" + "user:secret@example.test/db\n")
    _write(
        tmp_path / "docs/paths.md",
        "See /" + "home/developer/Projects/private/file.txt for details.\n",
    )
    _write(tmp_path / "data/resolutions/118th/bills/hres/example.xml", "<xml />\n")

    issues = collect_issues(tmp_path)
    rendered = format_issues(issues)

    assert [issue.kind for issue in issues] == [
        "RAW_CORPUS_PATH",
        "UNIX_HOME_PATH",
        "LOG_TREE",
        "DATABASE_URL",
    ]
    assert "Ship source manifests and frozen bundles instead of repository raw corpora." in rendered
    assert "Replace developer-specific absolute paths with repository-relative paths." in rendered
    assert "Remove runtime or diagnostic logs from the release tree." in rendered
    assert "Remove embedded database credentials from releasable files." in rendered


def test_file_list_mode_checks_only_listed_paths(tmp_path: Path) -> None:
    _write(tmp_path / "docs/ok.md", "safe\n")
    _write(tmp_path / "logs/ignored.log", "do not inspect\n")
    _write(tmp_path / "tracked.txt", "token=ghp_" + "abcdefghijklmnopqrstuvwxyz123456\n")

    issues = collect_issues(tmp_path, ["docs/ok.md", "tracked.txt"])

    assert [(issue.kind, issue.path) for issue in issues] == [
        ("GITHUB_TOKEN", "tracked.txt"),
    ]


def test_file_list_mode_reports_invalid_and_missing_paths(tmp_path: Path) -> None:
    _write(tmp_path / "docs/ok.md", "safe\n")

    issues = collect_issues(tmp_path, ["../escape.txt", "missing.txt", "docs/ok.md"])

    assert [(issue.kind, issue.path) for issue in issues] == [
        ("INVALID_LIST_PATH", "../escape.txt"),
        ("MISSING_LISTED_PATH", "missing.txt"),
    ]


def test_stdin_list_mode_supports_null_delimited_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(
        tmp_path / ".env",
        "PROVIDER_API_KEY=token-" + "abcdefghijklmnopqrstuvwxyz123456\n",
    )

    class _FakeStdin:
        def __init__(self, payload: bytes) -> None:
            self.buffer = self
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

    monkeypatch.setattr("sys.stdin", _FakeStdin(b".env\0"))

    exit_code = main([str(tmp_path), "--paths-stdin", "--null-delimited"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "SECRET_FILE" in captured.out


def test_environment_variants_are_rejected_but_example_is_allowed(tmp_path: Path) -> None:
    _write(tmp_path / ".env.example", "NEON_DATABASE_URL=\n")
    _write(tmp_path / ".env.production", "TOKEN=" + "not-a-real-token\n")

    issues = collect_issues(tmp_path)

    assert [(issue.kind, issue.path) for issue in issues] == [
        ("SECRET_FILE", ".env.production")
    ]


def test_database_url_placeholders_are_allowed_but_credentials_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path / "docs/example.md", "postgresql://user:password@host:5432/database\n")
    _write(
        tmp_path / "compose.yml",
        "postgresql://${POSTGRES_USER:-app}:${POSTGRES_PASSWORD:-local_only}@postgres:5432/app\n",
    )
    _write(
        tmp_path / "leaked.txt",
        "postgresql://" + "app:actual-secret@example.test/database\n",
    )

    issues = collect_issues(tmp_path)

    assert [(issue.kind, issue.path) for issue in issues] == [
        ("DATABASE_URL", "leaked.txt")
    ]


def test_symbolic_links_are_rejected_without_following_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text(
        "postgresql://" + "user:secret@example.test/db\n", encoding="utf-8"
    )
    (tmp_path / "linked.txt").symlink_to(outside)

    issues = collect_issues(tmp_path)

    assert [(issue.kind, issue.path) for issue in issues] == [("SYMLINK", "linked.txt")]


def test_internal_reference_tools_and_schema_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path / "ingest/load_stewart_reference.py", "pass\n")
    _write(
        tmp_path / "db/schema.sql",
        "CREATE TABLE stewart_" + "house (id bigint);\n",
    )

    issues = collect_issues(tmp_path)

    assert [(issue.kind, issue.path) for issue in issues] == [
        ("RESTRICTED_REFERENCE_SCHEMA", "db/schema.sql"),
        ("RESTRICTED_REFERENCE_TOOLING", "ingest/load_stewart_reference.py"),
    ]
