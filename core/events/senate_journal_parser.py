"""
Senate Journal (GPO-SJOURNAL) parser for committee appointment fallback discovery.

This parser is intentionally conservative and APPOINT-only. It runs after
resolution + CREC ingestion as a last-pass catch layer, so precision is
favored over recall. Senate semantics are kept separate from House semantics:
Senate-specific appointment context, Senate-specific committee/name patterns,
and termination patterns used only to skip non-appoint blocks.

Output dicts: decision_date, member_raw, committee_raw, source_loc, text_span, chamber, congress.
"""

from __future__ import annotations

from datetime import date
import re
from pathlib import Path

from core.committees.resolver import committee_name_to_id
try:
    import pdfplumber  # type: ignore
except ImportError:
    pdfplumber = None

from ingest.utils import CONGRESS_DATES

# Reuse only low-level parser helpers shared across chambers.
from core.events.journal_parser import (
    _extract_page_text_by_columns,
    extract_date_from_page,
    extract_printed_page_from_page_text,
    _normalize_page_text,
    _parse_date,
    _congress_for_year,
    _trim_committee_name,
    _extract_titled_names,
    _member_raw,
    MAX_COMMITTEE_LEN,
    MAX_BLOCK_CHARS,
    _ROSTER_TITLES_AFTER_DASH,
    _MEMBER_VALID_RE,
)

VERBOSE = False


def set_verbose(flag: bool) -> None:
    global VERBOSE
    VERBOSE = flag


# --- Senate-specific quality gates --------------------------------------------

_MEMBER_STOPLIST_SENATE = frozenset(
    {"the", "president", "vice", "senate", "ayes", "nays", "clerk", "journal", "tempore", "speaker"}
)
_SENATE_COMMITTEE_REQUIRED_PHRASES = (
    "committee on",
    "select committee on",
    "special committee on",
)
_SENATE_COMMITTEE_FORBIDDEN = re.compile(
    r"(?:\bH\.\s*R\.\s*\d*\b|\bS\.\s*\d+\b|\bA\s+bill\b|\bMESSAGE\s+FROM\b|\bROLL\s+CALL\b|"
    r"\bJournal\s+of\s+the\s+House\b|\bSpeaker\s+pro\s+tempore\b)",
    re.IGNORECASE,
)

# Senate-specific committee headers and appointment context.
_SENATE_COMMITTEE_PATTERN = re.compile(
    r"(?:Standing\s+)?(?:Select\s+|Special\s+)?Committee\s+on\s+(?:the\s+)?([^:\n—]{3,140}?)\s*"
    r"(?::|—|--|\.?\s*—|\.?\s*--|\.\s*(?=Mr\.|Mrs\.|Ms\.|Miss\.|(?:The\s+)?Senator\s+from)"
    r"|\s+(?=Mr\.|Mrs\.|Ms\.|Miss\.|(?:The\s+)?Senator\s+from))",
    re.IGNORECASE,
)
_SENATE_APPT_CONTEXT = re.compile(
    r"notwithstanding\s+(?:the\s+provisions\s+of\s+)?rule\s+XXV"
    r"|Standing\s+Committees"
    r"|Orders\s+for\s+Committee\s+Service"
    r"|to\s+constitute\s+the\s+(?:majority|minority)\s+party'?s?\s+membership"
    r"|shall\s+constitute\s+the\s+(?:majority|minority)\s+party'?s?\s+membership"
    r"|making\s+minority\s+party\s+appointments"
    r"|majority\s+party'?s?\s+membership\s+on\s+certain\s+committees",
    re.IGNORECASE,
)
_SENATE_TERMINATE_PATTERNS = [
    re.compile(r"relieved\s+from\s+further\s+service(?:\s+as\s+(?:a\s+)?member)?", re.IGNORECASE),
    re.compile(r"excused\s+from\s+further\s+attendance\s+upon\s+the\s+committees\s+named", re.IGNORECASE),
    re.compile(r"excused\s+from\s+further\s+service\s+on\s+the\s+committee", re.IGNORECASE),
    re.compile(r"resign(?:ation)?\s+(?:from\s+)?(?:the\s+)?committee", re.IGNORECASE),
]
_SENATE_SENATOR_FROM = re.compile(
    r"(?:The\s+)?Senator\s+from\s+([A-Za-z\s]+?),?\s+(?:Mr\.|Mrs\.|Ms\.)\s+([A-Za-z\-']+)",
    re.IGNORECASE,
)
_SENATE_SECTION_STOP = re.compile(
    r"\bYEAS\s+---|\bNAYS\s+---|\bRollcall\s+Vote\b|\bThe\s+question\s+being\s+on\b|"
    r"\bResolved,\s+That\s+the\s+Senate\s+agree\s+thereto\b|\bOn\s+motion\s+by\b|"
    r"\bORDER\s+FOR\s+CONSIDERATION\b|\bHOUSE\s+BILL\s+READ\b|\bMORNING\s+BUSINESS\b|"
    r"\bMESSAGE\s+FROM\b",
    re.IGNORECASE,
)
_SENATE_BLOCK_FORBIDDEN = re.compile(
    r"\bfor\s+himself\b|\bfor\s+herself\b|\bA\s+bill\b|\bto\s+provide\b|"
    r"\bintroduced,\s+read\s+the\s+first\b|\bplaced\s+on\s+the\s+calendar\b|"
    r"\bheld\s+at\s+the\s+desk\b|\bEC-\d+\b",
    re.IGNORECASE,
)


