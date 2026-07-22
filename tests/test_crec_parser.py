from core.events.crec_parser import (
    parse_crec_terminations,
    parse_crec_terminations_from_house,
    parse_crec_text,
)


def test_parse_crec_text_handles_names_before_committee():
    text = (
        "The Speaker appointed Mr. POMPEO, Mr. HECK, and Mr. TURNER "
        "to the Permanent Select Committee on Intelligence."
    )
    rows = parse_crec_text(text)
    assert rows, "expected appointment rows"
    row = rows[0]
    assert "Permanent Select Committee on Intelligence" in row["committee"]
    normalized = {name.lower() for name in row["members"]}
    assert any("pompeo" in name for name in normalized)
    assert any("heck" in name for name in normalized)
    assert any("turner" in name for name in normalized)


def test_parse_crec_text_keeps_roster_style_extraction():
    text = (
        "The Speaker appoints the following Members to the Committee on Rules: "
        "Mr. COLE, Mr. BURGESS, and Mr. BYRNE."
    )
    rows = parse_crec_text(text)
    assert rows, "expected roster-style appointments"
    row = rows[0]
    assert "Committee on Rules" in row["committee"]
    assert len(row["members"]) >= 3


def test_parse_crec_text_splits_repeated_titled_names_without_commas():
    text = (
        "The Speaker appointed Mr. MILLER of Florida Mr. CONAWAY of Texas "
        "Mr. KING of New York to the Permanent Select Committee on Intelligence."
    )
    rows = parse_crec_text(text)
    assert rows, "expected rows for repeated titled names"
    row = rows[0]
    normalized = " ".join(row["members"]).lower()
    assert "miller" in normalized
    assert "conaway" in normalized
    assert "king" in normalized


def test_parse_crec_terminations_accepts_us_representative_signature():
    text = """
    The SPEAKER pro tempore laid before the House the following resignations
    as a member of the Committee on the Judiciary and the Committee on
    Oversight and Government Reform:
    Dear Speaker Ryan: I hereby resign my seats on the House Judiciary
    Committee and the House Committee on Oversight and Government Reform
    effective immediately.
    Sincerely,
    Jason E. Chaffetz,
    U.S. Representative,
    Utah Third Congressional District.
    """

    rows = parse_crec_terminations(text, "2017-06-27")

    assert {row["member"] for row in rows} == {"Jason E. Chaffetz"}
    assert {row["committee"] for row in rows} == {
        "Committee on the Judiciary",
        "Committee on Oversight and Government Reform",
    }


def test_parse_crec_terminations_keeps_full_name_without_neighbor_state():
    text = """
    The SPEAKER pro tempore laid before the House the following resignations
    as a member of the Committee on the Judiciary and the Committee on
    Education and the Workforce:
    Dear Speaker Ryan: I resign my seats on the House Judiciary committee and
    the Committee on Education and the Workforce.
    Sincerely,
    Michael D. Bishop.
    """

    rows = parse_crec_terminations(text, "2017-02-16")

    assert {row["member"] for row in rows} == {"Michael D. Bishop"}
    assert len(rows) == 2


def test_parse_crec_terminations_splits_repeated_committee_labels():
    text = """
    The SPEAKER pro tempore laid before the House the following resignations as a member of the
    Committee on Education and Labor, Committee on Veterans' Affairs, and Committee on Foreign Affairs:
    Dear Speaker Pelosi: I write to respectfully tender my temporary resignation as a member of the
    House Committee on Education and Labor, House Veteran Affairs Committee, and House Foreign Affairs Committee.
    Sincerely,
    Steve Watkins,
    Member of Congress.
    The SPEAKER pro tempore. Without objection, the resignations are accepted.
    """

    rows = parse_crec_terminations(text, "2020-07-20")

    assert {row["member"] for row in rows} == {"Steve Watkins"}
    assert {row["committee"] for row in rows} == {
        "Committee on Education and Labor",
        "Committee on Veterans' Affairs",
        "Committee on Foreign Affairs",
    }


def test_parse_crec_terminations_accepts_nonstandard_signature_closing():
    text = """
    The SPEAKER pro tempore laid before the House the following resignation as a member of the
    Committee on Homeland Security:
    Dear Speaker Pelosi: I write to respectfully tender my resignation as a member of the House
    Committee on Homeland Security. It has been an honor to serve in this capacity.
    Semper Fidelis,
    Van Taylor,
    Member of Congress.
    The SPEAKER pro tempore. Without objection, the resignation is accepted.
    """

    rows = parse_crec_terminations(text, "2020-01-15")

    assert rows == [
        {
            "committee": "Committee on Homeland Security",
            "member": "Van Taylor",
            "effective_date": "2020-01-15",
        }
    ]


def test_house_resignation_prefers_explicit_future_effective_date():
    text = """
    The SPEAKER pro tempore laid before the House the following resignation from the House of Representatives:
    House of Representatives, Washington, DC, January 2, 2024.
    Dear Speaker Johnson, I hereby submit my resignation, effective at the end of the day,
    January 21, 2024, as United States Representative of Ohio's 6th Congressional District.
    Sincerely,
    Bill Johnson,
    Member of Congress.
    """

    row = parse_crec_terminations_from_house(text, "2024-01-09")

    assert row == {"member": "Bill Johnson", "effective_date": "2024-01-21"}


def test_house_resignation_handles_effective_calendar_day_on_date():
    text = """
    The SPEAKER pro tempore laid before the House the following resignation from the House of Representatives:
    House of Representatives, Washington, DC, March 12, 2024.
    Speaker Johnson, I hereby submit my resignation, effective at the end of the calendar day on
    March 22, 2024, as the United States Representative for the Fourth District of Colorado.
    Sincerely,
    Ken Buck,
    Member of Congress.
    """

    row = parse_crec_terminations_from_house(text, "2024-03-12")

    assert row == {"member": "Ken Buck", "effective_date": "2024-03-22"}


def test_house_resignation_handles_ordinal_effective_date():
    text = """
    The SPEAKER pro tempore laid before the House the following resignation from the House of Representatives:
    House of Representatives, Washington, DC, January 31, 2024.
    Dear Mr. Speaker: I have tendered my resignation as the Representative in Congress,
    effective at the end of the calendar day on February 2nd, 2024.
    Sincerely,
    Brian Higgins.
    """

    row = parse_crec_terminations_from_house(text, "2024-02-01")

    assert row == {"member": "Brian Higgins", "effective_date": "2024-02-02"}
