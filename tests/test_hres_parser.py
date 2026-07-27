from pathlib import Path

from core.events.hres_parser import parse_hres_xml
from core.committees.resolver import committee_name_to_id


def test_hres_strips_when_sworn_parenthetical(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form>
    <legis-num>H. RES. 7</legis-num>
    <action>
      <action-date date="20150106">January 6, 2015</action-date>
    </action>
  </form>
  <resolution-body>
    <section>
      <committee-appointment-paragraph>
        <header>Committee on Financial Services:</header>
        <text>Ms. Waters (when sworn); Mr. Meeks (when sworn); and Mr. Capuano.</text>
      </committee-appointment-paragraph>
    </section>
  </resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-114hres7eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))
    assert len(rows) == 1
    members = rows[0]["members"]

    assert "Ms. Waters" in members
    assert "Mr. Meeks" in members
    assert "Mr. Capuano" in members
    assert all("when sworn" not in m.lower() for m in members)


def test_hres_recovers_historical_journal_backstop_misses(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form>
    <legis-num>H. RES. 39</legis-num>
    <action>
      <action-date date="20150121">January 21, 2015</action-date>
    </action>
  </form>
  <resolution-body>
    <section>
      <committee-appointment-paragraph>
        <header>Committee on Appropriations:</header>
        <text>Mr. Nunnelee to rank immediately after Mr. Womack.</text>
      </committee-appointment-paragraph>
      <committee-appointment-paragraph>
        <header>Committee on Energy and Commerce:</header>
        <text>Mr. Tonko (when sworn).</text>
      </committee-appointment-paragraph>
      <committee-appointment-paragraph>
        <header>Committee on Ways and Means:</header>
        <text>Mr. Rangel (when sworn).</text>
      </committee-appointment-paragraph>
    </section>
  </resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-114hres39eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))

    assert [(row["committee"], row["members"]) for row in rows] == [
        ("Committee on Appropriations", ["Mr. Nunnelee"]),
        ("Committee on Energy and Commerce", ["Mr. Tonko"]),
        ("Committee on Ways and Means", ["Mr. Rangel"]),
    ]
    assert rows[0]["member_observations"] == [
        {
            "name": "Mr. Nunnelee",
            "source_ordinal": 1,
            "rank_after": "Mr. Womack",
        }
    ]


def test_hres_preserves_list_order_and_inline_rank_anchors(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 82</legis-num><action>
    <action-date date="20130226">February 26, 2013</action-date>
  </action></form>
  <resolution-body><section><committee-appointment-paragraph>
    <header>Committee on the Budget:</header>
    <text>Mr. Price of Georgia, to rank immediately after Mr. Cole; Mrs. Black, to rank immediately after Mr. Lankford; and Mr. Duffy.</text>
  </committee-appointment-paragraph></section></resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-113hres82eh.xml"
    path.write_text(xml, encoding="utf-8")

    row = list(parse_hres_xml(path))[0]

    assert row["members"] == ["Mr. Price of Georgia", "Mrs. Black", "Mr. Duffy"]
    assert row["member_observations"] == [
        {
            "name": "Mr. Price of Georgia",
            "source_ordinal": 1,
            "rank_after": "Mr. Cole",
        },
        {
            "name": "Mrs. Black",
            "source_ordinal": 2,
            "rank_after": "Mr. Lankford",
        },
        {"name": "Mr. Duffy", "source_ordinal": 3, "rank_after": None},
    ]


def test_hres_603_bare_after_reranks_without_phantom_members(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 603</legis-num><action>
    <action-date date="20160204">February 4, 2016</action-date>
  </action></form>
  <resolution-body><section><committee-appointment-paragraph>
    <header>Committee on Small Business:</header>
    <text>Mr. Takai, after Mrs. Lawrence; and Ms. Adams, after Ms. Clarke of New York.</text>
  </committee-appointment-paragraph></section></resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-114hres603eh.xml"
    path.write_text(xml, encoding="utf-8")

    row = list(parse_hres_xml(path))[0]

    assert row["members"] == ["Mr. Takai", "Ms. Adams"]
    assert row["member_observations"] == [
        {"name": "Mr. Takai", "source_ordinal": 1, "rank_after": "Mrs. Lawrence"},
        {"name": "Ms. Adams", "source_ordinal": 2, "rank_after": "Ms. Clarke of New York"},
    ]


def test_hres_normalizes_commitee_typo(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form>
    <legis-num>H. RES. 199</legis-num>
    <action>
      <action-date date="20150414">April 14, 2015</action-date>
    </action>
  </form>
  <resolution-body>
    <section>
      <committee-appointment-paragraph>
        <header>Commitee on Rules:</header>
        <text>Mr. Byrne and Mr. Newhouse.</text>
      </committee-appointment-paragraph>
    </section>
  </resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-114hres199eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))
    assert len(rows) == 1
    assert rows[0]["committee"] == "Committee on Rules"
    assert rows[0]["members"] == ["Mr. Byrne", "Mr. Newhouse"]


def test_hres_normalizes_comittee_typo(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 56</legis-num><action>
    <action-date date="20230124">January 24, 2023</action-date>
  </action></form>
  <resolution-body><section><committee-appointment-paragraph>
    <header>Comittee on Appropriations:</header>
    <text>Mr. Aderholt and Mr. Amodei.</text>
  </committee-appointment-paragraph></section></resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-118hres56eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))
    assert rows[0]["committee"] == "Committee on Appropriations"
    assert committee_name_to_id(rows[0]["committee"], "hres") == "HSAP"


