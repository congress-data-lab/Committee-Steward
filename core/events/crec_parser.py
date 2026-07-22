import re
from datetime import datetime
from typing import List, Dict, Optional

# GPO dateline: "Washington, DC, January 14, 2013." or "February 2, 2016."
GPO_DATELINE_RE = re.compile(
    r"(?i)(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})"
)
EXPLICIT_EFFECTIVE_DATE_RE = re.compile(
    r"(?is)\beffective\b(?:(?![.;]).){0,100}?\b"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}(?:st|nd|rd|th)?),\s+(\d{4})"
)

# Unified departure verb family (committee membership mutation actions).
TERMINATION_VERBS = (
    r"(?:"
    r"resign(?:ation)?s?"
    r"|request(?:\s+for|\s+to)?\s+(?:a\s+)?leave\s+of\s+absence"
    r"|request(?:\s+that\s+i)?\s+be\s+relieved"
    r"|take\s+(?:a\s+)?leave\s+of\s+absence"
    r"|stepp?ing\s+down"
    r"|written\s+notice\s+of\s+.*?resignation"
    r")"
)
TERMINATION_PREFIXES = (
    r"(?:"
    r"resign(?:ation)?s?"
    r"|i\s+hereby\s+resign"
    r"|request(?:\s+for|\s+to)?\s+(?:a\s+)?leave\s+of\s+absence"
    r"|request(?:\s+that\s+i)?\s+be\s+relieved"
    r"|take\s+(?:a\s+)?leave\s+of\s+absence"
    r"|stepp?ing\s+down"
    r"|written\s+notice\s+of\s+my\s+resignation"
    r")"
)
TERMINATION_COMMITTEE_BRIDGE = (
    r"(?:\s+from(?:\s+my\s+(?:seat|assignment)\s+on|\s+my\s+appointment\s+to)?|\s+on)"
)
COMMITTEE_TRAILING_CONTEXT = (
    r"(?:\s+(?:in|for)\s+the\s+(?:remainder\s+of\s+the\s+)?\d{1,3}(?:st|nd|rd|th)\s+Congress)?"
)
COMMITTEE_REFERENCE_PATTERN = (
    r"(?:"
    r"(?:House\s+Committee|Select\s+Committee|Committee)\s+on\s+[^,.;:\n]+?"
    r"|[A-Za-z][A-Za-z0-9&'().,\-\s]{1,140}?\s+Committee"
    r")"
)

TERMINATION_VERB_RE = re.compile(rf"(?i){TERMINATION_VERBS}")

TERMINATION_FROM_COMMITTEE_ANCHOR_RE = re.compile(
    rf"(?i){TERMINATION_PREFIXES}{TERMINATION_COMMITTEE_BRIDGE}\s+(?:the\s+)?"
    rf"{COMMITTEE_REFERENCE_PATTERN}{COMMITTEE_TRAILING_CONTEXT}(?=[,.;:\n]|$)"
)

INLINE_TERMINATION_WINDOW_RE = re.compile(
    rf"(?i)(?P<verb>{TERMINATION_PREFIXES})(?P<middle>.{{0,120}}?)"
    rf"(?P<committee>(?:the\s+)?{COMMITTEE_REFERENCE_PATTERN}{COMMITTEE_TRAILING_CONTEXT})(?=[,.;:\n]|$)"
)

COMPLETED_ACTION_RE = re.compile(
    r"(?i)\b(?:laid\s+before\s+the\s+(?:house|senate)|i\s+hereby|am\s+submitting|submitted|was\s+received|was\s+accepted)\b"
)

SPECULATIVE_ACTION_RE = re.compile(
    r"(?i)\b(?:may|might|could|consider(?:ing)?|discuss(?:ed|ing)?)\b.{0,40}\b(?:resign|stepp?ing\s+down|leave\s+of\s+absence)\b"
)

# Boilerplate preamble:
# "laid before the House the following resignation(s) as a member of the Committee on X"
# plus leave/step-down variants.
COMMITTEE_TERMINATION_PREAMBLE_RE = re.compile(
    r"(?i)laid\s+before\s+the\s+(?:House|Senate)\s+the\s+following\s+"
    rf"{TERMINATION_VERBS}\s+"
    r"(?:as\s+a\s+member\s+of\s+)?the\s+(.+?)[.:]",
    re.DOTALL,
)

# GPO Style Manual:
# "Re resignation from committee." (generic subject) or "Re resignation from the Committee on X"
# plus leave/step-down variants.
# See https://www.govinfo.gov/content/pkg/GPO-STYLEMANUAL-2008/html/GPO-STYLEMANUAL-2008-21.htm
RE_TERMINATION_FROM_COMMITTEE_RE = re.compile(
    r"(?i)Re\s+"
    rf"{TERMINATION_VERBS}\s+from\s+"
    r"(?:the\s+)?(?:(?:House\s+Committee|Select\s+Committee|Committee)\s+on\s+(.+?)|committee)\s*[.\s]",
    re.DOTALL,
)

# Committee in body: "resign from", "step down from", "relinquish my seat on", "withdraw from" (when subject is generic)
# Explicit committee-membership termination phrases found in CREC diagnostics.
COMMITTEE_IN_BODY_RE = re.compile(
    rf"(?i)(?:"
    rf"{TERMINATION_PREFIXES}{TERMINATION_COMMITTEE_BRIDGE}"
    r"|resign\s+my\s+(?:position\s+on|seat\s+on)"
    r"|relinquish\s+my\s+seat\s+on"
    r"|withdraw\s+from"
    r")\s+(?P<committee>(?:the\s+)?"
    rf"{COMMITTEE_REFERENCE_PATTERN}{COMMITTEE_TRAILING_CONTEXT}"
    r")(?=[,.;:\n]|$)",
    re.DOTALL,
)

