from pathlib import Path

from core.events.sres_parser import parse_sres_xml


def test_sres_preserves_member_source_order(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<resolution>
  <form><legis-num>S. RES. 17</legis-num><action>
    <action-date date="20130124">January 24, 2013</action-date>
  </action></form>
  <resolution-body><section><committee-appointment-paragraph>
    <header>Committee on Agriculture, Nutrition, and Forestry:</header>
    <text>Ms. Stabenow (MI); Mr. Leahy (VT); and Mr. Harkin (IA).</text>
  </committee-appointment-paragraph></section></resolution-body>
</resolution>
"""
    path = tmp_path / "BILLS-113sres17ats.xml"
    path.write_text(xml, encoding="utf-8")

    row = list(parse_sres_xml(path))[0]

    assert row["member_observations"] == [
        {"name": "Stabenow (MI)", "source_ordinal": 1, "rank_after": None},
        {"name": "Leahy (VT)", "source_ordinal": 2, "rank_after": None},
        {"name": "Harkin (IA)", "source_ordinal": 3, "rank_after": None},
    ]
