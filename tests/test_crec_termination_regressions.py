#!/usr/bin/env python3
"""
Regression fixtures for CREC committee termination parsing.
Run: python tests/test_crec_termination_regressions.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.committees.resolver import build_committee_index, resolve_from_index
from core.events.crec_parser import (
    parse_crec_terminations,
    parse_crec_terminations_from_death,
    parse_crec_terminations_from_house,
    parse_crec_terminations_from_senate,
)


def run_tests() -> None:
    project_root = Path(__file__).resolve().parent.parent
    idx = build_committee_index(
        [
            project_root / "data" / "reference" / "committees-current.yaml",
            project_root / "data" / "reference" / "committees-historical.yaml",
        ]
    )

    fixtures = [
        {
            "id": "israel_procedural_frame_leave_absence",
            "header_date": "2013-01-14",
            "expected_code": "HSAP",
            "expected_effective_date": "2013-01-14",
            "text": (
                "The SPEAKER pro tempore laid before the House the following resignation as a member of the "
                "Committee on Appropriations:\n"
                "Dear Speaker Boehner: I respectfully request a leave of absence from the Appropriations "
                "Committee in the 113th Congress, effective today.\n"
                "Sincerely,\n"
                "Steve Israel,\n"
                "Member of Congress.\n"
                "The SPEAKER pro tempore. Without objection, the resignation is accepted."
            ),
        },
        {
            "id": "southerland_procedural_frame_stepping_down",
            "header_date": "2013-02-26",
            "expected_code": "HSAG",
            "expected_effective_date": "2013-02-26",
            "text": (
                "The SPEAKER pro tempore laid before the House the following resignation as a member of the "
                "Committee on Agriculture:\n"
                "House of Representatives, Washington, DC, February 26, 2013.\n"
                "Dear Speaker Boehner: This letter is to notify you of my interest in stepping down from the "
                "House Committee on Agriculture.\n"
                "Sincerely,\n"
                "Steve Southerland,\n"
                "Member of Congress.\n"
                "The SPEAKER pro tempore. Without objection, the resignation is accepted."
            ),
        },
        {
            "id": "leave_absence_committee_on",
            "header_date": "2014-04-01",
            "expected_code": "HSAP",
            "expected_effective_date": "2014-04-28",
            "text": (
                "Washington, DC, April 28, 2014.\n"
                "Dear Mr. Speaker: I hereby request a leave of absence from the Committee on Appropriations.\n"
                "Sincerely,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "take_leave_from_my_seat",
            "header_date": "2013-01-14",
            "expected_code": "HSBU",
            "expected_effective_date": "2013-01-14",
            "text": (
                "Dear Speaker Boehner: In order to join another panel, I hereby take a leave of absence "
                "from my seat on the Committee on the Budget.\n"
                "Sincerely,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "stepping_down_house_committee",
            "header_date": "2013-05-10",
            "expected_code": "HSIF",
            "expected_effective_date": "2013-05-10",
            "text": (
                "I am submitting this letter to notify you that I am stepping down from the House Committee "
                "on Energy and Commerce.\n"
                "Respectfully,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "stepping_down_clause_trim_boundary",
            "header_date": "2013-05-10",
            "expected_code": "HSAG",
            "expected_effective_date": "2013-05-10",
            "text": (
                "I am submitting this letter to notify you that I am stepping down from the House Committee "
                "on Agriculture so that I can dedicate additional focus to district priorities.\n"
                "Respectfully,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "resign_from_appointment_to_committee",
            "header_date": "2013-06-03",
            "expected_code": "HSII",
            "expected_effective_date": "2013-06-03",
            "text": (
                "Dear Speaker: I hereby resign from my appointment to the Committee on Natural Resources.\n"
                "Sincerely,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "written_notice_resignation_from_appointment",
            "header_date": "2013-09-30",
            "expected_code": "HSPW",
            "expected_effective_date": "2013-09-30",
            "text": (
                "Please accept this written notice of my resignation from my appointment to the Committee on "
                "Transportation and Infrastructure.\n"
                "Sincerely,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "appropriations_committee_suffix_style",
            "header_date": "2013-10-15",
            "expected_code": "HSAP",
            "expected_effective_date": "2013-10-15",
            "text": (
                "I am stepping down from the Appropriations Committee.\n"
                "Sincerely,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "leave_absence_assignment_variant",
            "header_date": "2014-02-21",
            "expected_code": "HSJU",
            "expected_effective_date": "2014-02-21",
            "text": (
                "I request a leave of absence from my assignment on the Committee on the Judiciary.\n"
                "Best Regards,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "leave_absence_appropriations_with_congress_suffix",
            "header_date": "2013-01-14",
            "expected_code": "HSAP",
            "expected_effective_date": "2013-01-14",
            "text": (
                "Dear Speaker Boehner: I respectfully request a leave of absence from the Appropriations "
                "Committee in the 113th Congress, effective today.\n"
                "Sincerely,\n"
                "Steve Israel,\n"
                "Member of Congress"
            ),
        },
        {
            "id": "inline_floor_termination_no_re_subject",
            "header_date": "2014-05-29",
            "expected_code": "HSSM",
            "expected_effective_date": "2014-05-29",
            "text": (
                "Mr. Speaker, I hereby resign from the Committee on Small Business, effective immediately.\n"
                "Sincerely,\n"
                "Jane Member,\n"
                "Member of Congress"
            ),
        },
    ]
    multi_committee_fixture = {
        "id": "multi_committee_resignations_known_frame",
        "header_date": "2013-01-14",
        "expected_codes": {"HSJU", "HSGO"},
        "text": (
            "The SPEAKER pro tempore laid before the House the following resignations as a member of the "
            "Committees on the Judiciary and Oversight and Government Reform:\n"
            "Dear Speaker Boehner: In order to join the Committee on Appropriations, I hereby take a leave of "
            "absence from my seat on the Committee on the Judiciary, effective today.\n"
            "Sincerely,\n"
            "Mike Quigley,\n"
            "Member of Congress.\n"
            "The SPEAKER pro tempore. Without objection, the resignations are accepted."
        ),
    }
    negative_fixtures = [
        {
            "id": "committee_of_whole_excluded",
            "header_date": "2014-06-01",
            "text": "I step down from the Committee of the Whole House on the state of the Union.",
        },
        {
            "id": "bill_referral_excluded",
            "header_date": "2014-06-01",
            "text": "H.R. 999 was referred to the Committee on Appropriations and the Committee on the Budget.",
        },
    ]

    errors: list[str] = []
    for fx in fixtures:
        terms = parse_crec_terminations(fx["text"], fx["header_date"])
        if not terms:
            errors.append(f"{fx['id']}: no terminations parsed")
            continue

        matched = False
        for term in terms:
            try:
                code = resolve_from_index(term["committee"], 113, idx, "house")
            except Exception as exc:  # pragma: no cover - diagnostic only
                errors.append(f"{fx['id']}: resolver failed for '{term['committee']}': {exc}")
                continue
            if code == fx["expected_code"] and term["effective_date"] == fx["expected_effective_date"]:
                matched = True
                break

        if not matched:
            errors.append(f"{fx['id']}: parsed={terms}")

    multi_terms = parse_crec_terminations(
        multi_committee_fixture["text"],
        multi_committee_fixture["header_date"],
    )
    if not multi_terms:
        errors.append(f"{multi_committee_fixture['id']}: no terminations parsed")
    else:
        resolved_codes = set()
        for term in multi_terms:
            try:
                resolved_codes.add(resolve_from_index(term["committee"], 113, idx, "house"))
            except Exception as exc:  # pragma: no cover - diagnostic only
                errors.append(
                    f"{multi_committee_fixture['id']}: resolver failed for '{term['committee']}': {exc}"
                )
        if not multi_committee_fixture["expected_codes"].issubset(resolved_codes):
            errors.append(
                f"{multi_committee_fixture['id']}: resolved_codes={sorted(resolved_codes)}"
            )

    for fx in negative_fixtures:
        terms = parse_crec_terminations(fx["text"], fx["header_date"])
        if terms:
            errors.append(f"{fx['id']}: expected no parse, got {terms}")

    # Quigley known page: loader-style widened context should surface both HSJU + HSGO.
    quigley_file = project_root / "data" / "crec" / "2013" / "CREC-2013-01-14" / "json" / "CREC-2013-01-14-pt1-PgH64-8.json"
    qdata = json.loads(quigley_file.read_text())
    qitems = qdata.get("content", [])
    if len(qitems) > 2:
        qidx = 2  # letter body item
        qtext = qitems[qidx].get("text", "")
        qbase = parse_crec_terminations(qtext, "2013-01-14")
        qstart = max(0, qidx - 2)
        qend = min(len(qitems) - 1, qidx + 2)
        qctx = "\n".join((qitems[i].get("text", "") or "") for i in range(qstart, qend + 1))
        qctx_terms = parse_crec_terminations(qctx, "2013-01-14")
        qmerged = []
        qseen = set()
        for term in qctx_terms + qbase:
            key = (term["member"].lower(), term["committee"].lower(), term["effective_date"])
            if key in qseen:
                continue
            qseen.add(key)
            qmerged.append(term)
        resolved = set()
        for term in qmerged:
            try:
                resolved.add(resolve_from_index(term["committee"], 113, idx, "house"))
            except Exception:
                pass
        if not {"HSJU", "HSGO"}.issubset(resolved):
            errors.append(f"quigley_loader_context: resolved_codes={sorted(resolved)} terms={qmerged}")

    # Watt known page: district-style signature should resolve member in from_house parser.
    watt_file = project_root / "data" / "crec" / "2014" / "CREC-2014-01-07" / "json" / "CREC-2014-01-07-pt1-PgH3-5.json"
    wdata = json.loads(watt_file.read_text())
    witems = wdata.get("content", [])
    wtext = "\n".join((it.get("text", "") or "") for it in witems[:2])
    whouse = parse_crec_terminations_from_house(wtext, "2014-01-07")
    if not whouse or whouse.get("member") != "Melvin L. Watt":
        errors.append(f"watt_district_signature: got {whouse}")

    # Senate death resolution (S.Res. 160, Lautenberg): parser must return member and effective_date.
    lautenberg_death_text = (
        "S. Res. 160\n"
        "Whereas the Senate has heard with profound sorrow of the death of Senator Frank R. Lautenberg;\n"
        "Whereas Frank R. Lautenberg ... (resolution text) ...\n"
        "Resolved, That the Senate has heard with profound sorrow ...\n"
        "The resolution was agreed to."
    )
    lautenberg_death = parse_crec_terminations_from_death(lautenberg_death_text, "2013-06-04")
    if not lautenberg_death:
        errors.append("lautenberg_senate_death_resolution: no parse from death parser")
    elif lautenberg_death.get("member") != "Frank R. Lautenberg":
        errors.append(
            f"lautenberg_senate_death_resolution: expected member 'Frank R. Lautenberg', got {lautenberg_death.get('member')!r}"
        )

    # Senate death "FIRST, LAST of State" (CREC often uses comma before state): must capture full name, not "FRANK R".
    lautenberg_comma_state_text = (
        "Whereas the Senate has heard with profound sorrow of the death of Senator FRANK R. LAUTENBERG, LAUTENBERG of New Jersey;"
    )
    # "Name, of State" (e.g. "FRANK R. LAUTENBERG, of New Jersey"): capture full name before comma.
    lautenberg_name_of_state_text = (
        "Whereas the Senate has heard with profound sorrow of the death of Senator FRANK R. LAUTENBERG, of New Jersey;"
    )
    lautenberg_comma = parse_crec_terminations_from_death(lautenberg_comma_state_text, "2013-06-04")
    if not lautenberg_comma:
        errors.append("lautenberg_comma_state: no parse from death parser")
    elif "lautenberg" not in lautenberg_comma.get("member", "").lower():
        errors.append(
            f"lautenberg_comma_state: expected full name including 'Lautenberg', got {lautenberg_comma.get('member')!r}"
        )
    lautenberg_name_of = parse_crec_terminations_from_death(lautenberg_name_of_state_text, "2013-06-04")
    if not lautenberg_name_of:
        errors.append("lautenberg_name_of_state: no parse from death parser")
    elif "lautenberg" not in lautenberg_name_of.get("member", "").lower():
        errors.append(
            f"lautenberg_name_of_state: expected full name including 'Lautenberg', got {lautenberg_name_of.get('member')!r}"
        )
    # Truncated split (CREC item 0 ends with "FRANK R. ") must return None so next item (full resolution) is used.
    truncated_header_text = "SENATE RESOLUTION 161--RELATIVE TO THE DEATH OF THE HONORABLE FRANK R. "
    truncated_parse = parse_crec_terminations_from_death(truncated_header_text, "2013-06-04")
    if truncated_parse is not None:
        errors.append(
            f"lautenberg_truncated_split: expected None (truncated 'FRANK R.'), got {truncated_parse.get('member')!r}"
        )

    # Senate vacancy by death (115th: McCain).
    mccain_death_vacancy = "The certificate of appointment to fill the vacancy caused by the death of Senator John McCain of Arizona was read."
    mccain = parse_crec_terminations_from_senate(mccain_death_vacancy, "2018-09-05")
    if not mccain:
        errors.append("mccain_senate_death_vacancy: no parse from Senate parser")
    elif "mccain" not in mccain.get("member", "").lower():
        errors.append(
            f"mccain_senate_death_vacancy: expected member containing 'McCain', got {mccain.get('member')!r}"
        )
    elif mccain.get("effective_date") != "2018-09-05":
        errors.append(
            f"mccain_senate_death_vacancy: expected effective_date from header, got {mccain.get('effective_date')!r}"
        )

    # Senate vacancy by resignation (115th: Sessions).
    sessions_resignation = "Certificate of appointment to fill the vacancy created by the resignation of Senator Jeff Sessions of Alabama."
    sessions = parse_crec_terminations_from_senate(sessions_resignation, "2017-02-09")
    if not sessions:
        errors.append("sessions_senate_resignation_vacancy: no parse from Senate parser")
    elif "sessions" not in sessions.get("member", "").lower():
        errors.append(
            f"sessions_senate_resignation_vacancy: expected member containing 'Sessions', got {sessions.get('member')!r}"
        )

    # Letters of resignation from former Senator (115th: Cochran).
    cochran_letters = "The PRESIDENT laid before the Senate letters of resignation from former Senator Thad Cochran of Mississippi."
    cochran = parse_crec_terminations_from_senate(cochran_letters, "2018-04-09")
    if not cochran:
        errors.append("cochran_senate_letters_resignation: no parse from Senate parser")
    elif "cochran" not in cochran.get("member", "").lower():
        errors.append(
            f"cochran_senate_letters_resignation: expected member containing 'Cochran', got {cochran.get('member')!r}"
        )

    # Letters of resignation from former Senator (115th: Kyl), optional "of Arizona".
    kyl_letters = "The PRESIDENT laid before the Senate letters of resignation from former Senator Jon Kyl of Arizona."
    kyl = parse_crec_terminations_from_senate(kyl_letters, "2018-12-31")
    if not kyl:
        errors.append("kyl_senate_letters_resignation: no parse from Senate parser")
    elif "kyl" not in kyl.get("member", "").lower():
        errors.append(
            f"kyl_senate_letters_resignation: expected member containing 'Kyl', got {kyl.get('member')!r}"
        )

    # Vacancy caused by death (no certificate prefix).
    mccain_plain = "to fill the vacancy caused by the death of Senator John McCain of Arizona"
    mccain_plain_result = parse_crec_terminations_from_senate(mccain_plain, "2018-09-06")
    if not mccain_plain_result:
        errors.append("mccain_plain_death_vacancy: no parse from Senate parser")
    elif "mccain" not in mccain_plain_result.get("member", "").lower():
        errors.append(
            f"mccain_plain_death_vacancy: expected member containing 'McCain', got {mccain_plain_result.get('member')!r}"
        )

    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        sys.exit(1)
    print("All CREC termination regression fixtures passed.")


if __name__ == "__main__":
    run_tests()
