"""
House Journal (GPO-HJOURNAL) parser for committee appointment/termination discovery.

Supports:
- parse_journal_file(file_path) -> list[dict]: whole-PDF parse (backward compatible).
- Per-page: discover_parts, build_page_ranges, iter_print_pages, detect_actions_on_page
  for citation H### and date-from-header. Loader can iterate by page when available.

Output dicts: decision_date, member_raw, committee_raw, source_loc, text_span; optional chamber, congress.
Evidence-only: patterns from research/house_journal_phrases.md and journal_parser_adoption_from_tracker.md.
"""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import re
from typing import Iterator

try:
    import pdfplumber  # type: ignore
except ImportError:  # pragma: no cover
    pdfplumber = None

from ingest.utils import CONGRESS_DATES

VERBOSE = False


def set_verbose(flag: bool) -> None:
    global VERBOSE
    VERBOSE = flag


# --- Part discovery and page ranges --------------------------------------------

def discover_parts(journal_pdf_dir: Path) -> list[tuple[str, Path]]:
    """
    Discover GPO-HJOURNAL-{year}-*.pdf parts in journal_pdf_dir.
    Returns list of (part_key, path) sorted (e.g. '1', '2-1', '2-2').
    """
    if not journal_pdf_dir.is_dir():
        return []
    seen: set[str] = set()
    out: list[tuple[str, Path]] = []
    for p in journal_pdf_dir.iterdir():
        if p.suffix.lower() != ".pdf" or not p.name.startswith("GPO-HJOURNAL-"):
            continue
        rest = p.name.replace("GPO-HJOURNAL-", "").replace(".pdf", "").strip()
        parts = rest.split("-", 1)
        if len(parts) != 2:
            continue
        part_key = parts[1].strip()
        if part_key in seen:
            continue
        seen.add(part_key)
        out.append((part_key, p))

    def sort_key(item: tuple[str, Path]) -> tuple[int, int]:
        k = item[0]
        if "-" not in k:
            return (int(k), 0)
        a, b = k.split("-", 1)
        return (int(a), int(b))

    out.sort(key=sort_key)
    return out


def build_page_ranges(journal_pdf_dir: Path, year: int) -> list[tuple[int, int, Path, str, int]]:
    """
    Build (start_print_page, end_print_page, pdf_path, part_key, n_pdf) for each part.
    Uses "Pages X to Y" on first page when present; else infers from previous part.
    """
    lib = pdfplumber
    if lib is None:
        return []
    parts = discover_parts(journal_pdf_dir)
    if not parts:
        return []

    ranges: list[tuple[int, int, Path, str, int]] = []
    for part_key, path in parts:
        try:
            with lib.open(path) as pdf:
                n_pages = len(pdf.pages)
                first_text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
        except Exception:
            continue

        m = re.search(r"Pages?\s+(\d+)\s+(?:to\s+)?(\d+)?", first_text, re.IGNORECASE)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start
            if not m.group(2) and n_pages > 1:
                end = start + n_pages - 1
        else:
            prev = ranges[-1] if ranges else None
            prev_span = (prev[1] - prev[0] + 1) if prev else 0
            prev_n_pdf = prev[4] if prev else 0
            # Cover/TOC part: prev has huge range but few PDF pages; this part has many pages → new volume
            if prev and prev_span > 100 and prev_n_pdf < 20 and n_pages > 100:
                start = 1
                end = n_pages
            else:
                prev_end = prev[1] if prev else 0
                start = prev_end + 1
                end = prev_end + n_pages

        ranges.append((start, end, path, part_key, n_pages))
    return ranges


# GPO House Journal: 3-column layout on letter-size (612pt wide)
_COLUMN_BOUNDARIES = [(0, 210), (210, 395), (395, 612)]
# Safe default keeps historical behavior; set JOURNAL_COLUMN_MODE=words to opt into
# one-pass extraction experiments.
_COLUMN_MODE = os.environ.get("JOURNAL_COLUMN_MODE", "crop").strip().lower()