def test_committee_of_ways_and_means_resolves() -> None:
    assert committee_name_to_id("Committee of Ways and Means", bill_type="hres") == "HSWM"


def test_hres_appointment_records_carry_action(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 7</legis-num><action>
    <action-date date="20230109">January 9, 2023</action-date>
  </action></form>
  <resolution-body><section>
    <text>That the following named Members be elected to the following standing committee:</text>
    <committee-appointment-paragraph>
      <header>Committee on Rules:</header><text>Mr. Burgess and Mr. Reschenthaler.</text>
    </committee-appointment-paragraph>
  </section></resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-118hres7eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))

    assert len(rows) == 1
    assert rows[0]["action"] == "APPOINTED"


def test_hres_117_72_records_greene_removals(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 72</legis-num><action>
    <action-date date="20210204">February 4, 2021</action-date>
  </action></form>
  <resolution-body><section>
    <text>That the following named Member be, and is hereby, removed from the following standing committees of the House of Representatives:</text>
    <committee-appointment-paragraph>
      <header>Committee on the Budget:</header><text>Mrs. Greene of Georgia.</text>
    </committee-appointment-paragraph>
    <committee-appointment-paragraph>
      <header>Committee on Education and Labor:</header><text>Mrs. Greene of Georgia.</text>
    </committee-appointment-paragraph>
  </section></resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-117hres72eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))

    assert [(row["action"], row["committee"], row["members"]) for row in rows] == [
        ("REMOVED", "Committee on the Budget", ["Mrs. Greene of Georgia"]),
        ("REMOVED", "Committee on Education and Labor", ["Mrs. Greene of Georgia"]),
    ]


def test_hres_118_76_records_omar_removal(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 76</legis-num><action>
    <action-date date="20230202">February 2, 2023</action-date>
  </action></form>
  <resolution-body><section>
    <text>That the following named Member be, and is hereby, removed from the following standing committee of the House of Representatives:</text>
    <committee-appointment-paragraph>
      <header>Committee on Foreign Affairs:</header><text>Ms. Omar.</text>
    </committee-appointment-paragraph>
  </section></resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-118hres76eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))

    assert len(rows) == 1
    assert rows[0]["action"] == "REMOVED"
    assert rows[0]["committee"] == "Committee on Foreign Affairs"
    assert rows[0]["members"] == ["Ms. Omar"]


def test_hres_prose_removal_records_each_committee(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 789</legis-num><action>
    <action-date date="20211117">November 17, 2021</action-date>
  </action></form>
  <resolution-body><section>
    <paragraph><enum>(1)</enum><text>Representative Paul Gosar of Arizona be censured;</text></paragraph>
    <paragraph><enum>(4)</enum><text>Representative Paul Gosar be, and is hereby, removed from the Committee on Natural Resources and the Committee on Oversight and Reform.</text></paragraph>
  </section></resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-117hres789eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))

    assert [(row["action"], row["committee"], row["members"]) for row in rows] == [
        ("REMOVED", "Committee on Natural Resources", ["Representative Paul Gosar"]),
        ("REMOVED", "Committee on Oversight and Reform", ["Representative Paul Gosar"]),
    ]


def test_hres_unrelated_removal_text_does_not_change_appointment_action(
    tmp_path: Path,
) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 100</legis-num><action>
    <action-date date="20230202">February 2, 2023</action-date>
  </action></form>
  <resolution-body>
    <section>
      <text>That the following named Member be elected to the following standing committee:</text>
      <committee-appointment-paragraph>
        <header>Committee on Rules:</header><text>Mr. Burgess.</text>
      </committee-appointment-paragraph>
    </section>
    <section><text>A restriction elsewhere in this resolution is removed.</text></section>
  </resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-118hres100eh.xml"
    path.write_text(xml, encoding="utf-8")

    rows = list(parse_hres_xml(path))

    assert len(rows) == 1
    assert rows[0]["action"] == "APPOINTED"
