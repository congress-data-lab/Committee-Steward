#!/usr/bin/env python3
"""
Minimal reproducible tests for surgical fixes (Fix 1-6).
Run: python tests/test_surgical_fixes.py
"""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from core.events.crec_parser import (
    parse_crec_text,
    parse_crec_terminations_from_death,
    parse_crec_terminations,
    parse_crec_terminations_from_house,
)
from core.members.resolver import _strip_suffixes_for_lookup


def run_tests():
    """Run all tests."""
    errors = []

    # Fix 1
    text = "The Speaker appointed the following to the Committee on the Budget: Mr. Smith."
    apps = parse_crec_text(text)
    if not apps or apps[0]["committee"] != "Committee on the Budget":
        errors.append(f"Fix 1: got {apps[0]['committee'] if apps else None}")
    text2 = "The Speaker appointed the following to the Committee on the Budget, during the 113th Congress: Mr. Smith."
    apps2 = parse_crec_text(text2)
    if not apps2 or apps2[0]["committee"] != "Committee on the Budget":
        errors.append(f"Fix 1 trailing: got {apps2[0]['committee'] if apps2 else None}")

    # Fix 2
    for inp, expected in [("Andy Harris, M.D.", "Andy Harris"), ("Andy Harris, Jr.", "Andy Harris")]:
        if _strip_suffixes_for_lookup(inp) != expected:
            errors.append(f"Fix 2 {inp}: got {_strip_suffixes_for_lookup(inp)}")

    # Fix 3: No cross-chamber fallback in loader
    loader_path = __import__("pathlib").Path(__file__).resolve().parent.parent / "ingest" / "load_crec_events.py"
    source = loader_path.read_text()
    if '"S" if chamber_char == "H" else "H"' in source or "other_chamber" in source:
        errors.append("Fix 3: cross-chamber fallback still present")

    # Fix 4: Death parser should preserve middle initials and full last names.
    death_text_1 = (
        "H. Res. 383. Resolution relative to the death of the Honorable Thomas S. Foley, "
        "a former Representative from the State of Washington."
    )
    parsed_1 = parse_crec_terminations_from_death(death_text_1, "2013-10-28")
    if not parsed_1 or parsed_1["member"] != "Thomas S. Foley":
        errors.append(f"Fix 4 Thomas Foley: got {parsed_1}")

    death_text_2 = (
        "The Chair lays before the Senate a Certificate of Election to fill the vacancy "
        "created by the death of Senator Frank Lautenberg of New Jersey."
    )
    parsed_2 = parse_crec_terminations_from_death(death_text_2, "2013-10-31")
    if not parsed_2 or parsed_2["member"] != "Frank Lautenberg":
        errors.append(f"Fix 4 Frank Lautenberg: got {parsed_2}")

    death_text_3 = (
        "I was deeply saddened when I learned of the passing of Senator Frank Lautenberg. "
        "I am certain that anyone who had ever met Senator Lautenberg would agree."
    )
    parsed_3 = parse_crec_terminations_from_death(death_text_3, "2013-06-06")
    if parsed_3 is not None:
        errors.append(f"Fix 4 speech fragment should not parse: got {parsed_3}")

    death_text_4 = "I learned of the passing of Senator Lautenberg earlier this week."
    parsed_4 = parse_crec_terminations_from_death(death_text_4, "2013-06-05")
    if parsed_4 is not None:
        errors.append(f"Fix 4 relative phrase should not parse: got {parsed_4}")

    death_text_5 = (
        "The House remembers the death of the Honorable William Quincy Murphy. "
        "Mr. Murphy served Augusta for decades."
    )
    parsed_5 = parse_crec_terminations_from_death(death_text_5, "2013-08-02")
    if parsed_5 is not None:
        errors.append(f"Fix 4 long memorial text should not parse: got {parsed_5}")

    # Fix 5: committee anchor should allow "House Committee on" / "Select Committee on".
    leave_text = (
        "The SPEAKER laid before the House the following request for a leave of absence as a member of the "
        "House Committee on Energy and Commerce. "
        "Sincerely,\nJohn Doe,\nMember of Congress"
    )
    leave_terms = parse_crec_terminations(leave_text, "2013-06-03")
    if not leave_terms or leave_terms[0]["committee"] != "Committee on Energy and Commerce":
        errors.append(f"Fix 5 house committee anchor: got {leave_terms}")

    select_text = (
        "Re stepping down from the Select Committee on Intelligence. "
        "Sincerely,\nJane Doe,\nMember of Congress"
    )
    select_terms = parse_crec_terminations(select_text, "2013-06-03")
    if not select_terms or select_terms[0]["committee"] != "Committee on Intelligence":
        errors.append(f"Fix 5 select committee anchor: got {select_terms}")

    # Fix 6: unified departure verbs + finality guard.
    leave_body_text = (
        "I hereby request a leave of absence from the Committee on Appropriations.\n"
        "Sincerely,\nSteve Israel,\nMember of Congress"
    )
    leave_body_terms = parse_crec_terminations(leave_body_text, "2014-01-01")
    if not leave_body_terms or leave_body_terms[0]["committee"] != "Committee on Appropriations":
        errors.append(f"Fix 6 leave-of-absence parse: got {leave_body_terms}")

    speculative_text = (
        "I may resign from the Committee on Appropriations at a later date.\n"
        "Sincerely,\nJohn Doe,\nMember of Congress"
    )
    speculative_terms = parse_crec_terminations(speculative_text, "2014-01-01")
    if speculative_terms:
        errors.append(f"Fix 6 speculative language should not parse: got {speculative_terms}")

    # Fix 7: full-House resignation generic phrasing should parse.
    house_resign_text = (
        "The Chair received a communication regarding the resignation from the House of Representatives.\n"
        "Respectfully,\n"
        "John Doe,\n"
        "U.S. Congressman"
    )
    house_resign = parse_crec_terminations_from_house(house_resign_text, "2014-01-15")
    if not house_resign or house_resign["member"] != "John Doe":
        errors.append(f"Fix 7 house resignation generic parse: got {house_resign}")

    watt_house_resign_text = (
        "The SPEAKER pro tempore laid before the House the following resignation from the House of Representatives:\n"
        "House of Representatives, Washington, DC, January 6, 2014.\n"
        "Dear Speaker Boehner: I hereby resign as a member of the United States House of Representatives.\n"
        "Sincerely,\n"
        "Melvin L. Watt,\n"
        "U.S. Congressman"
    )
    watt_house_resign = parse_crec_terminations_from_house(watt_house_resign_text, "2014-01-07")
    if not watt_house_resign or watt_house_resign["member"] != "Melvin L. Watt":
        errors.append(f"Fix 7 Watt house resignation parse: got {watt_house_resign}")

    alexander_house_resign_text = (
        "The SPEAKER pro tempore laid before the House the following resignation from the House of Representatives.\n"
        "House of Representatives, August 8, 2013.\n"
        "Sincerely,\n"
        "Rodney Alexander,\n"
        "Member of Congress"
    )
    alexander_house_resign = parse_crec_terminations_from_house(alexander_house_resign_text, "2013-09-06")
    if not alexander_house_resign or alexander_house_resign["member"] != "Rodney Alexander":
        errors.append(f"Fix 7 Alexander house resignation parse: got {alexander_house_resign}")

    # Fix 8: appointment parser should capture vacancy-caused-by replacement name.
    vacancy_text = (
        "The Speaker appoints the following to the Committee on Appropriations to fill the vacancy "
        "caused by the resignation of Mr. Steve Israel: Mr. Smith."
    )
    vacancy_apps = parse_crec_text(vacancy_text)
    if not vacancy_apps or vacancy_apps[0].get("replaced_member") != "Steve Israel":
        errors.append(f"Fix 8 vacancy replacement parse: got {vacancy_apps}")

    # Fix 9: request-to-be-relieved phrasing should parse as termination.
    relieved_text = (
        "I respectfully request that I be relieved from the Committee on the Budget.\n"
        "Sincerely,\n"
        "Jane Doe,\n"
        "Member of Congress"
    )
    relieved_terms = parse_crec_terminations(relieved_text, "2014-01-01")
    if not relieved_terms or relieved_terms[0]["committee"] != "Committee on the Budget":
        errors.append(f"Fix 9 relieved phrasing parse: got {relieved_terms}")

    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        sys.exit(1)
    print("All surgical fix tests passed.")


if __name__ == "__main__":
    run_tests()