def _extract_page_text_by_columns_crop(page) -> list[str]:
    """Extract text per column using page.crop(); returns list of column texts (order preserved)."""
    w = getattr(page, "width", 612)
    boundaries = [
        (x0, min(x1, w))
        for x0, x1 in _COLUMN_BOUNDARIES
        if x0 < w
    ]
    out: list[str] = []
    for x0, x1 in boundaries:
        try:
            cropped = page.crop((x0, 0, x1, page.height))
            col_text = cropped.extract_text() or ""
            if col_text.strip():
                out.append(col_text)
        except Exception:
            pass
    return out


def _column_index_for_span(x0: float, x1: float, boundaries: list[tuple[float, float]]) -> int | None:
    mid = (x0 + x1) / 2.0
    for idx, (start, end) in enumerate(boundaries):
        if start <= mid < end:
            return idx
    return None


def _extract_page_text_by_columns_words(page) -> list[str]:
    """
    Extract text per column from one char pass, then use pdfplumber's own text
    collation on each column subset.
    This avoids three separate crop+extract_text operations per page.
    """
    w = float(getattr(page, "width", 612))
    boundaries = [
        (float(x0), float(min(x1, w)))
        for x0, x1 in _COLUMN_BOUNDARIES
        if x0 < w
    ]
    if not boundaries:
        return []
    lib = pdfplumber
    text_utils = getattr(lib, "utils", None) if lib is not None else None
    extract_text = getattr(text_utils, "extract_text", None) if text_utils is not None else None
    if extract_text is None:
        return []
    chars = getattr(page, "chars", None) or []
    if not chars:
        return []

    cols: list[list[dict]] = [[] for _ in boundaries]
    for ch in chars:
        try:
            x0 = float(ch.get("x0", 0.0))
            x1 = float(ch.get("x1", x0))
        except (TypeError, ValueError):
            continue
        col_idx = _column_index_for_span(x0, x1, boundaries)
        if col_idx is None:
            continue
        cols[col_idx].append(ch)

    out: list[str] = []
    for col_chars in cols:
        if not col_chars:
            continue
        try:
            col_text = (extract_text(col_chars, x_tolerance=1, y_tolerance=3) or "").strip()
        except Exception:
            col_text = ""
        if col_text:
            out.append(col_text)
    return out


def _extract_page_text_by_columns(page) -> list[str]:
    if _COLUMN_MODE == "crop":
        return _extract_page_text_by_columns_crop(page)
    words_out = _extract_page_text_by_columns_words(page)
    if words_out:
        return words_out
    return _extract_page_text_by_columns_crop(page)


def extract_printed_page_from_page_text(text: str) -> int | None:
    """Parse printed page number from footer (last line digits). Returns None if not found."""
    if not text or not text.strip():
        return None
    m = re.search(r"(\d+)\s*$", text.strip())
    return int(m.group(1)) if m else None


def extract_date_from_page(text: str, default_year: int | None = None) -> str | None:
    """Extract legislative date from page. Returns YYYY-MM-DD or None."""
    months = {
        "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
        "MAY": 5, "JUNE": 6, "JULY": 7, "AUGUST": 8,
        "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
    }
    m = re.search(
        r"(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),?\s+"
        r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
        r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2}),?\s+(\d{4})",
        text,
        re.IGNORECASE,
    )
    if m:
        mo = months.get(m.group(1).upper())
        if mo is not None:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"
    m = re.search(
        r"JOURNAL\s+OF\s+THE\s+(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
        r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2})\s+(\d{4})",
        text,
        re.IGNORECASE,
    )
    if m:
        mo = months.get(m.group(1).upper())
        if mo is not None:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"
    m = re.search(
        r"JOURNAL\s+OF\s+THE\s+(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
        r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2})\b",
        text,
        re.IGNORECASE,
    )
    if m and default_year is not None:
        mo = months.get(m.group(1).upper())
        if mo is not None:
            return f"{default_year}-{mo:02d}-{int(m.group(2)):02d}"
    # House header sometimes shows only "JANUARY 3" (year appears on adjacent pages).
    header_window = " ".join((text or "").splitlines()[:4])
    m = re.search(
        r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
        r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2})\b",
        header_window,
        re.IGNORECASE,
    )
    if m and default_year is not None:
        mo = months.get(m.group(1).upper())
        if mo is not None:
            return f"{default_year}-{mo:02d}-{int(m.group(2)):02d}"
    return None