def passes_quality_gates_senate(committee_raw: str, member_raw: str) -> tuple[bool, str]:
    """Senate version: Senate committee checks + Senate member stoplist."""
    c = (committee_raw or "").strip()
    if len(c) > MAX_COMMITTEE_LEN:
        return (False, "committee_too_long")
    c_lower = c.lower()
    if not any(phrase in c_lower for phrase in _SENATE_COMMITTEE_REQUIRED_PHRASES):
        return (False, "committee_no_required_phrase")
    if _SENATE_COMMITTEE_FORBIDDEN.search(c):
        return (False, "committee_has_bill_tokens")
    m = (member_raw or "").strip()
    if not m:
        return (False, "member_regex_fail")
    m_match = re.match(r"^(?:Mr|Mrs|Ms)\.\s+(\S+)", m, re.IGNORECASE)
    if m_match:
        first_token = m_match.group(1).rstrip(".,;").lower()
        if first_token in _MEMBER_STOPLIST_SENATE:
            return (False, "member_stopword")
    if not _MEMBER_VALID_RE.match(m):
        return (False, "member_regex_fail")
    return (True, "")


def _looks_like_appointment_senate(text: str, context_active: bool = False) -> bool:
    if _SENATE_APPT_CONTEXT.search(text):
        return True
    if _ROSTER_TITLES_AFTER_DASH.search(text):
        return True
    if context_active and re.search(r"\b(?:Mr\.|Mrs\.|Ms\.)\s+[A-Z][A-Za-z'\-]+(?:\s*,\s*(?:Mr\.|Mrs\.|Ms\.)\s+[A-Z])", text):
        return True
    if re.search(r"\bordered\s+that\s+the\s+following\b|\bcommittee\s+elected\b", text, re.IGNORECASE):
        return True
    return False


def _block_is_termination(block_text: str, page_prefix: str) -> bool:
    combined = (page_prefix + " " + block_text).lower()
    return any(p.search(combined) for p in _SENATE_TERMINATE_PATTERNS)


def _extract_senate_names(block: str) -> list[tuple[str | None, str, str | None]]:
    """Yield (first_name, last_name, state_phrase) from Senate member styles."""
    out: list[tuple[str | None, str, str | None]] = []
    out.extend(_extract_titled_names(block))
    for sm in _SENATE_SENATOR_FROM.finditer(block):
        state = (sm.group(1) or "").strip() or None
        last = (sm.group(2) or "").strip()
        if last and len(last) >= 2:
            out.append((None, last, state))
    return out


def _normalize_senate_committee_raw(raw: str) -> str:
    """Clean common OCR joins in Senate committee names."""
    cleaned = _trim_committee_name(raw)
    cleaned = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\r\n,.:;–—")
    if len(cleaned) > MAX_COMMITTEE_LEN:
        cleaned = cleaned[: MAX_COMMITTEE_LEN].rsplit(" ", 1)[0] or cleaned[: MAX_COMMITTEE_LEN]
    return cleaned


# --- detect_actions_on_page (Senate) ------------------------------------------