PROCEDURAL_COMMUNICATION_RE = re.compile(
    r"(?i)\b(?:the\s+speaker(?:\s+pro\s+tempore)?\s+laid\s+before\s+the\s+(?:house|senate)|laid\s+before\s+the\s+(?:house|senate)\s+the\s+following)\b"
)
PROCEDURAL_ACCEPTANCE_RE = re.compile(
    r"(?i)\bwithout\s+objection,\s+the\s+(?:resignation(?:s)?|request(?:s)?|communication(?:s)?)\s+are?\s+accepted\b"
)
COMMITTEE_OF_WHOLE_RE = re.compile(r"(?i)\bcommittee\s+of\s+the\s+whole\b")
BILL_REFERRAL_RE = re.compile(
    r"(?i)\b(?:referred|reference)\s+to\s+the\s+committee\s+on\b|\breports?\s+of\s+committees?\s+on\s+public\s+bills\b"
)


def _is_excluded_committee_context(text: str) -> bool:
    """Reject non-membership committee mentions."""
    return bool(COMMITTEE_OF_WHOLE_RE.search(text) or BILL_REFERRAL_RE.search(text))


def _has_procedural_committee_frame(text: str) -> bool:
    """Detect Speaker/Clerk communication + acceptance framing for committee departures."""
    has_frame = bool(PROCEDURAL_COMMUNICATION_RE.search(text) or PROCEDURAL_ACCEPTANCE_RE.search(text))
    if not has_frame:
        return False
    return bool(TERMINATION_FROM_COMMITTEE_ANCHOR_RE.search(text) or _has_inline_termination_window(text))


def has_committee_termination_signal(text: str) -> bool:
    """
    Guardrail for committee-level departures.
    Requires committee departure anchor + institutional finality, and rejects speculative mentions.
    """
    normalized = re.sub(r"\s+", " ", text)
    if _is_excluded_committee_context(normalized):
        return False
    if SPECULATIVE_ACTION_RE.search(normalized):
        return False
    if COMMITTEE_TERMINATION_PREAMBLE_RE.search(normalized):
        return True
    if TERMINATION_FROM_COMMITTEE_ANCHOR_RE.search(normalized):
        return True
    if _has_procedural_committee_frame(normalized):
        return True
    return _has_inline_termination_window(normalized)


def has_departure_verb_with_finality(text: str) -> bool:
    """
    True when departure verb appears with institutional finality, even if committee anchor is in nearby text.
    Used for header/body split layouts.
    """
    normalized = re.sub(r"\s+", " ", text)
    if _is_excluded_committee_context(normalized):
        return False
    if SPECULATIVE_ACTION_RE.search(normalized):
        return False
    if TERMINATION_VERB_RE.search(normalized) and COMPLETED_ACTION_RE.search(normalized):
        return True
    if _has_procedural_committee_frame(normalized):
        return True
    return _has_inline_termination_window(normalized)

# Resignation from House: "laid before the House the following resignation from the House of Representatives"
RESIGNATION_FROM_HOUSE_RE = re.compile(
    r"(?i)laid\s+before\s+the\s+House\s+the\s+following\s+resignation\s+from\s+the\s+House\s+of\s+Representatives",
)
RESIGNATION_FROM_HOUSE_GENERIC_RE = re.compile(
    r"(?i)\bresignation\s+from\s+the\s+House\s+of\s+Representatives\b",
)

# Death of member: GPO 81-84, 115-119. "death of the Honorable C.W. BILL YOUNG of Florida"
# or "heard with profound sorrow of the death of the Honorable [NAME]"
# Require Honorable/Representative/Senator to avoid "death of the bill" etc.
# "Senator Name, of State" (e.g. "Senator FRANK R. LAUTENBERG, of New Jersey") — capture full name before comma.
DEATH_OF_MEMBER_NAME_COMMA_OF_STATE_RE = re.compile(
    r"(?i)(?:heard\s+with\s+profound\s+sorrow\s+of\s+the\s+)?(?:death|passing|demise)\s+of\s+"
    r"(?:(?:the\s+Honorable\s+)|(?:Representative\s+)|(?:Senator\s+))([^,]+),\s*of\s+[A-Za-z\s]+",
    re.DOTALL,
)
# "Senator FIRST, LAST of State" (e.g. "Senator X, LAUTENBERG of New Jersey") — capture both parts.
DEATH_OF_MEMBER_WITH_STATE_COMMA_RE = re.compile(
    r"(?i)(?:heard\s+with\s+profound\s+sorrow\s+of\s+the\s+)?(?:death|passing|demise)\s+of\s+"
    r"(?:(?:the\s+Honorable\s+)|(?:Representative\s+)|(?:Senator\s+))([^,]+),\s*([^.;]+?)\s+of\s+[A-Za-z\s]+",
    re.DOTALL,
)
# Then "Name of Florida" (no comma in name)
DEATH_OF_MEMBER_WITH_STATE_RE = re.compile(
    r"(?i)(?:heard\s+with\s+profound\s+sorrow\s+of\s+the\s+)?(?:death|passing|demise)\s+of\s+"
    r"(?:(?:the\s+Honorable\s+)|(?:Representative\s+)|(?:Senator\s+))([^,]+?)(?:,\s*[^.;]+?)?\s+of\s+[A-Za-z\s]+",
    re.DOTALL,
)
DEATH_OF_MEMBER_NO_STATE_RE = re.compile(
    r"(?i)(?:heard\s+with\s+profound\s+sorrow\s+of\s+the\s+)?(?:death|passing|demise)\s+of\s+"
    r"(?:(?:the\s+Honorable\s+)|(?:Representative\s+)|(?:Senator\s+))([^,;]+)[.,;]",
    re.DOTALL,
)
# "The Chair announces ... in light of the passing of the gentleman from [State] (Mr. LASTNAME)"
DEATH_OF_MEMBER_GENTLEMAN_RE = re.compile(
    r"(?i)(?:passing|death|demise)\s+of\s+the\s+gentleman\s+from\s+"
    r"(?:the\s+State\s+of\s+)?[^,(]+(?:\(Mr\.\s+([^)]+)\)|,\s*Mr\.\s+([^.,]+))",
    re.DOTALL,
)