def extract_header_year_from_page(text: str) -> int | None:
    """Extract House header year token (e.g., leading '2013' in top-left header)."""
    if not text:
        return None
    header_lines = "\n".join(text.splitlines()[:4])
    m = re.search(r"(?m)^\s*((?:19|20)\d{2})\b", header_lines)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def iter_print_pages(
    page_ranges: list[tuple[int, int, Path, str, int]],
    year: int,
) -> Iterator[tuple[int, str, str | None]]:
    """
    Yield (printed_page_num, col_text, page_date) for each column of each printed page.
    Date is taken from full-page text (header); each yield is one column so detect_actions_on_page
    runs on column-isolated text and avoids column bleed.
    """
    lib = pdfplumber
    if lib is None:
        return
    last_page_date: str | None = None
    for start, end, path, part_key, n_pdf in page_ranges:
        if (end - start + 1) > 20 and n_pdf < 20:
            continue
        try:
            with lib.open(path) as pdf:
                for i in range(min(n_pdf, end - start + 1)):
                    if i >= len(pdf.pages):
                        break
                    print_page = start + i
                    if print_page > end:
                        break
                    page = pdf.pages[i]
                    full_text = page.extract_text() or ""
                    parsed = extract_printed_page_from_page_text(full_text)
                    if parsed is not None:
                        print_page = parsed
                    page_date = extract_date_from_page(full_text, default_year=year)
                    if page_date is not None:
                        last_page_date = page_date
                    else:
                        page_date = last_page_date
                    columns = _extract_page_text_by_columns(page)
                    for col_text in columns if columns else [full_text]:
                        yield (print_page, col_text, page_date)
        except Exception:
            continue


def get_single_print_page_text(
    page_ranges: list[tuple[int, int, Path, str, int]],
    year: int,
    target_print_page: int,
) -> tuple[str, str | None] | None:
    """Return (combined_page_text, page_date) for target printed page, or None. Text is column texts joined with \\n\\n; date from full page."""
    lib = pdfplumber
    if lib is None:
        return None
    for start, end, path, part_key, n_pdf in page_ranges:
        if (end - start + 1) > 20 and n_pdf < 20:
            continue
        if not (start <= target_print_page <= end):
            continue
        pdf_index = target_print_page - start
        if pdf_index >= n_pdf:
            continue
        try:
            with lib.open(path) as pdf:
                if pdf_index >= len(pdf.pages):
                    continue
                page = pdf.pages[pdf_index]
                full_text = page.extract_text() or ""
                page_date = extract_date_from_page(full_text, default_year=year)
                columns = _extract_page_text_by_columns(page)
                combined = "\n\n".join(columns) if columns else full_text
                return (combined, page_date)
        except Exception:
            continue
    return None


# --- Text normalization -------------------------------------------------------