def detect_actions_on_page_senate(
    page_text: str,
    printed_page: int,
    congress: int,
    default_year: int | None = None,
    page_date_override: str | None = None,
    context_active: bool = False,
) -> list[dict]:
    """
    Detect committee membership actions on a single Senate Journal page.
    Returns list of appointment dicts with chamber=S.
    """
    events: list[dict] = []
    if "committee" not in page_text.lower():
        return events
    event_date = extract_date_from_page(page_text, default_year=default_year)
    if not event_date and page_date_override:
        event_date = page_date_override
    source_loc = f"S{printed_page}"

    text = _normalize_page_text(page_text)
    if "committee" not in text.lower():
        return events

    seen_keys: set[tuple[str, str]] = set()

    for cm in _SENATE_COMMITTEE_PATTERN.finditer(text):
        fragment = _normalize_senate_committee_raw(cm.group(1))
        if not fragment:
            continue
        committee_raw = ("Committee on " + fragment) if "committee on" not in fragment.lower() else fragment
        # Only track real Senate committees; ignore procedural/body text fragments.
        if not committee_name_to_id(committee_raw, bill_type="sres"):
            continue
        start = cm.end()
        next_cm = _SENATE_COMMITTEE_PATTERN.search(text, start)
        block_end = next_cm.start() if next_cm else len(text)
        block_end = min(block_end, start + MAX_BLOCK_CHARS)
        block = text[start:block_end]
        section_stop = _SENATE_SECTION_STOP.search(block)
        if section_stop:
            block = block[: section_stop.start()]
        if _SENATE_BLOCK_FORBIDDEN.search(block):
            continue
        prefix_start = max(0, cm.start() - 300)
        page_prefix = text[prefix_start : cm.start()]
        if _block_is_termination(block, page_prefix):
            continue
        local_context = (page_prefix + " " + block[:220]).strip()
        if not _looks_like_appointment_senate(local_context, context_active=context_active):
            continue
        parsed_names = _extract_senate_names(block)
        if len(parsed_names) < 2:
            continue

        added: set[tuple[str, str]] = set()

        def add_event(ev_last: str, ev_state: str | None, ev_first: str | None = None) -> None:
            key = (ev_last.strip().lower(), committee_raw.lower())
            if key in added:
                return
            added.add(key)
            if (committee_raw.lower(), ev_last.strip().lower()) in seen_keys:
                return
            seen_keys.add((committee_raw.lower(), ev_last.strip().lower()))
            member_raw_str = _member_raw(ev_first, ev_last.strip(), ev_state)
            if not passes_quality_gates_senate(committee_raw, member_raw_str)[0]:
                return
            events.append({
                "decision_date": _parse_date(event_date) or event_date,
                "member_raw": member_raw_str,
                "committee_raw": committee_raw,
                "source_loc": source_loc,
                "text_span": (page_prefix + " " + block)[:500].strip(),
                "chamber": "S",
                "congress": congress,
            })

        for first, last, state in parsed_names:
            if last and len(last) >= 2:
                add_event(last, state, first)

    return events


# --- File discovery (GPO-SJOURNAL) --------------------------------------------

def get_gpo_senate_journal_files(base: Path, congress: int) -> list[Path]:
    """
    Discover GPO Senate Journal files for this congress.

    Layout: base / GPO-SJOURNAL-{congress} / *.pdf (e.g. GPO-SJOURNAL-113/
    GPO-CPUB-113spub21.pdf, GPO-CPUB-113spub23.pdf). If journal-only split
    files exist (e.g. GPO-CPUB-113spub21_3-898.pdf, GPO-CPUB-113spub23_3-873.pdf),
    those are preferred so we only parse the isolated journal content.
    """
    congress_dir = base / f"GPO-SJOURNAL-{congress}"
    if not congress_dir.exists():
        return []
    pdf_dir = congress_dir / "pdf"
    if pdf_dir.exists():
        candidates = list(pdf_dir.glob("*.pdf"))
    else:
        candidates = list(congress_dir.glob("*.pdf"))
    # Prefer journal-only splits (e.g. *_3-898.pdf) when present; skip full volumes then
    journal_only = [p for p in candidates if "_3-" in p.name]
    if journal_only:
        return sorted(journal_only)
    return sorted(candidates)


# --- Whole-file parse ---------------------------------------------------------

def _guess_decision_date_from_filename(file_path: Path) -> date:
    match = re.search(r"(\d{4})", file_path.name)
    if match:
        return date(int(match.group(1)), 1, 1)
    congress_match = re.search(r"(?:^|[^\d])(1\d{2})(?:[^\d]|$)", file_path.name)
    if congress_match:
        cno = int(congress_match.group(1))
        start, _ = CONGRESS_DATES.get(cno, (date(2013, 1, 1), date(2014, 12, 31)))
        return start
    return date(2013, 1, 1)


