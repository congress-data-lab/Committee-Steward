from scripts.validate_membership_integrity import (
    SQL,
    _parse_committee_codes_from_text_span,
    main,
)


def test_termination_alignment_uses_effective_date():
    assert "ee.effective_date AS termination_date" in SQL


def test_text_span_parser_collects_every_committee_heading():
    text = (
        "(1) COMMITTEE ON AGRICULTURE - Ms. Alpha. "
        "(2) COMMITTEE ON THE BUDGET - Mr. Beta. "
        "(3) COMMITTEE ON THE JUDICIARY - Ms. Gamma."
    )

    assert _parse_committee_codes_from_text_span(text, "H") == {
        "HSAG",
        "HSBU",
        "HSJU",
    }


def test_text_span_parser_handles_empty_evidence():
    assert _parse_committee_codes_from_text_span(None, "H") == set()


def test_fail_on_issues_returns_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scripts.validate_membership_integrity.run_validation",
        lambda _congress, _chamber: [{"issue_types": "bad_link"}],
    )

    exit_code = main(
        ["--output", str(tmp_path / "issues.csv"), "--fail-on-issues"]
    )

    assert exit_code == 2


def test_fail_on_issues_passes_empty_result(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scripts.validate_membership_integrity.run_validation",
        lambda _congress, _chamber: [],
    )

    exit_code = main(
        ["--output", str(tmp_path / "issues.csv"), "--fail-on-issues"]
    )

    assert exit_code == 0