# "Therefore, the Honorable Paul D. Ryan of the State of Wisconsin, having received ... is duly elected Speaker"
# Capture name as text before ", having received" or ", is duly elected" to avoid vote tally.
SPEAKER_ELECTION_RE = re.compile(
    r"(?i)(?:the\s+Honorable\s+)(.+?)\s*,\s*(?:having\s+received\s+a\s+majority\s+of\s+the\s+votes\s+cast\s*,?\s*)?is\s+duly\s+elected\s+Speaker\s+of\s+the\s+House",
    re.DOTALL,
)
SPEAKER_ELECTION_ALT_RE = re.compile(
    r"(?i)([A-Za-z][A-Za-z.\s]+?),?\s*a\s+Representative\s+from\s+[^,]+,\s+has\s+been\s+elected\s+Speaker\s+of\s+the\s+House",
    re.DOTALL,
)

# "request to be relieved from the Committee on X and appointed to the Committee on Y" / "transferred to the Committee on X"
TRANSFER_RELIEVED_APPOINTED_RE = re.compile(
    r"(?i)(?:request\s+to\s+be\s+)?relieved\s+from\s+(?:the\s+)?(?:Committee\s+on\s+)([^,]+?)\s+and\s+(?:appointed\s+to|transferred\s+to)\s+(?:the\s+)?(?:Committee\s+on\s+)([^,.\n]+?)(?=[,.\n]|$)",
    re.DOTALL,
)
TRANSFER_TO_ONLY_RE = re.compile(
    r"(?i)transferred\s+to\s+(?:the\s+)?(?:Committee\s+on\s+)([^,.\n]+?)(?=[,.\n]|$)",
    re.DOTALL,
)
# "I, Bob Dold, request to be relieved" / "request that the gentleman from Illinois (Mr. Dold) be transferred"
TRANSFER_MEMBER_I_REQUEST_RE = re.compile(
    r"(?i)I\s*,\s*([^,]+?)\s*,\s*request\s+to\s+be\s+relieved",
    re.DOTALL,
)
TRANSFER_GENTLEMAN_RE = re.compile(
    r"(?i)(?:request|communication)\s+(?:that\s+)?the\s+gentleman\s+from\s+[^,(]+(?:\(Mr\.\s+([^)]+)\)|,\s*Mr\.\s+([^.,]+))",
    re.DOTALL,
)

# Member: "I, Matthew A. Cartwright, am submitting" (suffix optional: Jr., Sr., II, III, IV, M.D., Ph.D.)
MEMBER_I_AM_SUBMITTING_RE = re.compile(
    r"(?i)I,\s*([^,]+(?:,\s*(?:Jr\.?|Sr\.?|II|III|IV|M\.D\.?|Ph\.D\.?))?),\s*am\s+submitting"
)

# Member: signature block "Best Regards,\n...Marsha Blackburn,\nMember of Congress"
# Suffix handling: optional ", Jr." / ", M.D." etc. anchored to delimiter; do not broaden base capture
MEMBER_SIGNATURE_RE = re.compile(
    r"(?i)(?:Sincerely|Best\s+Regards|Respectfully|Very\s+truly\s+yours)[^.]*?\n\s*([^\n,]+?)(,\s*(?:Jr\.?|Sr\.?|II|III|IV|M\.D\.?|Ph\.D\.?))?,\s*\n\s*Member\s+of\s+Congress",
    re.DOTALL,
)

# Member: "Matt Cartwright." (no Member of Congress)
MEMBER_SIGNATURE_ALT_RE = re.compile(
    r"(?i)(?:Sincerely|Best\s+Regards|Respectfully|Very\s+truly\s+yours)[^.]*?\n\s*([^\n.]+)\s*\.\s*$",
    re.DOTALL,
)

# Member: "Ed Whitfield,\n\nU.S. Congressman," or
# "Jason E. Chaffetz,\n\nU.S. Representative,".
MEMBER_SIGNATURE_US_CONGRESSMAN_RE = re.compile(
    r"(?i)(?:Respectfully|Sincerely|Best\s+Regards|Respectfully\s+Submitted)[^.]*?\n\s*([^\n,]+?)(,\s*(?:Jr\.?|Sr\.?|II|III|IV|M\.D\.?|Ph\.D\.?))?,\s*\n\s*U\.?S\.?\s+(?:Congressman|Congresswoman|Representative)",
    re.DOTALL,
)

# Member: "Melvin L. Watt,\n12th District of North Carolina."
MEMBER_SIGNATURE_DISTRICT_RE = re.compile(
    r"(?i)(?:Respectfully|Sincerely|Best\s+Regards|Respectfully\s+Submitted)[^.]*?\n"
    r"\s*([^\n,]+?)(,\s*(?:Jr\.?|Sr\.?|II|III|IV|M\.D\.?|Ph\.D\.?))?,\s*\n"
    r"\s*\d{1,2}(?:st|nd|rd|th)\s+District\s+of\s+[A-Za-z][A-Za-z .'-]+",
    re.DOTALL,
)