def _normalize_page_text(text: str) -> str:
    """Apply hyphenated-name fix, de-smash, and hyphen removal. Idempotent for regex passes."""
    if not text:
        return ""
    # Join hyphenated all-caps words before general hyphen removal (MIL- LER -> MILLER)
    text = re.sub(r"\b([A-Z]{2,5})-\s+([A-Z]{2,})\b", r"\1\2", text)
    text = re.sub(r"-\s+", "", text)
    # De-smash
    text = re.sub(r"(?<=[A-Za-z])of(?=\s*[A-Z])", " of ", text)
    text = re.sub(r"(?<=[a-z])and(?=[A-Z])", " and ", text)
    text = re.sub(r"(?i)\b(Mr|Mrs|Ms|Miss)\.(?=[A-Za-z])", r"\1. ", text)
    text = text.replace("COMMITTEEELECTION", "COMMITTEE ELECTION")
    text = re.sub(
        r"PERMANENTSELECTCOMMITTEEON(?=\s*INTELLIGENCE)",
        "PERMANENT SELECT COMMITTEE ON ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"SELECTCOMMITTEEON(?=\s*INTELLIGENCE)",
        "SELECT COMMITTEE ON ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(COMMITTEE\s+ON)\s*\n\s*(INTELLIGENCE)", r"\1 \2", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text


# --- Quality gates (committee + member) ---------------------------------------

MAX_COMMITTEE_LEN = 120

# Committee must contain one of these exact phrases (case-insensitive)
_COMMITTEE_REQUIRED_PHRASES = ("committee on", "select committee on", "permanent select committee on")

# Committee must NOT contain any of these (bill text, procedural headers, column bleed)
_COMMITTEE_FORBIDDEN = re.compile(
    r"\b(H\.R\.|S\.\s+\d+|A\s+bill|MESSAGE\s+FROM|Ordered,|That\s+pursuant\s+to\s+clause|rule\s+\d+|"
    r"AYES|NAYS|ROLL\s+CALL|Journal\s+of|Speaker\s+pro\s+tempore)\b",
    re.IGNORECASE,
)

# Member must match this (tight: Mr./Mrs./Ms. + Name + optional " of State")
_MEMBER_VALID_RE = re.compile(
    r"^(Mr|Mrs|Ms)\.\s+[A-Z][A-Za-z''\-]+(?:\s+of\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)?$",
    re.IGNORECASE,
)

# Reject these as member names (procedural tokens)
_MEMBER_STOPLIST = frozenset(
    {"the", "speaker", "house", "representatives", "ayes", "nays", "clerk", "journal", "tempore"}
)


def passes_quality_gates(committee_raw: str, member_raw: str) -> tuple[bool, str]:
    """
    Return (True, "") if candidate passes; else (False, reason).
    Reasons: committee_too_long, committee_no_required_phrase, committee_has_bill_tokens,
             member_stopword, member_regex_fail.
    """
    c = (committee_raw or "").strip()
    if len(c) > MAX_COMMITTEE_LEN:
        return (False, "committee_too_long")
    c_lower = c.lower()
    if not any(phrase in c_lower for phrase in _COMMITTEE_REQUIRED_PHRASES):
        return (False, "committee_no_required_phrase")
    if _COMMITTEE_FORBIDDEN.search(c):
        return (False, "committee_has_bill_tokens")
    m = (member_raw or "").strip()
    if not m:
        return (False, "member_regex_fail")
    # First token after "Mr." / "Mrs." / "Ms." (the surname for stoplist)
    m_match = re.match(r"^(?:Mr|Mrs|Ms)\.\s+(\S+)", m, re.IGNORECASE)
    if m_match:
        first_token = m_match.group(1).rstrip(".,;").lower()
        if first_token in _MEMBER_STOPLIST:
            return (False, "member_stopword")
    if not _MEMBER_VALID_RE.match(m):
        return (False, "member_regex_fail")
    return (True, "")


# --- Patterns (strict delimiter, gating, terminate) ----------------------------

# Committee name then : or — or "The SPEAKER"/"Messrs." — NOT bare period. Capture at most 120 chars.
_COMMITTEE_PATTERN = re.compile(
    r"(?:Permanent\s+)?(?:Select\s+|Special\s+|Standing\s+)?Committee\s*on\s*(?:the\s+)?([^:\n—]{3,120}?)\s*"
    r"(?::|—|--|\.?\s*—|\.?\s*--|\s+(?:The\s+SPEAKER|Messrs\.|Mmes\.)\s)",
    re.IGNORECASE,
)

MAX_BLOCK_CHARS = 700

_APPT_FRAME = re.compile(
    r"\bannounced\s+that\s+the\s+speaker\b.*?\bappointed\b|\bthe\s+speaker\b.*?\bappointed\b"
    r"|\bpursuant\s+to\b.*?\bappointed\b|\bappointed\b.*?\bthe\s+following\b"
    r"|COMMITTEE\s+ELECTION",
    re.IGNORECASE | re.DOTALL,
)
_ROSTER_TITLES_AFTER_DASH = re.compile(r"[—-]\s*(?:Mr\.|Mrs\.|Ms\.|Miss)\s+", re.IGNORECASE)


def _looks_like_appointment(text: str) -> bool:
    if _ROSTER_TITLES_AFTER_DASH.search(text):
        return True
    if _APPT_FRAME.search(text):
        return True
    return False


_TERMINATE_PATTERNS = [
    re.compile(r"\bremoved\s+from\s+the\s+following\s+standing\s+committees", re.IGNORECASE),
    re.compile(r"\bresign(?:ation)?s?\s+(?:from\s+)?(?:the\s+)?committee", re.IGNORECASE),
    re.compile(r"\bdischarg(?:ed|es?)\s+(?:from\s+)?(?:the\s+)?committee", re.IGNORECASE),
    re.compile(r"\bremov(?:ed|es?)\s+(?:from\s+)?(?:the\s+)?committee", re.IGNORECASE),
    re.compile(r"\bstripped\s+(?:of\s+(?:his|her|their)\s+)?committee\s+assignment", re.IGNORECASE),
    re.compile(r"\bcease(?:s)?\s+to\s+be\s+(?:a\s+)?member\s+of\s+(?:the\s+)?committee", re.IGNORECASE),
    re.compile(r"\bno\s+longer\s+(?:a\s+)?member\s+of\s+(?:the\s+)?committee", re.IGNORECASE),
]


def _block_is_termination(block_text: str, page_prefix: str) -> bool:
    combined = (page_prefix + " " + block_text).lower()
    return any(p.search(combined) for p in _TERMINATE_PATTERNS)


# --- Member extraction --------------------------------------------------------

_TITLE_NAME = re.compile(
    r"\b(?:Mr\.|Mrs\.|Ms\.|Miss)\s+"
    r"([A-Z][A-Za-z'´`\-\.]+(?:\s+[A-Z][A-Za-z'´`\-\.]+){0,3})"
    r"(?:\s+of\s+([A-Za-z]+(?:\s+[A-Za-z]+)*))?",
)

# Mr./Mrs./Ms. LASTNAME, State (e.g. "Mr. ROGERS, Michigan, Chairman")
_NAME_PATTERN_COMMA_STATE = re.compile(
    r"\b(?:Mr\.|Mrs\.|Ms\.|Miss)\s+([A-Za-z\-'\u00c0-\u00ff]+)\s*,\s*([A-Za-z\s]+?)(?=[,.]|\s+and\s+|\s*$)",
    re.IGNORECASE,
)
# Mr./Mrs./Ms. LASTNAMEof State (no space before "of")
_NAME_PATTERN_OF_NO_SPACE = re.compile(
    r"\b(?:Mr\.|Mrs\.|Ms\.|Miss)\s+([A-Za-z\-'\u00c0-\u00ff]+?)of\s+([A-Za-z\s]+?)(?=[,.;)\s]|\s+and\s+|\s*$)",
    re.IGNORECASE,
)
_MESSRS_PATTERN = re.compile(r"\b(?:Messrs\.|Mmes\.|Mesdames)\s+(.+)", re.IGNORECASE)
_MESSRS_SKIP = frozenset(
    {"MESSRS", "MMES", "MESDAMES", "MRS", "CHAIRMAN", "CHAIR", "RANKING",
     "NEVADA", "YORK", "ALABAMA", "ARIZONA", "CALIFORNIA", "FLORIDA", "GEORGIA", "MICHIGAN", "NEW"}
)


def _extract_titled_names(text: str) -> list[tuple[str | None, str, str | None]]:
    """Yield (first_name, last_name, state_phrase). Fix OCR joins before matching."""
    t = re.sub(r"(?i)\b(Mr|Ms|Mrs|Miss)\.(?=[A-Z])", r"\1. ", text)
    t = re.sub(r"(?i)(?<=[a-z,])(?=(Mr|Ms|Mrs|Miss)\.)", " ", t)
    out: list[tuple[str | None, str, str | None]] = []
    for m in _TITLE_NAME.finditer(t):
        name = (m.group(1) or "").strip().rstrip(".,;")
        if not name:
            continue
        parts = name.split()
        last = parts[-1].rstrip(".,;") if parts else name
        first = " ".join(parts[:-1]).strip() or None if len(parts) > 1 else None
        state = (m.group(2) or "").strip() or None
        out.append((first, last, state))
    return out


def _member_raw(first: str | None, last: str, state: str | None) -> str:
    """Build member_raw for resolver (e.g. 'Mr. ROGERS of Michigan')."""
    if state:
        state = state.strip().lstrip("of ").strip()  # avoid "LAST of of Florida"
        raw = f"Mr. {last} of {state}" if state else f"Mr. {last}"
    else:
        raw = f"Mr. {last}"
    return re.sub(r"\s+of\s+of\s+", " of ", raw)


# --- Narrative appointment pass -----------------------------------------------

_NARR_APPT = re.compile(r"\bappointed\b", re.IGNORECASE)
# Capture committee name: at most 120 chars to avoid column bleed / paragraph swallow
_TO_COMMITTEE = re.compile(
    r"\bto\s+(?:the\s+)?((?:Permanent\s+)?(?:Select\s+|Special\s+|Standing\s+|Joint\s+)?(?:Committee|COMMITTEE)(?:\s+on\s+(?:the\s+)?)[^:.;\n—]{0,100})",
    re.IGNORECASE,
)
# End of member list: newline or semicolon; do not split on "Mr." / "Mrs." / "Ms."
_STOP_SENTENCE = re.compile(r"(?:\n|;\s+|$)")


def _extract_narrative_appointments(
    text: str,
    printed_page: int,
    event_date: str | None,
    source_loc: str,
) -> list[dict]:
    """Extract from 'Speaker appointed the following Members ... to the Permanent Select Committee on Intelligence: Messrs. ...'"""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _NARR_APPT.finditer(text):
        # Include context before "appointed" so gating phrase (announced that the Speaker...) is in window
        window_start = max(0, m.start() - 150)
        window = text[window_start : m.start() + 900]
        if "committee" not in window.lower():
            continue
        cm = _TO_COMMITTEE.search(window)
        if not cm:
            continue
        committee_raw = cm.group(1).strip().rstrip(" ,;.")
        seg_start = cm.end()
        seg = window[seg_start:]
        stop = _STOP_SENTENCE.search(seg)
        if stop:
            seg = seg[: stop.start()]
        seg = seg[:500].strip()
        if not _looks_like_appointment(window[: seg_start] + " " + seg):
            continue
        for first, last, state in _extract_titled_names(seg):
            if not last or len(last) < 2:
                continue
            key = (committee_raw.lower(), last.lower())
            if key in seen:
                continue
            seen.add(key)
            member_raw = _member_raw(first, last, state)
            committee_trimmed = committee_raw[:MAX_COMMITTEE_LEN].rsplit(" ", 1)[0] if len(committee_raw) > MAX_COMMITTEE_LEN else committee_raw
            if not passes_quality_gates(committee_trimmed, member_raw)[0]:
                continue
            out.append({
                "decision_date": event_date,
                "member_raw": member_raw,
                "committee_raw": committee_trimmed,
                "source_loc": source_loc,
                "text_span": (window[: seg_start] + " " + seg)[:500].strip(),
            })
    return out


# --- Roster pass (committee header + block) ------------------------------------

_CLAUSE_TRIM_RE = re.compile(r"\s+(?:so\s+that|because|in\s+order\s+to|to\s+[a-z]|which|that\s+(?:i|we))\b", re.IGNORECASE)


def _trim_committee_name(raw: str) -> str:
    cleaned = raw.strip(" \t\r\n,.:;–—")
    parts = _CLAUSE_TRIM_RE.split(cleaned)
    cleaned = (parts[0].strip() if parts else cleaned).strip(" \t\r\n,.:;–—")
    if len(cleaned) > MAX_COMMITTEE_LEN:
        cleaned = cleaned[: MAX_COMMITTEE_LEN].rsplit(" ", 1)[0] or cleaned[: MAX_COMMITTEE_LEN]
    return cleaned


def _parse_date(d: str | None) -> date | None:
    if not d or len(d) < 10:
        return None
    try:
        return date(int(d[:4]), int(d[5:7]), int(d[8:10]))
    except (ValueError, IndexError):
        return None


def detect_actions_on_page(
    page_text: str,
    printed_page: int,
    congress: int,
    default_year: int | None = None,
    page_date_override: str | None = None,
) -> list[dict]:
    """
    Detect committee membership actions on a single House Journal page.
    Returns list of appointment dicts: decision_date, member_raw, committee_raw, source_loc, text_span.
    Skips terminate blocks; only emits appoint. Uses strict delimiter and gating.
    """
    events: list[dict] = []
    if "committee" not in page_text.lower():
        return events
    event_date = extract_date_from_page(page_text, default_year=default_year)
    if not event_date and page_date_override:
        event_date = page_date_override
    source_loc = f"H{printed_page}"

    text = _normalize_page_text(page_text)
    if "committee" not in text.lower():
        return events

    # Narrative pass first
    for ev in _extract_narrative_appointments(text, printed_page, event_date, source_loc):
        ev["chamber"] = "H"
        ev["congress"] = congress
        if ev.get("decision_date"):
            ev["decision_date"] = _parse_date(ev["decision_date"]) or ev["decision_date"]
        events.append(ev)

    seen_keys = {(e.get("committee_raw", "").lower(), (e.get("member_raw", "").split()[-1] if e.get("member_raw") else "").lower()) for e in events}

    # Roster pass
    for cm in _COMMITTEE_PATTERN.finditer(text):
        fragment = _trim_committee_name(cm.group(1))
        if not fragment:
            continue
        # Roster pattern captures only the part after "Committee on "; prepend so gate and resolver see full name
        committee_raw = ("Committee on " + fragment) if "committee on" not in fragment.lower() else fragment
        start = cm.end()
        next_cm = _COMMITTEE_PATTERN.search(text, start)
        block_end = next_cm.start() if next_cm else len(text)
        block_end = min(block_end, start + MAX_BLOCK_CHARS)
        block = text[start:block_end]
        section_stop = re.search(
            r"\s+T\d+\.\d+\s+[A-Z]|\bWhen\s+said\s+resolution\b|\bA\s+motion\s+to\s+reconsider\b|\bto\s+rank\s+immediately\b",
            block,
            re.IGNORECASE,
        )
        if section_stop:
            block = block[: section_stop.start()]
        prefix_start = max(0, cm.start() - 300)
        page_prefix = text[prefix_start : cm.start()]
        if _block_is_termination(block, page_prefix):
            continue
        if not _looks_like_appointment(page_prefix + " " + block):
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
            member_raw = _member_raw(ev_first, ev_last.strip(), ev_state)
            if not passes_quality_gates(committee_raw, member_raw)[0]:
                return
            events.append({
                "decision_date": _parse_date(event_date) or event_date,
                "member_raw": member_raw,
                "committee_raw": committee_raw,
                "source_loc": source_loc,
                "text_span": (page_prefix + " " + block)[:500].strip(),
                "chamber": "H",
                "congress": congress,
            })

        if _ROSTER_TITLES_AFTER_DASH.search(page_prefix + " " + block):
            for first, last, state in _extract_titled_names(block):
                if last and len(last) >= 2:
                    add_event(last, state, first)
            continue

        for nm in _NAME_PATTERN_COMMA_STATE.finditer(block):
            last_name = (nm.group(1) or "").strip()
            state_phrase = (nm.group(2) or "").strip() or None
            if state_phrase and state_phrase.upper() in ("MESSRS", "MMES", "MESDAMES", "CHAIRMAN", "CHAIR", "RANKING"):
                state_phrase = None
            last_name = re.sub(r"\s*(?:Chairman|Chair|Ranking)$", "", last_name, flags=re.IGNORECASE).strip()
            if last_name and len(last_name) >= 2:
                add_event(last_name, state_phrase, None)
        for nm in _NAME_PATTERN_OF_NO_SPACE.finditer(block):
            last_name = (nm.group(1) or "").strip()
            state_phrase = (nm.group(2) or "").strip() or None
            if last_name and len(last_name) >= 2:
                add_event(last_name, state_phrase, None)

        for nm in _TITLE_NAME.finditer(block):
            name = (nm.group(1) or "").strip().rstrip(".,;")
            state = (nm.group(2) or "").strip() or None
            name = re.sub(r"\s*[,;]\s*(?:Chairman|Chair)\.?\s*$", "", name, flags=re.IGNORECASE).strip()
            if not name:
                continue
            parts = name.split()
            last_name = parts[-1] if parts else name
            first_name = parts[0] if len(parts) > 1 else None
            if last_name.endswith("of"):
                last_name = last_name[:-2].strip()
            if len(last_name) >= 2:
                add_event(last_name, state, first_name)

        messrs_match = _MESSRS_PATTERN.search(block)
        if messrs_match:
            names_text = messrs_match.group(1)
            section_end = re.search(r"\s+T\d+\.\d+\s+[A-Z]|\s+Ordered,|\s+The\s+SPEAKER\s+pro\s+tempore", names_text, re.IGNORECASE)
            if section_end:
                names_text = names_text[: section_end.start()]
            for segment in re.split(r"\s*,\s*|\s+and\s+", names_text):
                segment = segment.strip()
                if not segment:
                    continue
                tokens = segment.split()
                last_name = None
                rest_start = 0
                for i, tok in enumerate(tokens):
                    clean = tok.rstrip(".,;")
                    if not clean or len(clean) < 2 or clean.endswith("-"):
                        continue
                    if not clean.replace("-", "").replace("'", "").isalpha():
                        continue
                    if clean.upper() != clean:
                        continue
                    if clean.upper() in _MESSRS_SKIP:
                        continue
                    if clean.endswith("of"):
                        clean = clean[:-2].strip()
                    last_name = re.sub(r"(?:Chairman|Chair|Ranking)$", "", clean, flags=re.IGNORECASE).strip()
                    rest_start = i + 1
                    break
                if not last_name or len(last_name) < 2:
                    continue
                state_phrase = " ".join(tokens[rest_start:]).strip() or None
                if state_phrase:
                    state_phrase = state_phrase.rstrip(".,;")
                add_event(last_name, state_phrase, None)

    return events


# --- Whole-file parse (backward compatible) ------------------------------------

def _guess_decision_date_from_filename(file_path: Path) -> date:
    match = re.search(r"(\d{4})", file_path.name)
    if match:
        return date(int(match.group(1)), 1, 1)
    return date(2013, 1, 1)


def _congress_for_year(year: int) -> int:
    for congress, (start, end) in sorted(CONGRESS_DATES.items(), key=lambda item: item[1][0]):
        if start.year <= year < end.year:
            return congress
    return max(CONGRESS_DATES)


def parse_journal_file(file_path: Path, max_pages: int | None = None) -> list[dict]:
    """
    Parse a single House Journal PDF. Uses 3-column crop per page so regexes see one column at a time
    (avoids column bleed). Runs detect_actions_on_page on each relevant column; merges and dedupes.

    Fast-path: columns without the token "committee" are skipped before calling detect_actions_on_page,
    avoiding normalization and regex passes for obviously irrelevant text.
    """
    if pdfplumber is None:
        return []

    year_match = re.search(r"(\d{4})", file_path.name)
    year = int(year_match.group(1)) if year_match else 2013

    congress = _congress_for_year(year)

    all_events: list[dict] = []
    seen: set[tuple[str, str]] = set()
    last_page_date: str | None = None
    last_header_year: int = year

    try:
        lib = pdfplumber
        if lib is None:
            return []
        with lib.open(file_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                if max_pages is not None and page_num > max_pages:
                    break
                try:
                    full_text = page.extract_text() or ""
                    columns = _extract_page_text_by_columns(page)
                    if not columns:
                        columns = [full_text] if full_text.strip() else []
                    # House headers alternate year/date and may shift across columns.
                    header_candidates = [full_text] + columns
                    for candidate in header_candidates:
                        header_year = extract_header_year_from_page(candidate)
                        if header_year is not None:
                            last_header_year = header_year
                            break
                    printed_page = extract_printed_page_from_page_text(full_text)
                    if printed_page is None:
                        header_footer_text = columns[0] if columns else ""
                        printed_page = extract_printed_page_from_page_text(header_footer_text)
                    if printed_page is None:
                        printed_page = page_num
                    page_date = None
                    for candidate in header_candidates:
                        page_date = extract_date_from_page(candidate, default_year=last_header_year)
                        if page_date is not None:
                            break
                    if page_date is not None:
                        last_page_date = page_date
                    else:
                        page_date = last_page_date
                    for col_text in columns:
                        if not col_text.strip():
                            continue
                        if "committee" not in col_text.lower():
                            continue
                        page_events = detect_actions_on_page(
                            col_text,
                            printed_page=printed_page,
                            congress=congress,
                            default_year=year,
                            page_date_override=page_date,
                        )
                        for ev in page_events:
                            key = (
                                (ev.get("committee_raw") or "").strip().lower(),
                                (ev.get("member_raw") or "").strip().lower(),
                            )
                            if key not in seen:
                                seen.add(key)
                                all_events.append(ev)
                finally:
                    # pdfplumber caches layout and character objects per Page. Bound
                    # Journals can otherwise retain tens of GiB until the PDF closes.
                    page.close()
    except Exception:
        return []

    events = all_events

    # Normalize output: source_loc = filename when no H###; decision_date as date
    out: list[dict] = []
    for ev in events:
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
        })
    if VERBOSE:
        print(f"    [Parser] {file_path.name} extracted {len(out)} appointments")
    return out
