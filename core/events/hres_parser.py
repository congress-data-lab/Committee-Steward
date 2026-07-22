"""
Parse H.Res. XML files to extract committee appointment data.

Handles four structural patterns:
- Pattern A: committee-appointment-paragraph with header + text (semicolon-separated names)
- Pattern B: subsection with header + paragraph elements (one name per paragraph)
- Pattern C: section > paragraph with header + text (comma-separated names, e.g. H.Res. 7)
- Pattern D: prose removal paragraph naming one member and one or more committees
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Iterator

DC_NS = "http://purl.org/dc/elements/1.1/"


def _get_text(elem: ET.Element | None) -> str:
    """Recursively extract text from element and its children."""
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def _strip_rank_instruction(name: str) -> str:
    """Remove parenthetical or inline committee-ranking instructions."""
    cleaned = re.sub(
        r"\s*\([^)]*\bto\s+rank\b[^)]*\)\s*",
        " ",
        name,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\s+\bto\s+rank\b.*$", "", cleaned, flags=re.IGNORECASE
    ).strip()


def _strip_when_sworn_parenthetical(name: str) -> str:
    """Remove '(when sworn)' qualifiers that are not part of the member identity."""
    return re.sub(r"\s*\([^)]*when\s+sworn[^)]*\)\s*", " ", name, flags=re.IGNORECASE).strip()


def _parse_members_from_text(text: str) -> list[str]:
    """Split member text by semicolons, commas, and 'and'; clean each name."""
    if not text:
        return []
    # Normalize " and " to comma for uniform splitting (handles both formats)
    text = re.sub(r"\s+and\s+", ", ", text, flags=re.IGNORECASE)
    # Split by semicolon or comma (Pattern A uses ";", Pattern C uses ",")
    parts = re.split(r"\s*[;,]\s*", text)
    names = []
    for p in parts:
        p = p.strip().strip(".,")
        p = _strip_rank_instruction(p)
        p = _strip_when_sworn_parenthetical(p)
        # Skip "to rank immediately after Mr. X" fragments (comma-separated, not parenthetical)
        if p.lower().startswith("to rank"):
            continue
        if len(p) > 2 and (" " in p or "." in p):
            names.append(p)
    return names


MEMBER_START_RE = re.compile(r"(?i)\b(?:Mr\.?|Mrs\.?|Ms\.?|Miss)\s+")
RANK_AFTER_RE = re.compile(r"(?i)\bto\s+rank\s+immediately\s+after\s+")


def _clean_observation_token(value: str) -> str:
    """Remove list punctuation and non-identity appointment qualifiers."""
    cleaned = re.sub(r"(?i)^\s*and\s+", "", value)
    cleaned = re.sub(r"(?i)[,;]\s*and\s*$", "", cleaned)
    cleaned = cleaned.strip(" \t\r\n,;().")
    cleaned = _strip_when_sworn_parenthetical(cleaned)
    return cleaned.strip(" \t\r\n,;().")


def _rank_observations_from_text(text: str) -> list[dict]:
    """Return source-order slots while retaining explicit predecessor instructions."""
    matches = list(MEMBER_START_RE.finditer(text))
    main_starts: list[int] = []
    for match in matches:
        prefix = text[max(0, match.start() - 48) : match.start()]
        if re.search(r"(?i)to\s+rank\s+immediately\s+after\s*$", prefix):
            continue
        main_starts.append(match.start())

    observations: list[dict] = []
    for index, start in enumerate(main_starts):
        end = main_starts[index + 1] if index + 1 < len(main_starts) else len(text)
        segment = text[start:end]
        rank_match = RANK_AFTER_RE.search(segment)
        if rank_match:
            raw_name = segment[: rank_match.start()]
            raw_anchor = segment[rank_match.end() :]
            name = _clean_observation_token(raw_name)
            anchor = _clean_observation_token(raw_anchor)
        else:
            name = _clean_observation_token(segment)
            anchor = None
        if name:
            observations.append(
                {
                    "name": name,
                    "source_ordinal": len(observations) + 1,
                    "rank_after": anchor or None,
                }
            )
    return observations


def _member_observations(text: str, members: list[str]) -> list[dict]:
    """Align richer source observations to the established member parser output."""
    parsed = _rank_observations_from_text(text)
    parsed_by_name = {item["name"]: item for item in parsed}
    return [
        {
            "name": name,
            "source_ordinal": ordinal,
            "rank_after": parsed_by_name.get(name, {}).get("rank_after"),
        }
        for ordinal, name in enumerate(members, start=1)
    ]


def _parse_header(header_elem: ET.Element | None) -> str:
    """Extract committee name from header, strip trailing colon."""
    raw = _get_text(header_elem)
    cleaned = raw.rstrip(":").strip()
    # Some source XMLs contain "Commitee on ..." typo; normalize to canonical form.
    cleaned = re.sub(r"(?i)^com{1,2}it{1,2}ee\s+on\s+", "Committee on ", cleaned)
    return cleaned


def _parse_action_date(action_date_elem: ET.Element | None) -> str | None:
    """Parse action-date, preferring visible date text when available."""
    if action_date_elem is None:
        return None
    text = _get_text(action_date_elem)
    if text:
        m = re.search(
            r"\b("
            r"January|February|March|April|May|June|July|August|September|October|November|December"
            r")\s+(\d{1,2}),\s+(\d{4})\b",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            parsed = f"{m.group(1)} {m.group(2)}, {m.group(3)}"
            try:
                return datetime.strptime(parsed, "%B %d, %Y").date().isoformat()
            except ValueError:
                pass
    date_attr = (action_date_elem.get("date") or "").strip()
    if re.fullmatch(r"\d{8}", date_attr):
        return f"{date_attr[:4]}-{date_attr[4:6]}-{date_attr[6:8]}"
    return None


def _parse_context_action(
    elem: ET.Element,
    parent_map: dict[ET.Element, ET.Element],
    root: ET.Element,
) -> str:
    """Classify the committee action from its nearest operative section."""
    ancestor = elem
    while ancestor is not root:
        if ancestor.tag in {"section", "subsection"}:
            operative_text = " ".join(
                _get_text(text_elem) for text_elem in ancestor.findall("./text")
            )
            if re.search(
                r"\bremov(?:e|ed|ing)\b", operative_text, flags=re.IGNORECASE
            ):
                return "REMOVED"
        ancestor = parent_map.get(ancestor, root)

    # Some renditions omit an operative section text but retain an official title.
    official_title = _get_text(root.find(".//official-title"))
    if re.search(r"\bremov(?:e|ed|ing)\b", official_title, flags=re.IGNORECASE):
        return "REMOVED"
    return "APPOINTED"


PROSE_REMOVAL_RE = re.compile(
    r"^(?P<member>.+?)\s+be,\s+and\s+is\s+hereby,\s+removed\s+from\s+"
    r"(?P<committees>(?:the\s+)?Committee\s+on\s+.+?)\.?$",
    flags=re.IGNORECASE,
)


def _parse_prose_removal(text: str) -> tuple[str, list[str]] | None:
    """Parse a member and repeated committee references from a prose removal."""
    normalized = re.sub(r"\s+", " ", text).strip()
    match = PROSE_REMOVAL_RE.match(normalized)
    if not match:
        return None

    member = match.group("member").strip(" ,.;")
    raw_committees = re.sub(
        r"(?i)^(?:the\s+)?Committee\s+on\s+",
        "",
        match.group("committees").strip(" ,.;"),
    )
    committee_parts = re.split(
        r"\s+and\s+(?:the\s+)?Committee\s+on\s+",
        raw_committees,
        flags=re.IGNORECASE,
    )
    committees = [
        f"Committee on {part.strip(' ,.;')}"
        for part in committee_parts
        if part.strip(" ,.;")
    ]
    if not member or not committees:
        return None
    return member, committees


def parse_hres_xml(path: Path) -> Iterator[dict]:
    """
    Parse an H.Res. XML file and yield appointment records.

    Yields dicts with: event_date, citation, action, committee, members
    """
    tree = ET.parse(path)
    root = tree.getroot()
    parent_map = {child: parent for parent in root.iter() for child in parent}

    # Event date from action-date
    action_date = root.find(".//action-date")
    event_date = _parse_action_date(action_date)

    # Citation from legis-num
    legis_num = root.find(".//legis-num")
    citation = _get_text(legis_num) if legis_num is not None else ""

    if not event_date:
        return

    # Pattern A: committee-appointment-paragraph
    for cap in root.iter("committee-appointment-paragraph"):
        header = cap.find("header")
        text_elem = cap.find("text")
        committee = _parse_header(header)
        if not committee:
            continue
        members_text = _get_text(text_elem)
        members = _parse_members_from_text(members_text)
        if members:
            action = _parse_context_action(cap, parent_map, root)
            yield {
                "event_date": event_date,
                "citation": citation,
                "action": action,
                "committee": committee,
                "members": members,
                "member_observations": _member_observations(members_text, members),
            }

    # Pattern B: subsection with header + paragraph children
    for subsection in root.iter("subsection"):
        header = subsection.find("header")
        committee = _parse_header(header)
        if not committee:
            continue
        members = []
        for para in subsection.iter("paragraph"):
            text_elem = para.find("text")
            name = _get_text(text_elem)
            if name and len(name) > 2:
                members.append(name)
        if members:
            action = _parse_context_action(subsection, parent_map, root)
            yield {
                "event_date": event_date,
                "citation": citation,
                "action": action,
                "committee": committee,
                "members": members,
                "member_observations": _member_observations("; ".join(members), members),
            }

    # Pattern C: section > paragraph with header + text (comma-separated names, e.g. H.Res. 7)
    for section in root.iter("section"):
        for para in section.iter("paragraph"):
            header = para.find("header")
            text_elem = para.find("text")
            if header is None or text_elem is None:
                continue
            committee = _parse_header(header)
            if not committee or "Committee" not in committee:
                continue
            members_text = _get_text(text_elem)
            members = _parse_members_from_text(members_text)
            if members:
                action = _parse_context_action(para, parent_map, root)
                yield {
                    "event_date": event_date,
                    "citation": citation,
                    "action": action,
                    "committee": committee,
                    "members": members,
                    "member_observations": _member_observations(members_text, members),
                }

    # Pattern D: a prose paragraph directly removes one member from one or more
    # committees (for example, H.Res. 789 in the 117th Congress).
    for para in root.iter("paragraph"):
        parsed = _parse_prose_removal(_get_text(para.find("text")))
        if not parsed:
            continue
        member, committees = parsed
        for committee in committees:
            yield {
                "event_date": event_date,
                "citation": citation,
                "action": "REMOVED",
                "committee": committee,
                "members": [member],
                "member_observations": [
                    {"name": member, "source_ordinal": 1, "rank_after": None}
                ],
            }
