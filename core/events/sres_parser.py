"""
Parse S.Res. XML and Senate free text (UC requests, consent language) for committee appointments.

- XML: Same structural patterns as H.Res. (committee-appointment-paragraph, subsection,
  section/paragraph). Senate-specific: member lists with state qualifiers (e.g. "Mr. Scott (FL)")
  and roles (Chair), (Ranking), (ex officio).
- Free text: Anchors "Mr. President", "I ask unanimous consent", "submitted the following
  resolution"; then committee + member extraction with state-qualifier awareness.

Yields same shape as H.Res.: event_date, citation, committee, members.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Iterator

from core.committees.resolver import committee_name_to_id
from core.committees.types import SENATE_AUTHORIZING_COMMITTEE_IDS

# ---------------------------------------------------------------------------
# Shared XML helpers (mirror hres_parser where applicable)
# ---------------------------------------------------------------------------


def _get_text(elem: ET.Element | None) -> str:
    """Recursively extract text from element and its children."""
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def _parse_header(header_elem: ET.Element | None) -> str:
    """Extract committee name from header; S.Res. may use <committee-name> or plain text."""
    if header_elem is None:
        return ""
    raw = _get_text(header_elem)
    return raw.rstrip(":").strip()


def _parse_action_date(action_date_elem: ET.Element | None) -> str | None:
    """Parse action-date, preferring display text over known-bad date attributes."""
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


def _member_observations(members: list[str]) -> list[dict]:
    """Preserve the Senate source-list position for downstream rank derivation."""
    return [
        {"name": name, "source_ordinal": ordinal, "rank_after": None}
        for ordinal, name in enumerate(members, start=1)
    ]


HONORIFIC_PREFIX_RE = re.compile(
    r"(?i)^(?:Mr\.?|Mrs\.?|Ms\.?|Miss|Sen\.?|Senator)\s+"
)
STATE_NAME_TO_CODE = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _split_core_and_state(raw: str) -> tuple[str, str | None]:
    """
    Extract trailing state marker first (e.g. "(SD)"), then strip all trailing
    parenthetical annotations.
    """
    text = _collapse_ws(raw).strip(" ,.;:")

    # Walk trailing parentheticals from right to left to retain a state code if present.
    trailing_groups: list[str] = []
    while True:
        m = re.search(r"\s*\(([^)]*)\)\s*$", text)
        if not m:
            break
        trailing_groups.append(m.group(1).strip())
        text = text[: m.start()].rstrip()

    state: str | None = None
    for grp in trailing_groups:
        if re.fullmatch(r"(?i)[A-Z]{2}", grp):
            state = grp.upper()
            break

    text = _collapse_ws(text)
    text = HONORIFIC_PREFIX_RE.sub("", text).strip()

    # Alternate state form: "Udall of Colorado"
    of_state = re.search(r"(?i)\bof\s+([A-Za-z][A-Za-z\s]+)$", text)
    if of_state:
        state_name = _collapse_ws(of_state.group(1)).lower()
        state = state or STATE_NAME_TO_CODE.get(state_name)
        text = text[: of_state.start()].strip()

    return text.strip(" ,.;:"), state


def normalize_senate_resolution_name(raw: str) -> str:
    """
    Normalize S.Res. member token to deterministic last_name lookup value.
    """
    core, _state = _split_core_and_state(raw)
    if not core:
        return ""
    core = _collapse_ws(core).strip(" ,.;:")
    if not core:
        return ""
    return core.split()[-1]


def senate_resolution_name_candidates(raw: str) -> list[str]:
    """
    Return deterministic candidate lookup keys for Senate member resolution.

    Prefer full core name first (handles compound surnames like "Van Hollen",
    "Cortez Masto"), then fall back to terminal token for traditional last-name
    matching (e.g., "Mr. John Doe" -> "Doe").
    """
    core, _state = _split_core_and_state(raw)
    if not core:
        return []
    core = _collapse_ws(core).strip(" ,.;:")
    if not core:
        return []
    last = core.split()[-1]
    if core == last:
        return [core]
    return [core, last]


def extract_senate_resolution_state(raw: str) -> str | None:
    """
    Extract two-letter state code from S.Res. member token if present.
    """
    _core, state = _split_core_and_state(raw)
    return state


def _normalize_senate_member_token(raw: str) -> str:
    """
    Canonical member token for parser output; preserves state code when available.
    """
    core, state = _split_core_and_state(raw)
    core = _collapse_ws(core).strip(" ,.;:") if core else ""
    if not core:
        return ""
    return f"{core} ({state})" if state else core


def _parse_members_senate(text: str) -> list[str]:
    """
    Split member text by semicolons/commas; clean each name.
    Keeps state qualifiers (e.g. " of Iowa", " (FL)") for resolver.
    Strips role parentheticals (Chair), (Ranking), (ex officio).
    """
    if not text:
        return []
    text = re.sub(r"\s+and\s+", ", ", text, flags=re.IGNORECASE)
    parts = re.split(r"\s*[;,]\s*", text)
    names = []
    for p in parts:
        p = _normalize_senate_member_token(p)
        if len(p) > 1:
            names.append(p)
    return names


TRACKED_SENATE_COMMITTEES = set(SENATE_AUTHORIZING_COMMITTEE_IDS)


def _is_tracked_senate_committee_heading(committee: str) -> bool:
    """
    Restrict S.Res. extraction to tracked Senate committees.
    Prevents section-title bleed (e.g., "In general", "Expenses", "Authority").
    """
    if not committee or "committee" not in committee.lower():
        return False
    code = committee_name_to_id(committee, bill_type="sres")
    return bool(code and code in TRACKED_SENATE_COMMITTEES)


# ---------------------------------------------------------------------------
# S.Res. XML parsing
# ---------------------------------------------------------------------------


def parse_sres_xml(path: Path) -> Iterator[dict]:
    """
    Parse an S.Res. XML file and yield appointment records.

    Yields dicts with: event_date, citation, committee, members
    (same shape as parse_hres_xml for downstream compatibility).
    """
    tree = ET.parse(path)
    root = tree.getroot()

    action_date = root.find(".//action-date")
    event_date = _parse_action_date(action_date)

    legis_num = root.find(".//legis-num")
    citation = _get_text(legis_num) if legis_num is not None else ""

    if not event_date:
        return

    # Pattern A: committee-appointment-paragraph (same as H.Res.; S.Res. 17, 26 use this)
    for cap in root.iter("committee-appointment-paragraph"):
        header = cap.find("header")
        text_elem = cap.find("text")
        committee = _parse_header(header)
        if not _is_tracked_senate_committee_heading(committee):
            continue
        members_text = _get_text(text_elem)
        members = _parse_members_senate(members_text)
        if members:
            yield {
                "event_date": event_date,
                "citation": citation,
                "committee": committee,
                "members": members,
                "member_observations": _member_observations(members),
            }

    # Pattern B: subsection with header + paragraph children
    for subsection in root.iter("subsection"):
        header = subsection.find("header")
        committee = _parse_header(header)
        if not _is_tracked_senate_committee_heading(committee):
            continue
        members = []
        for para in subsection.iter("paragraph"):
            text_elem = para.find("text")
            name = _get_text(text_elem)
            if name and len(name) > 2:
                name = _normalize_senate_member_token(name)
                members.append(name)
        if members:
            yield {
                "event_date": event_date,
                "citation": citation,
                "committee": committee,
                "members": members,
                "member_observations": _member_observations(members),
            }

    # Pattern C: section > paragraph with header + text
    for section in root.iter("section"):
        for para in section.iter("paragraph"):
            header = para.find("header")
            text_elem = para.find("text")
            if header is None or text_elem is None:
                continue
            committee = _parse_header(header)
            if not _is_tracked_senate_committee_heading(committee):
                continue
            members_text = _get_text(text_elem)
            members = _parse_members_senate(members_text)
            if members:
                yield {
                    "event_date": event_date,
                    "citation": citation,
                    "committee": committee,
                    "members": members,
                    "member_observations": _member_observations(members),
                }


# ---------------------------------------------------------------------------
# Senate free-text parsing (UC requests, consent language)
# ---------------------------------------------------------------------------

# Anchors that indicate Senate UC / resolution submission context
SENATE_ANCHORS_RE = re.compile(
    r"(?i)"
    r"(?:Mr\.\s+President\b|"
    r"I\s+ask\s+unanimous\s+consent\b|"
    r"submitted\s+the\s+following\s+resolution\b|"
    r"unanimous\s+consent\s+(?:that\s+)?(?:the\s+)?(?:Senate\s+)?)"
)

# Committee + members block: "to the Committee on X: A, B, and C" or "as members of the Committee on X: ..."
COMMITTEE_MEMBERS_RE = re.compile(
    r"(?i)"
    r"(?:to the|as (?:a )?members? of the|"
    r"member(?:s)? of the Senate (?:to the|on the part of the Senate to the)\s+)"
    r"(.*?)(?::|\.(?!\s*[A-Z]))\s*(.*)",
    re.DOTALL,
)


def parse_senate_appointment_text(
    text: str,
    default_date: str | None = None,
    citation: str | None = None,
) -> Iterator[dict]:
    """
    Parse free text (e.g. CREC Senate, UC request) for committee appointments.

    Looks for Senate anchors (Mr. President, I ask unanimous consent, submitted the
    following resolution), then extracts committee name and member list. Member
    references may include state qualifiers (e.g. "Senator Smith of Iowa").

    Yields dicts with: event_date, citation, committee, members.
    If default_date/citation are None and none found in text, skips yielding.
    """
    if not text or not SENATE_ANCHORS_RE.search(text):
        return

    text_clean = re.sub(r"\s+", " ", text).strip()
    # Split on appointment language to find segments that list committee + members
    segments = re.split(r"(?i)\b(?:re)?appoint(?:s|ment|ed|ing)?\b", text_clean)
    event_date = default_date
    cite = citation or ""

    for segment in segments[1:]:
        m = COMMITTEE_MEMBERS_RE.search(segment)
        if not m:
            continue
        committee_raw = m.group(1).strip()
        committee = re.split(
            r"(?i)\s+(?:during|for a|pursuant|effective|vice|as )\b",
            committee_raw,
        )[0].strip().strip(".,")
        if not _is_tracked_senate_committee_heading(committee):
            continue

        members_block = m.group(2).strip()
        # Stop at common following phrases
        stop = re.search(
            r"(?:\.)?\s*(?:The (?:message|Chair|Senator)|Enrolled Bills|vice ).*",
            members_block,
            re.IGNORECASE,
        )
        if stop:
            members_block = members_block[: stop.start()].strip()
        if members_block.endswith("."):
            members_block = members_block[:-1].strip()

        members_block = re.sub(r"(?i)\band\b", ",", members_block)
        raw_names = re.split(r"\s*[;,]\s*", members_block)
        names = []
        for n in raw_names:
            n = n.strip()
            if not n:
                continue
            n = re.sub(r"(?i)^(the Honorable|Representative|Senator)\s+", "", n)
            n = re.sub(r"(?i)\s*(Chairman|Vice Chair|Ranking Member)$", "", n)
            n = n.strip(" ,.")
            if len(n) > 2 and (" " in n or "." in n):
                names.append(n)

        if committee and names:
            yield {
                "event_date": event_date or "",
                "citation": cite,
                "committee": committee,
                "members": names,
                "member_observations": _member_observations(names),
            }