# GPO Style Manual: "Sincerely,\n\nVINCENT J. DELLAY." or "Sincerely yours,\n\nNAME." (no "Member of Congress")
MEMBER_SIGNATURE_SINCERELY_ALT_RE = re.compile(
    r"(?i)(?:Sincerely,?|Sincerely\s+yours,?)\s*\n\s*([^\n]+?)\s*\.\s*(?:\n|$|--)",
    re.DOTALL,
)

# Some members use a personal closing (for example, "Semper Fidelis") rather
# than one of the conventional closings above. The role line is the stable
# structural anchor in those letters.
MEMBER_SIGNATURE_MEMBER_OF_CONGRESS_RE = re.compile(
    r"(?im)^\s*(?:Rep\.\s*)?([^\n,]{3,100}?),\s*\n\s*Member\s+of\s+Congress\b"
)

VACANCY_CAUSED_BY_RE = re.compile(
    r"(?i)\bto\s+fill\s+the\s+vacancy\s+(?:caused|created)\s+by\s+(?:the\s+)?"
    r"(?:resignation|death|retirement)\s+of\s+(.+?)(?=\s*(?::|,|;|$))"
)

# Senate resignation: "vacancy created by the resignation of Senator Jeff Sessions of Alabama"
# or "letters of resignation from former Senator Thad Cochran of Mississippi"
SENATE_RESIGNATION_VACANCY_RE = re.compile(
    r"(?i)\bvacancy\s+(?:therein\s+)?(?:caused|created)\s+by\s+the\s+resignation\s+of\s+"
    r"(?:United\s+States\s+)?(?:former\s+)?Senator\s+(.+?)(?:\s+of\s+([A-Za-z][A-Za-z\s]+?))?(?=[,.;]|\s+is\s+filled|$)",
    re.DOTALL,
)
SENATE_RESIGNATION_LETTERS_RE = re.compile(
    r"(?i)\bletters?\s+of\s+resignation\s+from\s+former\s+Senator\s+(.+?)\s+of\s+([A-Za-z][A-Za-z\s]+?)(?=[,.]|\s+and|$)",
    re.DOTALL,
)
SENATE_RESIGNATION_CERTIFICATE_RE = re.compile(
    r"(?i)(?:certificate\s+of\s+appointment\s+to\s+fill\s+the\s+)?"
    r"vacancy\s+(?:therein\s+)?(?:caused|created)\s+by\s+the\s+resignation\s+of\s+(.+?)(?=[,.;]|\s+is\s+filled|$)",
    re.DOTALL,
)

# Senate vacancy by death: "vacancy caused/created by the death of Senator X of State"
# Used when a Senator dies in office; caller derives REMOVED for each committee (same as resignation).
SENATE_DEATH_VACANCY_RE = re.compile(
    r"(?i)\bvacancy\s+(?:therein\s+)?(?:caused|created)\s+by\s+the\s+death\s+of\s+"
    r"(?:United\s+States\s+)?Senator\s+(.+?)(?:\s+of\s+([A-Za-z][A-Za-z\s]+?))?(?=[,.;]|\s+is\s+filled|$)",
    re.DOTALL,
)


