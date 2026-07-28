#!/usr/bin/env python3
"""Tests for House journal parser (detect_actions_on_page, narrative pass, gating)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.events.journal_parser as journal_parser
from core.events.journal_parser import (
    detect_actions_on_page,
    extract_date_from_page,
    extract_header_year_from_page,
    _guess_decision_date_from_filename,
    _normalize_page_text,
    _looks_like_appointment,
    _extract_narrative_appointments,
    passes_quality_gates,
)


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _FakePdfLib:
    def __init__(self, pages):
        self.pages = pages

    def open(self, _path):
        return _FakePdf(self.pages)


class _FakePage:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.closed = False

    def extract_text(self):
        if self.fail:
            raise RuntimeError("synthetic extraction failure")
        return "No relevant content"

    def close(self):
        self.closed = True


# H101-style text (2013-02-08): Speaker appointed ... to the Permanent Select Committee on Intelligence: Messrs. ...
H101_SNIPPET = """
The SPEAKER pro tempore, Mr. THORNBERRY, pursuant to clause 11 of rule X, clause 11 of rule I, and the order of the House of January 3, 2013, announced that the Speaker appointed the following Members of the House to the Permanent Select Committee on Intelligence: Messrs. THORNBERRY, MILLER of Florida, CONAWAY, KING of New York, LOBIONDO, NUNES, WESTMORELAND, M.
"""

# Jan 3 narrative: "the following Members ... Mr. ROGERS, Michigan, Chairman, and Mr. RUPPERSBERGER, Maryland."
JAN3_HLIG_SNIPPET = """
announced that the Speaker appointed the following Members of the House to the Permanent Select Committee on Intelligence: Mr. ROGERS, Michigan, Chairman, and Mr. RUPPERSBERGER, Maryland.
"""

# Anti-pattern: bill referral (should NOT produce appointments)
REFERRAL_SNIPPET = """
The bill was referred to the Committee on Agriculture. Mr. Smith submitted the report.
"""


def test_extract_date_from_page():
    text = "THURSDAY, JANUARY 3, 2013"
    assert extract_date_from_page(text) == "2013-01-03"
    text2 = "JOURNAL OF THE JANUARY 8 2013"
    assert extract_date_from_page(text2) == "2013-01-08"
    text3 = "2013 HOUSE OF REPRESENTATIVES  ¶2.4\n...\nJOURNAL OF THE    JANUARY 3"
    assert extract_date_from_page(text3, default_year=2013) == "2013-01-03"


def test_extract_header_year_from_page():
    text = "2013 HOUSE OF REPRESENTATIVES   ¶2.4"
    assert extract_header_year_from_page(text) == 2013


def test_looks_like_appointment():
    assert _looks_like_appointment("announced that the Speaker appointed the following Members") is True
    assert _looks_like_appointment("— Mr. ROGERS, Michigan") is True
    assert _looks_like_appointment("referred to the Committee on Agriculture. Mr. Smith") is False


def test_anti_pattern_referral():
    """Bill referral should not yield appointments (no Speaker appointed / roster delimiter)."""
    events = detect_actions_on_page(REFERRAL_SNIPPET, printed_page=99, congress=113, default_year=2013)
    # Parser should not emit from bare "Committee on X." referral
    assert len(events) == 0


def test_narrative_jan3_hlig():
    """Jan 3 HLIG text should yield Rogers and Ruppersberger."""
    events = _extract_narrative_appointments(
        JAN3_HLIG_SNIPPET, printed_page=25, event_date="2013-01-03", source_loc="H25"
    )
    last_names = set()
    for e in events:
        raw = e.get("member_raw", "")
        if "ROGERS" in raw.upper() or "Rogers" in raw:
            last_names.add("Rogers")
        if "RUPPERSBERGER" in raw.upper() or "Ruppersberger" in raw:
            last_names.add("Ruppersberger")
    assert "Rogers" in last_names or "Ruppersberger" in last_names, f"Expected Rogers or Ruppersberger in {events}"


def test_quality_gates_reject_junk():
    """Quality gates reject long committee swallow, bill tokens, and procedural member names."""
    # Committee too long (paragraph swallow; > 120 chars)
    long_committee = "Committee on Natural ReBurgess Heck (NV) Nunnelee and other bill text " * 2
    ok, reason = passes_quality_gates(long_committee[:130], "Mr. Smith")
    assert not ok and reason == "committee_too_long"
    # Committee has bill/procedural tokens
    ok, reason = passes_quality_gates(
        "Committee on Agriculture. H.R. 1234 Ordered, That pursuant to clause",
        "Mr. Smith",
    )
    assert not ok and reason == "committee_has_bill_tokens"
    # Member stopword
    for bad in ("Mr. The", "Mr. Speaker", "Mr. AYES", "Mr. House of Representatives"):
        ok, reason = passes_quality_gates("Committee on Intelligence", bad)
        assert not ok and reason == "member_stopword", f"expected member_stopword for {bad!r}"
    # Member regex fail (single letter / invalid)
    ok, reason = passes_quality_gates("Committee on Intelligence", "Mr. X")
    assert not ok and reason == "member_regex_fail"
    # Legit passes
    ok, reason = passes_quality_gates("Permanent Select Committee on Intelligence", "Mr. ROGERS of Michigan")
    assert ok and reason == ""


def test_detect_actions_h101_style():
    """H101-style bulk HLIG narrative should yield at least one appointment (committee = Intelligence)."""
    events = detect_actions_on_page(
        H101_SNIPPET, printed_page=101, congress=113, default_year=2013, page_date_override="2013-02-08"
    )
    hlig = [e for e in events if e.get("committee_raw") and "intelligence" in (e.get("committee_raw") or "").lower()]
    assert len(hlig) >= 1, f"Expected at least one HLIG appointment from H101-style text; got {events}"


def test_filename_date_fallback_is_deterministic():
    assert _guess_decision_date_from_filename(Path("weird_name.pdf")).isoformat() == "2013-01-01"


def test_parse_journal_file_closes_page_cache(monkeypatch):
    pages = [_FakePage(), _FakePage(fail=True)]
    monkeypatch.setattr(journal_parser, "pdfplumber", _FakePdfLib(pages))
    monkeypatch.setattr(journal_parser, "_extract_page_text_by_columns", lambda _page: [])

    assert journal_parser.parse_journal_file(Path("GPO-HJOURNAL-2013.pdf")) == []
    assert all(page.closed for page in pages)


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
    print("All journal parser tests passed.")