def parse_senate_journal_file(file_path: Path, max_pages: int | None = None) -> list[dict]:
    """
    Parse a single Senate Journal PDF. Uses same 3-column extraction as House.

    Only processes pages whose header contains "JOURNAL OF THE SENATE"; skips
    INDEX and other front/back matter (those pages have "INDEX" in the header).
    Senate has 2 PDFs per congress; both use 3-column layout.

    Returns list of appointment dicts with chamber=S.
    """
    if pdfplumber is None:
        return []

    year_match = re.search(r"(\d{4})", file_path.name)
    if year_match:
        year = int(year_match.group(1))
        congress = _congress_for_year(year)
    else:
        # Filename like GPO-CPUB-113spub23.pdf: use congress number
        congress_match = re.search(r"(?:^|[^\d])(1\d{2})(?:[^\d]|$)", file_path.name)
        congress = int(congress_match.group(1)) if congress_match else 113
        start, _ = CONGRESS_DATES.get(congress, (date(2013, 1, 1), date(2014, 12, 31)))
        year = start.year

    all_events: list[dict] = []
    seen: set[tuple[str, str]] = set()
    force_journal_pages = "_3-" in file_path.name
    last_page_date: str | None = None

    try:
        with pdfplumber.open(file_path) as pdf:
            pages_list = pdf.pages
            prev_has_phrase = False
            # Carry Senate appointment context across nearby pages because titles often
            # appear on one page/column and committee rosters continue on the next.
            appt_context_ttl = 0
            for page_num, page in enumerate(pages_list, 1):
                if max_pages is not None and page_num > max_pages:
                    break
                full_text = page.extract_text() or ""
                has_phrase = "JOURNAL OF THE SENATE" in (full_text or "").upper()
                next_has_phrase = False
                if page_num < len(pages_list):
                    next_text = pages_list[page_num].extract_text() or ""
                    next_has_phrase = "JOURNAL OF THE SENATE" in next_text.upper()
                # Use full-page text: page 1 has full-width title; interior pages with odd layout
                # (e.g. no repeated header) still count if flanked by journal pages
                is_journal_page = force_journal_pages or has_phrase or (prev_has_phrase and next_has_phrase)
                prev_has_phrase = is_journal_page
                if not is_journal_page:
                    continue
                if _SENATE_APPT_CONTEXT.search(full_text):
                    appt_context_ttl = 3
                context_active = appt_context_ttl > 0
                columns = _extract_page_text_by_columns(page)
                if not columns:
                    columns = [full_text] if full_text.strip() else []
                # Use full-page text for page/date metadata: in Senate 3-column layouts,
                # date headers can shift between columns on alternating pages.
                header_footer_text = columns[0] if columns else ""
                printed_page = extract_printed_page_from_page_text(full_text)
                if printed_page is None:
                    printed_page = extract_printed_page_from_page_text(header_footer_text)
                if printed_page is None:
                    printed_page = page_num
                page_date = extract_date_from_page(full_text, default_year=year)
                if page_date is None:
                    page_date = extract_date_from_page(header_footer_text, default_year=year)
                if page_date is not None:
                    last_page_date = page_date
                else:
                    page_date = last_page_date
                for col_text in columns:
                    if not col_text.strip():
                        continue
                    if "committee" not in col_text.lower():
                        continue
                    page_events = detect_actions_on_page_senate(
                        col_text,
                        printed_page=printed_page,
                        congress=congress,
                        default_year=year,
                        page_date_override=page_date,
                        context_active=context_active,
                    )
                    for ev in page_events:
                        key = (
                            (ev.get("committee_raw") or "").strip().lower(),
                            (ev.get("member_raw") or "").strip().lower(),
                        )
                        if key not in seen:
                            seen.add(key)
                            all_events.append(ev)
                if appt_context_ttl > 0:
                    appt_context_ttl -= 1
    except Exception:
        return []

    out: list[dict] = []
    for ev in all_events:
        d = ev.get("decision_date")
        if isinstance(d, str):
            d = _parse_date(d)
        if d is None or not hasattr(d, "isoformat"):
            continue
        out.append({
            "decision_date": d,
            "member_raw": ev.get("member_raw", ""),
            "committee_raw": ev.get("committee_raw", ""),
            "source_loc": ev.get("source_loc", file_path.name),
            "text_span": ev.get("text_span", ""),
            "chamber": "S",
            "congress": ev.get("congress", congress),
        })
    if VERBOSE:
        print(f"    [Senate Parser] {file_path.name} extracted {len(out)} appointments")
    return out