def _parse_effective_date(text: str) -> Optional[str]:
    """Extract an explicit effective date, otherwise the first GPO dateline."""
    for pattern in (EXPLICIT_EFFECTIVE_DATE_RE, GPO_DATELINE_RE):
        m = pattern.search(text)
        if not m:
            continue
        month_name, day, year = m.group(1), m.group(2), m.group(3)
        day = re.sub(r"(?i)(?:st|nd|rd|th)$", "", day)
        try:
            dt = datetime.strptime(f"{month_name} {day}, {year}", "%B %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_resignation_member(text: str) -> Optional[str]:
    """Extract member name from resignation letter text."""
    m = MEMBER_I_AM_SUBMITTING_RE.search(text)
    if m:
        return m.group(1).strip()
    m = MEMBER_SIGNATURE_RE.search(text)
    if m:
        name = m.group(1).strip()
        suffix = (m.group(2) or "")
        return (name + suffix).strip()
    m = MEMBER_SIGNATURE_ALT_RE.search(text)
    if m:
        return m.group(1).strip()
    m = MEMBER_SIGNATURE_US_CONGRESSMAN_RE.search(text)
    if m:
        name = m.group(1).strip()
        suffix = (m.group(2) or "")
        return (name + suffix).strip()
    m = MEMBER_SIGNATURE_DISTRICT_RE.search(text)
    if m:
        name = m.group(1).strip()
        suffix = (m.group(2) or "")
        return (name + suffix).strip()
    m = MEMBER_SIGNATURE_SINCERELY_ALT_RE.search(text)
    if m:
        return m.group(1).strip()
    m = MEMBER_SIGNATURE_MEMBER_OF_CONGRESS_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _split_committee_names(raw: str) -> List[str]:
    """
    Split committee list into individual committee names.
    Handles: "Committee on X and the Committee on Y",
             "Committee on X, the Committee on Y, and the Committee on Z",
             "Committee on X and Committee on Y" (no "the"),
             "Committees on X and Y" (plural).
    """
    raw = raw.strip().strip(".:,")
    if not raw:
        return []

    # "Committees on Armed Services and Agriculture" (may have newline before Agriculture)
    m = re.match(r"(?i)^Committees\s+on\s+(.+)$", raw, re.DOTALL)
    if m:
        rest = re.sub(r"\s+", " ", m.group(1).strip())
        parts = re.split(r"\s+and\s+", rest, flags=re.IGNORECASE)
        return [f"Committee on {p.strip()}" for p in parts if p.strip()]

    # Repeated committee labels, with or without "the":
    # ", Committee on", ", the Committee on", or ", and Committee on".
    parts = re.split(
        r",\s*(?:and\s+)?(?:the\s+)?Committee\s+on\s+",
        raw,
        flags=re.IGNORECASE,
    )
    if len(parts) > 1:
        result = []
        for i, p in enumerate(parts):
            p = p.strip().strip(".,")
            if not p:
                continue
            if i == 0 and re.match(r"(?i)^Committee\s+on\s+", p):
                result.append(p)
            else:
                result.append(f"Committee on {p}")
        if result:
            return result

    # " and the Committee on " or " and Committee on "
    parts = re.split(r"\s+and\s+(?:the\s+)?Committee\s+on\s+", raw, flags=re.IGNORECASE)
    if len(parts) > 1:
        result = []
        for i, p in enumerate(parts):
            p = p.strip().strip(".,")
            if not p:
                continue
            if i == 0 and re.match(r"(?i)^Committee\s+on\s+", p):
                result.append(p)
            else:
                result.append(f"Committee on {p}")
        if result:
            return result

    return [raw] if raw else []


def _canonicalize_committee_label(raw: str) -> str:
    """Normalize committee label variants to canonical 'Committee on ...' form."""
    s = re.sub(r"\s+", " ", raw.strip().strip(".:"))
    s = re.split(r"(?i)\s+(?:during|in|for|pursuant|effective)\b", s, maxsplit=1)[0].strip()
    s = re.sub(r"(?i)^(?:the\s+)?house\s+committee\s+on\s+", "Committee on ", s)
    s = re.sub(r"(?i)^(?:the\s+)?select\s+committee\s+on\s+", "Committee on ", s)
    s = re.sub(r"(?i)^(?:the\s+)?committee\s+on\s+", "Committee on ", s)
    # Support "Appropriations Committee" style references.
    if not re.match(r"(?i)^Committee\s+on\s+", s):
        m = re.match(r"(?i)^(?:the\s+)?(.+?)\s+Committee$", s)
        if m:
            body = m.group(1).strip()
            if body:
                s = f"Committee on {body}"
    return s


def _trim_trailing_committee_clause(raw: str) -> str:
    """
    Trim narrative clause spillover after a formal committee noun phrase.
    """
    return re.split(
        r"(?i)\s+(?:so\s+that|because|in\s+order\s+to|to\s+[a-z]|which|that\s+i|that\s+we)\b",
        raw,
        maxsplit=1,
    )[0].strip().rstrip(".,;:")


def _has_inline_termination_window(text: str) -> bool:
    """Check for inline departure language + committee mention within a tight window."""
    if COMMITTEE_IN_BODY_RE.search(text):
        return True
    for m in INLINE_TERMINATION_WINDOW_RE.finditer(text):
        middle = re.sub(r"\s+", " ", (m.group("middle") or "")).lower()
        if "from" in middle or " on " in f" {middle} ":
            return True
    return False


def _collect_inline_body_committees(text: str) -> List[str]:
    """Extract committee labels from inline/body termination phrases."""
    normalized = re.sub(r"\s+", " ", text)
    committees: List[str] = []
    for body_m in COMMITTEE_IN_BODY_RE.finditer(normalized):
        raw = body_m.group("committee").strip().strip(".:")
        if raw:
            committees.extend(_split_committee_names(raw))
    if committees:
        return committees
    for m in INLINE_TERMINATION_WINDOW_RE.finditer(normalized):
        middle = re.sub(r"\s+", " ", (m.group("middle") or "")).lower()
        if "from" not in middle and " on " not in f" {middle} ":
            continue
        raw = m.group("committee").strip().strip(".:")
        if raw:
            committees.extend(_split_committee_names(raw))
    return committees


def _clean_member_label(raw: str) -> str:
    s = re.sub(r"(?i)^(?:the\s+)?(?:honorable|mr\.?|mrs\.?|ms\.?|representative|senator)\s+", "", raw).strip()
    return s.strip(" ,.")


def parse_crec_terminations_from_house(text: str, header_date: str) -> Optional[Dict]:
    """
    Parse "resignation from the House of Representatives" text.
    Returns {member: str, effective_date: str} or None if not found.
    Used when a member leaves Congress entirely; caller must derive REMOVED for each committee.
    """
    if not RESIGNATION_FROM_HOUSE_RE.search(text) and not RESIGNATION_FROM_HOUSE_GENERIC_RE.search(text):
        return None
    member = _parse_resignation_member(text)
    if not member:
        return None
    effective_date = _parse_effective_date(text) or header_date
    return {"member": member, "effective_date": effective_date}


def _normalize_senate_resignation_name(raw: str) -> str:
    """Strip Senator/United States/ of State from captured name."""
    s = re.sub(r"(?i)^(?:United\s+States\s+)?(?:former\s+)?Senator\s+", "", raw).strip()
    s = re.sub(r"\s+of\s+[A-Za-z][A-Za-z\s]+$", "", s, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", s).strip(" ,.;")


def parse_crec_terminations_from_senate(text: str, header_date: str) -> Optional[Dict]:
    """
    Parse Senate chamber-exit text: resignation ("vacancy created by the resignation of Senator X",
    "letters of resignation from former Senator X") or death ("vacancy caused by the death of Senator X").
    Returns {member: str, effective_date: str} or None if not found.
    Used when a Senator leaves Congress; caller must derive REMOVED for each committee.
    """
    raw = None
    m = SENATE_RESIGNATION_LETTERS_RE.search(text)
    if m:
        raw = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;")
    if not raw:
        m = SENATE_RESIGNATION_VACANCY_RE.search(text)
        if m:
            raw = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;")
            # Remove trailing " of State" if present in capture
            raw = re.sub(r"\s+of\s+[A-Za-z][A-Za-z\s]+$", "", raw, flags=re.IGNORECASE).strip()
    if not raw:
        m = SENATE_RESIGNATION_CERTIFICATE_RE.search(text)
        if m:
            raw = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;")
    if not raw:
        m = SENATE_DEATH_VACANCY_RE.search(text)
        if m:
            raw = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;")
            raw = re.sub(r"\s+of\s+[A-Za-z][A-Za-z\s]+$", "", raw, flags=re.IGNORECASE).strip()
    if not raw:
        return None
    member = _normalize_senate_resignation_name(raw)
    if not member or len(member) < 4:
        return None
    effective_date = _parse_effective_date(text) or header_date
    return {"member": member, "effective_date": effective_date}


# Pattern for death-resolution text: "S. Res. 619" or "S.J.Res. 42" or "A resolution relative to the death"
DEATH_RESOLUTION_START_RE = re.compile(
    r"(?i)(S\.\s*Res\.\s*\d+|S\.\s*J\.\s*Res\.\s*\d+|A\s+resolution\s+relative\s+to\s+the\s+(?:death|passing))"
)


def narrow_death_resolution_span(text: str, max_len: int = 1500) -> str:
    """
    For death-of-member CREC passages, return a narrower span starting at the
    resolution text (S. Res. NNN or "A resolution relative to the death")
    rather than the co-sponsor introduction ("By Mr. FLAKE (for himself...").
    """
    if not text:
        return ""
    m = DEATH_RESOLUTION_START_RE.search(text)
    if m:
        start = m.start()
        return text[start : start + max_len]
    return text[:5000]


def parse_crec_terminations_from_death(text: str, header_date: str) -> Optional[Dict]:
    """
    Parse death-of-member announcement text (GPO 81-84, 115-119).
    Returns {member: str, effective_date: str} or None if not found.
    Used when a member dies; caller must derive REMOVED for each committee.
    """
    # Try "Senator Name, of State" first (e.g. "FRANK R. LAUTENBERG, of New Jersey").
    m_name_of = DEATH_OF_MEMBER_NAME_COMMA_OF_STATE_RE.search(text)
    if m_name_of:
        raw = re.sub(r"\s+", " ", m_name_of.group(1)).strip(" ,.;")
    else:
        m_comma = DEATH_OF_MEMBER_WITH_STATE_COMMA_RE.search(text)
        if m_comma:
            part1 = re.sub(r"\s+", " ", m_comma.group(1)).strip(" ,.;")
            part2 = re.sub(r"\s+", " ", m_comma.group(2)).strip(" ,.;")
            # If part2 is just "of" (e.g. "Lautenberg, of New Jersey"), use only part1.
            if part2 and part2.lower() != "of":
                # If part1 already ends with part2 (e.g. "FRANK R. LAUTENBERG", "LAUTENBERG"), use part1 only.
                if part1 and (part1.endswith(part2) or part1.split()[-1] == part2):
                    raw = part1
                else:
                    raw = f"{part1} {part2}" if part1 else part2
            else:
                raw = part1 or part2
        else:
            m = (
                DEATH_OF_MEMBER_WITH_STATE_RE.search(text)
                or DEATH_OF_MEMBER_NO_STATE_RE.search(text)
                or DEATH_OF_MEMBER_GENTLEMAN_RE.search(text)
            )
            if not m:
                return None
            # Gentleman pattern can have name in group 1 or 2 (often just "Nunnelee" / "Takai")
            raw = m.group(1) if m.lastindex >= 1 and m.group(1) else (m.group(2) if m.lastindex >= 2 else "")
    member = re.sub(r"\s+", " ", raw).strip(" ,.;")
    # Defensive cleanup for no-state fallback patterns that may include "of [State]".
    member = re.sub(r"\s+of\s+[A-Za-z][A-Za-z\s]+$", "", member, flags=re.IGNORECASE)
    # Reject captures that are clearly sentence fragments, not person names.
    tokens = [t.strip(".,") for t in member.split() if t.strip(".,")]
    if len(tokens) < 1:
        return None
    # Reject likely truncated split (e.g. CREC header "DEATH OF THE HONORABLE FRANK R. " in one item, "LAUTENBERG, SENATOR FROM..." in next).
    if len(tokens) == 2 and len(tokens[1]) <= 2 and tokens[1][0].isalpha():
        return None
    # Single-token (e.g. "Nunnelee" from "gentleman from ... (Mr. Nunnelee)") is acceptable
    if len(tokens) < 2 and len(tokens) == 1 and not tokens[0][0].isupper():
        return None
    if len(tokens) > 6:
        return None
    lower_particles = {"de", "la", "van", "von", "del", "da", "di", "du"}
    suffixes = {"jr", "sr", "ii", "iii", "iv"}
    non_name_words = {
        "none",
        "served",
        "would",
        "agree",
        "earlier",
        "week",
        "certain",
        "anyone",
        "respected",
        "lawmaker",
        "resident",
        "community",
    }
    for tok in tokens:
        if tok.lower() in non_name_words:
            return None
        if tok.lower() in lower_particles:
            continue
        if tok.lower() in suffixes:
            continue
        if re.fullmatch(r"[A-Za-z]\.", tok):
            continue
        if re.fullmatch(r"[A-Za-z](?:\.[A-Za-z])+\.?", tok):
            continue
        if not tok[0].isupper():
            return None
    if not member or len(member) < 3:
        return None
    effective_date = _parse_effective_date(text) or header_date
    return {"member": member, "effective_date": effective_date}


def parse_crec_speaker_election(text: str, header_date: str) -> Optional[Dict]:
    """
    Parse announcement that a member was elected Speaker of the House.
    Returns {member: str, effective_date: str} or None.
    Caller must derive REMOVED for each of the member's committees.
    """
    m = SPEAKER_ELECTION_RE.search(text) or SPEAKER_ELECTION_ALT_RE.search(text)
    if not m:
        return None
    member = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;")
    member = re.sub(r"\s+of\s+the\s+State\s+of\s+[A-Za-z\s]+$", "", member, flags=re.IGNORECASE).strip()
    if not member or len(member) < 4:
        return None
    effective_date = _parse_effective_date(text) or header_date
    return {"member": member, "effective_date": effective_date}


def parse_crec_transfer(text: str, header_date: str) -> Optional[Dict]:
    """
    Parse "relieved from Committee on X and appointed/transferred to Committee on Y"
    or "transferred to the Committee on X". Returns {member, from_committee?, to_committee, effective_date}
    or None. from_committee is the committee they are leaving; to_committee is the one they are joining.
    If only to_committee is present, caller should REMOVED from all of member's committees except to_committee.
    """
    member = _parse_resignation_member(text)
    if not member:
        m_i = TRANSFER_MEMBER_I_REQUEST_RE.search(text)
        if m_i:
            member = m_i.group(1).strip()
        if not member:
            m_g = TRANSFER_GENTLEMAN_RE.search(text)
            if m_g:
                member = (m_g.group(1) or m_g.group(2) or "").strip()
    if not member:
        return None
    effective_date = _parse_effective_date(text) or header_date
    from_comm = None
    to_comm = None
    m = TRANSFER_RELIEVED_APPOINTED_RE.search(text)
    if m:
        from_comm = _canonicalize_committee_label("Committee on " + m.group(1).strip() if not re.match(r"(?i)^(?:the\s+)?Committee\s+on\s+", m.group(1)) else m.group(1).strip())
        to_comm = _canonicalize_committee_label("Committee on " + m.group(2).strip() if not re.match(r"(?i)^(?:the\s+)?Committee\s+on\s+", m.group(2)) else m.group(2).strip())
    else:
        m2 = TRANSFER_TO_ONLY_RE.search(text)
        if m2:
            raw = m2.group(1).strip()
            to_comm = _canonicalize_committee_label("Committee on " + raw if not re.match(r"(?i)^(?:the\s+)?Committee\s+on\s+", raw) else raw)
    if not to_comm:
        return None
    result: Dict = {"member": member, "to_committee": to_comm, "effective_date": effective_date}
    if from_comm:
        result["from_committee"] = from_comm
    return result


def parse_crec_terminations(
    text: str, header_date: str
) -> List[Dict]:
    """
    Parse resignation text. Returns list of:
    {committee: str, member: str, effective_date: str}
    Uses GPO dateline for effective_date; falls back to header_date.

    Handles three formats:
    1. "laid before the House the following resignation(s) as a member of the Committee on X"
    2. GPO "Re resignation from committee." (subject line) or "Re resignation from the Committee on X"
    3. Inline letter/floor-remark termination where departure phrase and committee mention co-occur.
    """
    results = []
    if not has_committee_termination_signal(text):
        return results
    member = _parse_resignation_member(text)
    effective_date = _parse_effective_date(text) or header_date

    if not member:
        return results

    # Format 1: preamble lists committee(s)
    m = COMMITTEE_TERMINATION_PREAMBLE_RE.search(text)
    if m:
        raw_committees = m.group(1)
        committees = _split_committee_names(raw_committees)
        for comm in committees:
            comm_clean = _canonicalize_committee_label(comm)
            comm_clean = _trim_trailing_committee_clause(comm_clean)
            if comm_clean:
                results.append({
                    "committee": comm_clean,
                    "member": member,
                    "effective_date": effective_date,
                })
        return results

    # Format 2: GPO "Re resignation from committee" (subject line in letter).
    re_m = RE_TERMINATION_FROM_COMMITTEE_RE.search(text)

    committees = []
    if re_m and re_m.group(1):
        # "Re resignation from the Committee on X" - committee in subject
        raw = re_m.group(1).strip().strip(".:")
        if raw:
            committees = _split_committee_names(f"Committee on {raw}")
    if not committees:
        # Format 3: inline/body declarative variants with tight verb+committee proximity.
        committees = _collect_inline_body_committees(text)

    for comm in committees:
        comm_clean = _canonicalize_committee_label(comm)
        comm_clean = _trim_trailing_committee_clause(comm_clean)
        if comm_clean:
            results.append({
                "committee": comm_clean,
                "member": member,
                "effective_date": effective_date,
            })
    return results


def _normalize_member_tokens(raw_members: List[str]) -> List[str]:
    """Normalize raw member text snippets into resolver-friendly labels."""
    state_phrases = {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
        "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
        "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
        "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
        "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
        "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
        "wisconsin", "wyoming", "district of columbia", "puerto rico", "guam", "american samoa",
        "u.s. virgin islands", "virgin islands", "northern mariana islands",
    }
    names: List[str] = []
    for n in raw_members:
        n = (n or "").strip()
        if not n:
            continue
        # Clean off leading titles/noise.
        n = re.sub(r"(?i)^(?:the\s+)?(the Honorable|Representative|Representatives|Senator|Senators)\s+", "", n)
        n = re.sub(r"(?i)^members?\s+", "", n)
        # Remove trailing role labels.
        n = re.sub(r"(?i)\s*(Chairman|Chair|Vice Chair|Co-Chairman|Ranking Member)$", "", n)
        n = n.strip(" ,.;:-")
        if len(n) <= 2:
            continue
        if re.sub(r"\s+", " ", n.lower()) in state_phrases:
            continue
        # Keep strings that look name-like for resolver.
        if " " in n or "." in n:
            names.append(n)
    return names


def _split_raw_member_parts(text: str) -> List[str]:
    """Split roster text into candidate member strings."""
    if not text:
        return []
    work = re.sub(r"(?i)\band\b", ",", text)
    parts: List[str] = []
    for chunk in re.split(r"[;,]", work):
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        # OCR often drops delimiters between titled names; split on repeated title starts.
        titled = re.split(r"(?=(?:Mr|Mrs|Ms|Miss)\.\s+[A-Z])", chunk)
        for piece in titled:
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return parts


def _extract_names_before_committee(segment: str, committee_start: int) -> List[str]:
    """
    Extract names when text is in the form:
      "... appointed Mr. X, Ms. Y to the Committee on Z."
    """
    prefix = (segment[:committee_start] or "").strip(" ,.;:-")
    if not prefix:
        return []
    # Only trust this path when explicit member titles appear.
    if not re.search(r"(?i)\b(Mr|Mrs|Ms|Miss|Representative|Representatives|Senator|Senators)\.?\b", prefix):
        return []
    raw_names = _split_raw_member_parts(prefix)
    return _normalize_member_tokens(raw_names)


def parse_crec_text(text: str) -> List[Dict]:
    """
    Parses a single CREC text block and extracts committee appointments.
    Yields dictionaries like: {'committee': 'Name', 'members': ['Name 1', 'Name 2']}
    """
    results = []
    # Replace structural newlines but keep them recognizable if needed. 
    # Actually, a literal newline might be a good separator.
    # Let's use spaces for easier regex across lines.
    text_clean = re.sub(r'\s+', ' ', text).strip()
    
    # Split by "appoint" / "reappoint" / "appointment" etc.
    segments = re.split(r'(?i)\b(?:re)?appoint(?:s|ment|ed|ing)?\b', text_clean)
    if len(segments) <= 1:
        return []
        
    for segment in segments[1:]:
        # Improved regex to handle "Member of the House/Senate to the ..."
        # and to be more flexible with the leading phrase.
        # Use \.(?!\s*[A-Z]) to avoid splitting on "Mr." / "Mrs." etc.
        pattern = re.search(
            r'(?:to the|as members of|member(?:s)? of the (?:House|Senate) on the part of the House to the|on the part of the House to the|as a member of the) (.*?)(?::|\.(?!\s*[A-Z]))\s*(.*)',
            segment, re.IGNORECASE
        )
        if not pattern:
            # Fallback for simpler "to the X"
            pattern = re.search(
                r'(?:to|of) (?:the )?(.*?)(?::|\.(?!\s*[A-Z]))\s*(.*)',
                segment, re.IGNORECASE
            )
            if not pattern:
                continue
            
        committee_raw = pattern.group(1).strip()
        # GPO 1524: "vice [departed member]" = replacement; extract before stripping
        # Stop at ": " or ".\s" or ",\s" or end (avoid period in "Mr.")
        vice_match = re.search(r'(?i)\s+vice\s+(.+?)(?=:\s|\.\s*$|,\s*$|\s*$)', committee_raw)
        replaced_member = vice_match.group(1).strip() if vice_match else None
        if replaced_member:
            replaced_member = _clean_member_label(replaced_member)
        if not replaced_member:
            vacancy_match = VACANCY_CAUSED_BY_RE.search(segment)
            if vacancy_match:
                replaced_member = _clean_member_label(vacancy_match.group(1))
        # Clean up committee name - remove trailing junk like "during the 113th Congress" or "for a term..."
        # Do NOT split on "on the" — it is part of official names (e.g. "Committee on the Budget")
        committee = re.split(
            r'(?i)\s+(?:during|for a|pursuant|effective|vice|as |to fill the vacancy)\b',
            committee_raw,
        )[0].strip().strip(".,")
        
        members_block = pattern.group(2).strip()
        names = _extract_names_before_committee(segment, pattern.start())
        
        # Determine the end of the members block
        stop_match = re.search(r'(?:\.)?\s*(?:The message|The Chair|Enrolled Bills|vice |The Senator).*', members_block, re.IGNORECASE)
        if stop_match:
            members_block = members_block[:stop_match.start()].strip()
            
        if members_block.endswith('.'):
            members_block = members_block[:-1].strip()
            
        if members_block:
            raw_names = _split_raw_member_parts(members_block)
            names.extend(_normalize_member_tokens(raw_names))
        # Preserve order while removing duplicates.
        if names:
            seen_names: set[str] = set()
            deduped: List[str] = []
            for name in names:
                key = name.lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                deduped.append(name)
            names = deduped
                    
        if committee and names:
            item = {'committee': committee, 'members': names}
            if replaced_member and len(replaced_member) > 2:
                item['replaced_member'] = replaced_member
            results.append(item)
            
    return results
