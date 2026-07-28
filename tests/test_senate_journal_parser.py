#!/usr/bin/env python3
"""Tests for Senate journal parser (appoint-only fallback behavior)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.events.senate_journal_parser import (
    detect_actions_on_page_senate,
    passes_quality_gates_senate,
)


SENATE_APPOINT_SNIPPET = """
Standing Committees.
Notwithstanding the provisions of rule XXV, to constitute the minority party's membership on certain committees:
Committee on Agriculture, Nutrition, and Forestry: Mr. COCHRAN, Mr. ROBERTS, Mr. BOOZMAN.
"""

SENATE_TERMINATE_SNIPPET = """
Standing Committees.
Committee on Agriculture, Nutrition, and Forestry: Mr. COCHRAN was relieved from further service as a member.
"""

SENATE_CONTEXT_ONLY_NO_NAMES = """
CONSTITUTING MINORITY PARTY MEMBERSHIP ON CERTAIN COMMITTEES FOR THE ONE HUNDRED THIRTEENTH CONGRESS.
The Presiding Officer laid before the Senate the resolution making minority party appointments.
Resolved, That the Senate agree thereto.
"""


def test_detect_senate_appoints_from_context_and_roster():
    events = detect_actions_on_page_senate(
        SENATE_APPOINT_SNIPPET,
        printed_page=44,
        congress=113,
        default_year=2013,
    )
    assert len(events) >= 2, f"expected at least 2 appointments, got {events}"
    assert all(e.get("chamber") == "S" for e in events)
    assert all("Agriculture" in (e.get("committee_raw") or "") for e in events)


def test_detect_senate_termination_is_skipped():
    events = detect_actions_on_page_senate(
        SENATE_TERMINATE_SNIPPET,
        printed_page=45,
        congress=113,
        default_year=2013,
    )
    assert len(events) == 0, f"termination text should be skipped, got {events}"


def test_context_only_without_names_emits_nothing():
    events = detect_actions_on_page_senate(
        SENATE_CONTEXT_ONLY_NO_NAMES,
        printed_page=46,
        congress=113,
        default_year=2013,
    )
    assert len(events) == 0, f"context-only text should not emit names, got {events}"


def test_senate_quality_gates():
    ok, reason = passes_quality_gates_senate(
        "Committee on Agriculture, Nutrition, and Forestry",
        "Mr. ROBERTS",
    )
    assert ok and reason == ""
    ok, reason = passes_quality_gates_senate(
        "Committee on Agriculture. H.R. 1234",
        "Mr. ROBERTS",
    )
    assert not ok and reason == "committee_has_bill_tokens"
    ok, reason = passes_quality_gates_senate(
        "Committee on Agriculture, Nutrition, and Forestry",
        "Mr. Speaker",
    )
    assert not ok and reason == "member_stopword"


def run_tests():
    errors = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                errors.append(f"{name}: {e}")
    return errors


if __name__ == "__main__":
    errs = run_tests()
    if errs:
        for e in errs:
            print("FAIL:", e)
        sys.exit(1)
    print("All senate journal parser tests passed.")
