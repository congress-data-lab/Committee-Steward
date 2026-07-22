import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

import psycopg2.extras
from db.connection import get_connection
from core.committees.resolver import build_committee_index, resolve_from_index, CommitteeResolutionError
from core.committees.resolver import committee_name_to_id
from core.events.crec_parser import (
    has_committee_termination_signal,
    has_departure_verb_with_finality,
    narrow_death_resolution_span,
    parse_crec_terminations,
    parse_crec_terminations_from_death,
    parse_crec_terminations_from_house,
    parse_crec_terminations_from_senate,
    parse_crec_text,
    parse_crec_speaker_election,
    parse_crec_transfer,
)
from core.members.resolver import MemberResolver, MemberResolutionError
from ingest.event_ledger import compute_event_id
from ingest.event_state import has_active_appointment, has_active_removal

EXTRACTION_MODE = "record_pattern"
BATCH_SIZE = 500
APPOINTMENT_CUE_RE = re.compile(r"(?i)\b(?:re)?appoint(?:s|ed|ment|ing)?\b")
APPOINTMENT_COMMITTEE_WINDOW_RE = re.compile(
    r"(?is)\b(?:re)?appoint(?:s|ed|ment|ing)?\b.{0,240}\bcommittee\b"
    r"|\bcommittee\b.{0,240}\b(?:re)?appoint(?:s|ed|ment|ing)?\b"
)

INSERT_EVENT_SQL = """
INSERT INTO committee_event (
    event_id, congress_no, chamber, bioguide_id, committee_code,
    action, decision_date, effective_date,
    source_document_id, source_locator, text_span, extraction_mode
) VALUES %s
ON CONFLICT DO NOTHING
"""


# #region agent log
DEBUG_LOG_PATH = Path(".cursor/debug-6f994e.log")


def _debug_log(hypothesis_id: str, message: str, data: dict) -> None:
    """Lightweight NDJSON logger for debug session 6f994e."""
    try:
        entry = {
            "sessionId": "6f994e",
            "runId": "house-115-crec",
            "hypothesisId": hypothesis_id,
            "location": "ingest/load_crec_events.py",
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        # Logging must never break ingest.
        pass
# #endregion agent log


def _normalize_effective_date(decision_date: str, effective_date: str | None) -> str:
    """
    Enforce schema invariant: effective_date >= decision_date.
    Inputs are expected in YYYY-MM-DD form.
    """
    d = str(decision_date or "")[:10]
    e = str(effective_date or "")[:10]
    if not e:
        return d
    return e if e >= d else d


def _is_expected_out_of_scope_body(name: str) -> bool:
    """
    Return True for committee-like bodies intentionally excluded from tracking.
    """
    normalized = re.sub(r"\s+", " ", name).strip().lower()
    if not normalized:
        return False
    if normalized.startswith("joint committee"):
        return True
    if normalized.startswith("select committee") or normalized.startswith("special committee"):
        return True
    if normalized.endswith("policy committee") and not normalized.startswith("committee on "):
        return True
    if normalized.startswith("commission") or normalized.startswith("board"):
        return True
    return any(
        token in normalized
        for token in (" commission", " board", " council", " task force", " advisory ")
    )


def _looks_like_committee_appointment_text(text: str) -> bool:
    """
    Fast prefilter for appointment parsing.
    Keeps committee-appointment contexts while skipping generic "appoint" noise.
    """
    if not APPOINTMENT_CUE_RE.search(text):
        return False
    return bool(APPOINTMENT_COMMITTEE_WINDOW_RE.search(text))


# Formal death/passing resolution (S.Res., H.Res., or "A resolution relative to the death/passing").
# Used so we do not skip procedural death resolutions when they appear in items tagged as speech.
DEATH_RESOLUTION_CITATION_RE = re.compile(
    r"(?i)(?:s\.\s*res\.\s*\d+|h\.\s*res\.\s*\d+"
    r"|a\s+resolution\s+relative\s+to\s+the\s+(?:death|passing))"
)


# CREC: "gentleman from New York (Mr. King)" / "gentlewoman from Ohio (Ms. X)" / "the State of Mississippi, Mr. Nunnelee"
CREC_STATE_FROM_GENTLEMAN_RE = re.compile(
    r"(?i)(?:gentleman|gentlewoman|gentlelady)\s+from\s+(?:the\s+State\s+of\s+)?([A-Za-z\s]+?)\s*[,\(]"
)
CREC_STATE_PASSING_OF_RE = re.compile(
    r"(?i)(?:death|passing)\s+of\s+(?:the\s+)?(?:Honorable\s+)?(?:Representative|Senator)?\s*.+?\s+from\s+(?:the\s+State\s+of\s+)?([A-Za-z\s]+?)(?:\s*[,\(]|\s+of\s+)"
)
# Senate resignation: "resignation of Senator Jeff Sessions of Alabama" / "former Senator Thad Cochran of Mississippi"
CREC_STATE_SENATOR_OF_RE = re.compile(
    r"(?i)(?:resignation\s+of\s+(?:United\s+States\s+)?(?:former\s+)?Senator\s+[^,.]+\s+of\s+"
    r"|(?:former\s+)?Senator\s+[^,.]+\s+of\s+)([A-Za-z][A-Za-z\s]+?)(?=[,.]|\s+and\s+of|\s+is\s+filled|$)"
)


def _extract_state_from_crec_text(text: str, member_raw: str | None = None) -> str | None:
    """Extract state from CREC for disambiguation. Uses 'gentleman from X' or 'Mr. Name, State' list format."""
    if not text or not text.strip():
        return None
    normalized = re.sub(r"\s+", " ", text).strip()
    m = CREC_STATE_FROM_GENTLEMAN_RE.search(normalized)
    if m:
        return m.group(1).strip()
    m = CREC_STATE_PASSING_OF_RE.search(normalized)
    if m:
        return m.group(1).strip()
    m = CREC_STATE_SENATOR_OF_RE.search(normalized)
    if m:
        return m.group(1).strip()
    # List format: "Mr. King, New York" / "Ms. Ros-Lehtinen, Florida"
    if member_raw and member_raw.strip():
        name_part = member_raw.strip()
        # Escape for regex; match "Mr. King, New York" or "King, New York"
        pattern = re.escape(name_part) + r",\s*([A-Za-z\s]+?)(?:\s*\n|$|,)"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        # Try last word of name (e.g. "King" from "Mr. King")
        last_word = name_part.split()[-1].strip(".,")
        if last_word:
            pattern = re.escape(last_word) + r",\s*([A-Za-z\s]+?)(?:\s*\n|$|,)"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
    return None


def _looks_like_formal_death_resolution(lower_normalized: str) -> bool:
    """
    True if text looks like a formal death/passing resolution (e.g. S.Res. 160, H.Res. N, or
    "A resolution relative to the death of ..."). Such items must be processed even when
    kind=speech and speaker is a named member, so we do not skip Senate/House death REMOVED events.
    """
    return bool(DEATH_RESOLUTION_CITATION_RE.search(lower_normalized))


def _resolve_committee_code(
    committee_name: str,
    *,
    congress: int,
    committee_idx: dict,
    chamber_hint: str,
    chamber_char: str,
) -> str:
    """
    Resolve committee code with YAML index first, then hardcoded parser fallback.
    This keeps source-first matching while recovering common CREC/Journal-style variants.
    """
    try:
        return resolve_from_index(committee_name, congress, committee_idx, chamber_hint)
    except CommitteeResolutionError:
        bill_type = "hres" if chamber_char == "H" else "sres"
        fallback = committee_name_to_id(committee_name, bill_type=bill_type)
        if fallback:
            return fallback
        raise


def _event_effective_date_needs_refresh(
    conn, event_id: str, effective_date: str
) -> bool:
    """Return True when this source event exists with an obsolete effective date."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT effective_date FROM committee_event WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
    if not row:
        return False
    current = row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])[:10]
    return current != str(effective_date)[:10]


def _flush_event_batch(conn, batch: list) -> tuple[int, int]:
    """Insert batch of events; return (appointed_inserted, terminations_inserted)."""
    if not batch:
        return 0, 0
    appt = [t for t in batch if t[5] == "APPOINTED"]  # action is index 5
    raw_term = [t for t in batch if t[5] == "REMOVED"]
    term: list[tuple] = []
    term_repairs: list[tuple] = []
    for t in raw_term:
        # Guard against repeated REMOVED transitions when the latest prior state
        # is already removed at this effective date. Preserve a same-source event
        # when reparsing found a corrected effective date so the upsert can repair it.
        active_removal = has_active_removal(
            conn, t[1], t[2], t[3], t[4], t[7]
        )
        needs_refresh = active_removal and _event_effective_date_needs_refresh(
            conn, t[0], t[7]
        )
        if active_removal and not needs_refresh:
            continue
        term.append(t)
        if needs_refresh:
            term_repairs.append(t)
    appt_inserted, term_inserted = 0, 0
    with conn.cursor() as cur:
        if appt:
            vals = [(t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7], t[8], t[9], t[10], t[11]) for t in appt]
            psycopg2.extras.execute_values(cur, INSERT_EVENT_SQL, vals, template=None, page_size=len(vals))
            appt_inserted = cur.rowcount
        if term:
            vals = [(t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7], t[8], t[9], t[10], t[11]) for t in term]
            psycopg2.extras.execute_values(cur, INSERT_EVENT_SQL, vals, template=None, page_size=len(vals))
            term_inserted = cur.rowcount
            for t in term_repairs:
                cur.execute(
                    """
                    UPDATE committee_event
                    SET effective_date = %s
                    WHERE event_id = %s
                      AND effective_date IS DISTINCT FROM %s
                    RETURNING event_id
                    """,
                    (t[7], t[0], t[7]),
                )
                if cur.fetchone():
                    term_inserted += 1
    return appt_inserted, term_inserted


def _year_to_congress(year: int) -> int:
    """Map year to congress number. Fixed order: check newest first."""
    if year >= 2025:
        return 119
    if year >= 2023:
        return 118
    if year >= 2021:
        return 117
    if year >= 2019:
        return 116
    if year >= 2017:
        return 115
    if year >= 2015:
        return 114
    if year >= 2013:
        return 113
    return 113


def _congress_to_years(congress: int) -> list[int]:
    """Map congress number to calendar years (e.g. 113 -> [2013, 2014])."""
    # Congress N: 1st=1789, 2nd=1791, ... 113th=2013 (1789 + (N-1)*2)
    start_year = 1789 + (congress - 1) * 2
    return [start_year, start_year + 1]


def _get_or_create_source_document(conn, source_id: int, fpath: str, doc_date: str, raw_content: str) -> int:
    """Get or create source_document; return source_document_id.
    Citation only (document + page in external_id); full text is not stored.
    """
    content_hash = hashlib.sha256(raw_content.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_document_id FROM source_document WHERE content_hash = %s",
            (content_hash,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """
            INSERT INTO source_document (source_id, external_id, doc_date, content_hash)
            VALUES (%s, %s, %s, %s)
            RETURNING source_document_id
            """,
            (source_id, Path(fpath).name, doc_date, content_hash),
        )
        return cur.fetchone()[0]


def _get_member_committees_on_date(
    conn, bioguide_id: str, as_of_date: str, valid_codes: set,
    congress_no: int, chamber: str,
) -> list:
    """
    Return committee_code list for committees the member was on as of as_of_date
    for the given congress and chamber. Uses committee_event: member is on committee
    if last event before/on date is APPOINTED.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
                SELECT committee_code, action,
                    ROW_NUMBER() OVER (
                        PARTITION BY committee_code
                        ORDER BY effective_date DESC, event_id DESC
                    ) AS rn
                FROM committee_event
                WHERE bioguide_id = %s
                  AND congress_no = %s
                  AND chamber = %s
                  AND effective_date <= %s::date
                  AND action IN ('APPOINTED', 'REMOVED')
            )
            SELECT committee_code FROM ranked
            WHERE rn = 1 AND action = 'APPOINTED'
            """,
            (bioguide_id, congress_no, chamber, as_of_date),
        )
        rows = cur.fetchall()
    return [r[0] for r in rows if r[0] in valid_codes]


def _get_existing_source_removal_committees(
    conn,
    source_document_id: int,
    source_locator: str,
    bioguide_id: str,
    congress_no: int,
    chamber: str,
    valid_codes: set,
) -> list[str]:
    """Recover same-source committees so a replay can refresh an existing removal."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT committee_code
            FROM committee_event
            WHERE source_document_id = %s
              AND source_locator = %s
              AND bioguide_id = %s
              AND congress_no = %s
              AND chamber = %s
              AND action = 'REMOVED'
            """,
            (
                source_document_id,
                source_locator,
                bioguide_id,
                congress_no,
                chamber,
            ),
        )
        rows = cur.fetchall()
    return [row[0] for row in rows if row[0] in valid_codes]


def _neighbor_context_text(content_items: list, idx: int, radius: int = 1) -> tuple[str, int, int]:
    """
    Build a small text window around content[idx] to handle header/body split layouts.
    Returns (joined_text, start_idx, end_idx).
    """
    start_idx = max(0, idx - radius)
    end_idx = min(len(content_items) - 1, idx + radius)
    parts: list[str] = []
    for j in range(start_idx, end_idx + 1):
        item = content_items[j]
        text = item.get("text", "")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts), start_idx, end_idx


def _resolve_document_chamber(fname: str, data: dict) -> tuple[str, str]:
    """
    Resolve document chamber from JSON header when present; fallback to filename.
    Returns (chamber_hint, chamber_char): ("house"/"senate", "H"/"S").
    """
    header = data.get("header", {})
    header_chamber = header.get("chamber") if isinstance(header, dict) else None
    if isinstance(header_chamber, str):
        normalized = header_chamber.strip().lower()
        if normalized.startswith("house"):
            return "house", "H"
        if normalized.startswith("senate"):
            return "senate", "S"
    if "PgS" in fname:
        return "senate", "S"
    return "house", "H"


def load_crec_events(
    verbose: bool = False,
    congress_filter: int | None = None,
    file_limit: int | None = None,
    chamber_filter: str = "both",
    file_name_filter: str | None = None,
):
    """
    Ingests committee events from Congressional Record JSON files.
    If congress_filter is set (e.g. 113), only processes files from that congress's years.
    If file_limit is set, stop after processing that many JSON files (for testing).
    If chamber_filter is set to 'house' or 'senate', only process that chamber.
    If file_name_filter is set, only process that exact JSON basename.
    """
    project_root = Path(__file__).resolve().parent.parent
    data_dir = project_root / "data" / "crec"
    allowed_years = _congress_to_years(congress_filter) if congress_filter else None
    
    # 1. Setup Resolvers
    conn = get_connection()
    member_resolver = MemberResolver(conn)
    
    yaml_paths = [
        project_root / "data" / "reference" / "committees-current.yaml",
        project_root / "data" / "reference" / "committees-historical.yaml",
    ]
    committee_idx = build_committee_index(yaml_paths)
    
    extracted_count = 0
    resolved_count = 0
    terminations_extracted = 0
    terminations_inserted = 0
    appointments_attempted = 0
    terminations_attempted = 0
    
    # Pre-fetch valid committee codes so we only process tracked committees
    valid_committee_codes = set()
    with conn.cursor() as cur:
        cur.execute("SELECT committee_code FROM committee")
        for row in cur.fetchall():
            valid_committee_codes.add(row[0])

    # Get or create CREC source
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id FROM source WHERE source_type = 'CR' AND source_name = 'Congressional Record JSON' LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            crec_source_id = row[0]
        else:
            cur.execute(
                """
                INSERT INTO source (source_type, source_name, version_tag)
                VALUES ('CR', 'Congressional Record JSON', 'crec_json')
                RETURNING source_id
                """
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("Failed to create CREC source: INSERT did not return source_id")
            crec_source_id = row[0]

    event_batch: list[tuple] = []
    files_processed = 0

    if congress_filter:
        print(f"Congress filter: {congress_filter} (years {allowed_years})")
    if chamber_filter != "both":
        print(f"Chamber filter: {chamber_filter}")
    if file_limit:
        print(f"File limit: {file_limit}")
    if file_name_filter:
        file_name_filter = Path(file_name_filter).name
        print(f"File filter: {file_name_filter}")

    # 2. Walk directory for JSONs (sorted for deterministic replay)
    for root, dirs, files in sorted(os.walk(str(data_dir))):
        if file_limit and files_processed >= file_limit:
            break
        if allowed_years is not None and root == str(data_dir):
            dirs[:] = [d for d in dirs if d.isdigit() and int(d) in allowed_years]
        for fname in sorted(files):
            if file_limit and files_processed >= file_limit:
                break
            if not fname.endswith(".json"):
                continue
            if file_name_filter and fname != file_name_filter:
                continue
                
            fpath = os.path.join(root, fname)
            files_processed += 1
            
            # Extract date
            date_match = re.search(r'CREC-(\d{4}-\d{2}-\d{2})', fname)
            if not date_match:
                continue
            event_date = date_match.group(1)
            
            year = int(event_date[:4])
            if allowed_years is not None and year not in allowed_years:
                continue
            congress = _year_to_congress(year)

            with open(fpath, "r") as f:
                raw_content = f.read()
            try:
                data = json.loads(raw_content)
            except Exception:
                continue

            chamber_hint, chamber_char = _resolve_document_chamber(fname, data)
            if chamber_filter != "both" and chamber_hint != chamber_filter:
                continue

            source_document_id = _get_or_create_source_document(
                conn, crec_source_id, fpath, event_date, raw_content
            )
            content_items = data.get("content", [])
            seen_committee_departures: set[tuple[str, str, str]] = set()

            for content_idx, item in enumerate(content_items):
                text = item.get("text", "")
                if not isinstance(text, str):
                    continue

                lower_text = text.lower()
                # Normalize whitespace for trigger checks (CREC often has newlines mid-phrase)
                lower_normalized = re.sub(r"\s+", " ", lower_text)
                kind = str(item.get("kind", "")).strip().lower()
                speaker = str(item.get("speaker", "")).strip()

                # Terminations: death of member (GPO 81-84, 115-119); same flow as resignation-from-House
                if "death of" in lower_normalized or "passing of" in lower_normalized or "demise of" in lower_normalized:
                    # Member floor tributes frequently include "passing of ..." but are not procedural events.
                    # Do not skip when text is a formal death resolution (S.Res./H.Res. or "A resolution relative to...").
                    is_formal_resolution = _looks_like_formal_death_resolution(lower_normalized)
                    if kind == "speech" and not speaker.lower().startswith("the ") and not is_formal_resolution:
                        continue
                    from_death = parse_crec_terminations_from_death(text, event_date)
                    if from_death:
                        # Skip memorials for former members only in House record; in Senate record allow "former Senator" (e.g. death resolution).
                        if "former representative" in lower_normalized or (
                            chamber_char == "H" and "former senator" in lower_normalized
                        ):
                            if verbose:
                                print(f"  [Skip] Former-member death notice in {fname}")
                            continue
                        # House-only feeds often include memorial notices for Senators; skip these in House runs.
                        if chamber_char == "H" and (
                            "late a senator" in lower_normalized
                            or "a senator from the state of" in lower_normalized
                            or "senator from the state of" in lower_normalized
                        ):
                            if verbose:
                                print(f"  [Skip] Senator death notice in House record ({fname})")
                            continue
                        raw_member = from_death["member"]
                        eff_date = _normalize_effective_date(event_date, from_death["effective_date"])
                        crec_state = _extract_state_from_crec_text(text)
                        try:
                            # event_date=None so departed members (e.g. just died) are still in candidate set.
                            bioguide_id = member_resolver.resolve(
                                raw_member, congress, chamber_char,
                                state=crec_state, event_date=None,
                            )
                        except MemberResolutionError:
                            if verbose:
                                print(f"  [Skip] Could not resolve member '{raw_member}' (death) in {fname}")
                            continue
                        # Cascading removals: member died → REMOVED for each committee they were on.
                        committees = _get_member_committees_on_date(
                            conn, bioguide_id, event_date, valid_committee_codes, congress, chamber_char
                        )
                        if not committees and verbose:
                            print(f"  [Debug] No committee assignments for {raw_member} on {event_date} (death); no REMOVED rows from this item in {fname}")
                        source_loc = f"{Path(fpath).name}#content[{content_idx}]"
                        text_span = narrow_death_resolution_span(text)
                        for comm_code in committees:
                            event_id = compute_event_id(
                                congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, source_document_id, source_loc,
                            )
                            terminations_attempted += 1
                            event_batch.append((
                                event_id, congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, eff_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                            ))
                            if len(event_batch) >= BATCH_SIZE:
                                a, t = _flush_event_batch(conn, event_batch)
                                resolved_count += a
                                terminations_inserted += t
                                event_batch.clear()
                                if verbose and (a or t):
                                    print(f"  [Progress] Inserted {a} appointments, {t} terminations (total: {resolved_count} / {terminations_inserted})")
                        terminations_extracted += 1
                    continue

                # Terminations: "resignation from the House of Representatives" (member leaves Congress)
                if "resignation from the house of representatives" in lower_normalized:
                    from_house = parse_crec_terminations_from_house(text, event_date)
                    if from_house:
                        raw_member = from_house["member"]
                        eff_date = _normalize_effective_date(event_date, from_house["effective_date"])
                        crec_state = _extract_state_from_crec_text(text)
                        try:
                            # event_date=None so resigned members (valid_daterange ended) are still in candidate set.
                            bioguide_id = member_resolver.resolve(
                                raw_member, congress, chamber_char,
                                state=crec_state, event_date=None,
                            )
                            _debug_log(
                                "H1-full-exit",
                                "Resolved full-House resignation",
                                {
                                    "member": raw_member,
                                    "bioguide_id": bioguide_id,
                                    "event_date": event_date,
                                    "effective_date": eff_date,
                                    "fname": fname,
                                },
                            )
                        except MemberResolutionError:
                            if verbose:
                                print(f"  [Skip] Could not resolve member '{raw_member}' (resignation from House) in {fname}")
                            _debug_log(
                                "H1-full-exit",
                                "Failed to resolve full-House resignation",
                                {
                                    "member": raw_member,
                                    "event_date": event_date,
                                    "effective_date": eff_date,
                                    "fname": fname,
                                },
                            )
                            continue
                        source_loc = f"{Path(fpath).name}#content[{content_idx}]"
                        # Cascading full-House exits normally derive affected committees from
                        # current state. On replay, the old removal may already have closed that
                        # state, so also recover committees previously emitted by this exact source.
                        committees = set(_get_member_committees_on_date(
                            conn, bioguide_id, event_date, valid_committee_codes, congress, chamber_char
                        ))
                        committees.update(_get_existing_source_removal_committees(
                            conn,
                            source_document_id,
                            source_loc,
                            bioguide_id,
                            congress,
                            chamber_char,
                            valid_committee_codes,
                        ))
                        _debug_log(
                            "H1-full-exit",
                            "Computed committees for full-House resignation",
                            {
                                "member": raw_member,
                                "bioguide_id": bioguide_id,
                                "event_date": event_date,
                                "effective_date": eff_date,
                                "fname": fname,
                                "committees": sorted(committees),
                            },
                        )
                        if not committees and verbose:
                            print(f"  [Debug] No committee assignments for {raw_member} on {event_date} (full-House exit); no REMOVED rows from this item in {fname}")
                        text_span = text[:5000] if text else ""
                        for comm_code in sorted(committees):
                            event_id = compute_event_id(
                                congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, source_document_id, source_loc,
                            )
                            terminations_attempted += 1
                            event_batch.append((
                                event_id, congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, eff_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                            ))
                            if len(event_batch) >= BATCH_SIZE:
                                a, t = _flush_event_batch(conn, event_batch)
                                resolved_count += a
                                terminations_inserted += t
                                event_batch.clear()
                                if verbose and (a or t):
                                    print(f"  [Progress] Inserted {a} appointments, {t} terminations (total: {resolved_count} / {terminations_inserted})")
                        terminations_extracted += 1
                    continue

                # Terminations: Senator chamber exit (resignation or death). Unified parser handles both.
                # Gate: vacancy+resignation, letters of resignation, or vacancy+death (e.g. "vacancy caused by the death of Senator X").
                if chamber_char == "S" and (
                    ("vacancy" in lower_normalized and "resignation of" in lower_normalized)
                    or ("letters of resignation from" in lower_normalized and "senator" in lower_normalized)
                    or ("vacancy" in lower_normalized and "death of" in lower_normalized and "senator" in lower_normalized)
                ):
                    from_senate = parse_crec_terminations_from_senate(text, event_date)
                    if from_senate:
                        raw_member = from_senate["member"]
                        eff_date = _normalize_effective_date(event_date, from_senate["effective_date"])
                        crec_state = _extract_state_from_crec_text(text)
                        try:
                            # Use event_date=None so members who already left (valid_daterange ended) are still in candidate set.
                            bioguide_id = member_resolver.resolve(
                                raw_member, congress, chamber_char,
                                state=crec_state, event_date=None,
                            )
                        except MemberResolutionError:
                            if verbose:
                                print(f"  [Skip] Could not resolve member '{raw_member}' (resignation from Senate) in {fname}")
                            continue
                        committees = _get_member_committees_on_date(
                            conn, bioguide_id, event_date, valid_committee_codes, congress, chamber_char
                        )
                        if not committees and verbose:
                            print(f"  [Debug] No committee assignments for {raw_member} on {event_date} (Senate exit); no REMOVED rows from this item in {fname}")
                        source_loc = f"{Path(fpath).name}#content[{content_idx}]"
                        text_span = text[:5000] if text else ""
                        for comm_code in committees:
                            event_id = compute_event_id(
                                congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, source_document_id, source_loc,
                            )
                            terminations_attempted += 1
                            event_batch.append((
                                event_id, congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, eff_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                            ))
                            if len(event_batch) >= BATCH_SIZE:
                                a, t = _flush_event_batch(conn, event_batch)
                                resolved_count += a
                                terminations_inserted += t
                                event_batch.clear()
                                if verbose and (a or t):
                                    print(f"  [Progress] Inserted {a} appointments, {t} terminations (total: {resolved_count} / {terminations_inserted})")
                        terminations_extracted += 1
                    continue

                # Terminations: member elected Speaker (leave all committees).
                if "elected speaker" in lower_normalized or "duly elected speaker" in lower_normalized:
                    speaker_result = parse_crec_speaker_election(text, event_date)
                    if speaker_result:
                        raw_member = speaker_result["member"]
                        eff_date = _normalize_effective_date(event_date, speaker_result["effective_date"])
                        crec_state = _extract_state_from_crec_text(text)
                        try:
                            bioguide_id = member_resolver.resolve(
                                raw_member, congress, chamber_char,
                                state=crec_state, event_date=event_date,
                            )
                        except MemberResolutionError:
                            if verbose:
                                print(f"  [Skip] Could not resolve member '{raw_member}' (Speaker election) in {fname}")
                            continue
                        committees = _get_member_committees_on_date(
                            conn, bioguide_id, event_date, valid_committee_codes, congress, chamber_char
                        )
                        if not committees and verbose:
                            print(f"  [Debug] No committee assignments for {raw_member} on {event_date} (Speaker election); no REMOVED rows in {fname}")
                        source_loc = f"{Path(fpath).name}#content[{content_idx}]"
                        text_span = text[:5000] if text else ""
                        for comm_code in committees:
                            event_id = compute_event_id(
                                congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, source_document_id, source_loc,
                            )
                            terminations_attempted += 1
                            event_batch.append((
                                event_id, congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, eff_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                            ))
                            if len(event_batch) >= BATCH_SIZE:
                                a, t = _flush_event_batch(conn, event_batch)
                                resolved_count += a
                                terminations_inserted += t
                                event_batch.clear()
                                if verbose and (a or t):
                                    print(f"  [Progress] Inserted {a} appointments, {t} terminations (total: {resolved_count} / {terminations_inserted})")
                        terminations_extracted += 1
                    continue

                # Terminations: transfer (relieved from X and appointed to Y, or transferred to Y).
                if "transferred to" in lower_normalized or ("relieved from" in lower_normalized and "appointed to" in lower_normalized):
                    transfer_result = parse_crec_transfer(text, event_date)
                    if transfer_result:
                        raw_member = transfer_result["member"]
                        eff_date = _normalize_effective_date(event_date, transfer_result["effective_date"])
                        crec_state = _extract_state_from_crec_text(text)
                        try:
                            bioguide_id = member_resolver.resolve(
                                raw_member, congress, chamber_char,
                                state=crec_state, event_date=event_date,
                            )
                        except MemberResolutionError:
                            if verbose:
                                print(f"  [Skip] Could not resolve member '{raw_member}' (transfer) in {fname}")
                            continue
                        to_committee_raw = transfer_result.get("to_committee")
                        from_committee_raw = transfer_result.get("from_committee")
                        try:
                            to_code = (
                                _resolve_committee_code(
                                    to_committee_raw,
                                    congress=congress,
                                    committee_idx=committee_idx,
                                    chamber_hint=chamber_hint,
                                    chamber_char=chamber_char,
                                )
                                if to_committee_raw
                                else None
                            )
                        except CommitteeResolutionError:
                            if verbose:
                                print(f"  [Skip] Could not resolve committee '{to_committee_raw}' (transfer to) in {fname}")
                            continue
                        from_code = None
                        if from_committee_raw:
                            try:
                                from_code = _resolve_committee_code(
                                    from_committee_raw,
                                    congress=congress,
                                    committee_idx=committee_idx,
                                    chamber_hint=chamber_hint,
                                    chamber_char=chamber_char,
                                )
                            except CommitteeResolutionError:
                                pass
                        if from_code:
                            committees_to_remove = [from_code] if from_code in valid_committee_codes else []
                        else:
                            member_committees = _get_member_committees_on_date(
                                conn, bioguide_id, event_date, valid_committee_codes, congress, chamber_char
                            )
                            committees_to_remove = [c for c in member_committees if c != to_code]
                        source_loc = f"{Path(fpath).name}#content[{content_idx}]"
                        text_span = text[:5000] if text else ""
                        for comm_code in committees_to_remove:
                            if comm_code not in valid_committee_codes:
                                continue
                            event_id = compute_event_id(
                                congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, source_document_id, source_loc,
                            )
                            terminations_attempted += 1
                            event_batch.append((
                                event_id, congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, eff_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                            ))
                            if len(event_batch) >= BATCH_SIZE:
                                a, t = _flush_event_batch(conn, event_batch)
                                resolved_count += a
                                terminations_inserted += t
                                event_batch.clear()
                                if verbose and (a or t):
                                    print(f"  [Progress] Inserted {a} appointments, {t} terminations (total: {resolved_count} / {terminations_inserted})")
                        if committees_to_remove:
                            terminations_extracted += 1
                    continue

                # Committee-level departures (resignation/leave/step-down family).
                has_committee_departure = has_committee_termination_signal(text)
                has_split_candidate = has_departure_verb_with_finality(text)
                if has_committee_departure or has_split_candidate:
                    terminations = parse_crec_terminations(text, event_date)
                    parse_text = text
                    source_start = content_idx
                    source_end = content_idx
                    # CREC resignation frames are often split across adjacent items
                    # (header committee list + letter body + acceptance line).
                    ctx_text, ctx_start, ctx_end = _neighbor_context_text(content_items, content_idx, radius=2)
                    ctx_terminations = []
                    if (ctx_start != content_idx or ctx_end != content_idx) and (
                        has_committee_termination_signal(ctx_text) or has_departure_verb_with_finality(ctx_text)
                    ):
                        ctx_terminations = parse_crec_terminations(ctx_text, event_date)
                    if ctx_terminations:
                        merged: list[dict] = []
                        seen_keys: set[tuple[str, str, str]] = set()
                        for term in ctx_terminations + terminations:
                            key = (
                                term["member"].lower(),
                                term["committee"].lower(),
                                term["effective_date"],
                            )
                            if key in seen_keys:
                                continue
                            seen_keys.add(key)
                            merged.append(term)
                        terminations = merged
                        parse_text = ctx_text
                        source_start = ctx_start
                        source_end = ctx_end
                    if terminations:
                        _debug_log(
                            "H2-committee-exit",
                            "Parsed committee-level terminations",
                            {
                                "fname": fname,
                                "event_date": event_date,
                                "count": len(terminations),
                                "sample": terminations[:4],
                            },
                        )
                        unresolved_terminations = [
                            t
                            for t in terminations
                            if (t["member"].lower(), t["committee"].lower(), t["effective_date"])
                            not in seen_committee_departures
                        ]
                        if not unresolved_terminations:
                            continue
                        if verbose:
                            print(
                                f"  [Debug] Committee departure in {fname}: "
                                f"{len(unresolved_terminations)} terminations"
                            )
                        for t in unresolved_terminations:
                            comm_name = t["committee"]
                            raw_member = t["member"]
                            eff_date = _normalize_effective_date(event_date, t["effective_date"])
                            dedupe_key = (raw_member.lower(), comm_name.lower(), eff_date)
                            if dedupe_key in seen_committee_departures:
                                continue
                            seen_committee_departures.add(dedupe_key)
                            terminations_extracted += 1
                            try:
                                comm_code = _resolve_committee_code(
                                    comm_name,
                                    congress=congress,
                                    committee_idx=committee_idx,
                                    chamber_hint=chamber_hint,
                                    chamber_char=chamber_char,
                                )
                                if comm_code not in valid_committee_codes:
                                    if verbose:
                                        print(f"  [Skip] Committee '{comm_name}' not in valid set ({fname})")
                                    continue
                            except CommitteeResolutionError:
                                if verbose:
                                    print(f"  [Skip] Could not resolve committee '{comm_name}' ({fname})")
                                continue
                            # Neighbor items help recover split resignation frames, but can
                            # mention unrelated members and states. Resolve the signer from
                            # the narrow item so adjacent committee business cannot override
                            # an otherwise unambiguous full name.
                            crec_state = _extract_state_from_crec_text(text)
                            try:
                                bioguide_id = member_resolver.resolve(
                                    raw_member, congress, chamber_char,
                                    state=crec_state, event_date=event_date,
                                )
                            except MemberResolutionError:
                                if verbose:
                                    print(f"  [Skip] Could not resolve member '{raw_member}' for {comm_name} ({fname})")
                                continue
                            if source_start == source_end:
                                source_loc = f"{Path(fpath).name}#content[{source_start}]"
                            else:
                                source_loc = f"{Path(fpath).name}#content[{source_start}..{source_end}]"
                            text_span = parse_text[:5000] if parse_text else ""
                            event_id = compute_event_id(
                                congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, source_document_id, source_loc,
                            )
                            terminations_attempted += 1
                            event_batch.append((
                                event_id, congress, chamber_char, bioguide_id, comm_code,
                                "REMOVED", event_date, eff_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                            ))
                            if len(event_batch) >= BATCH_SIZE:
                                a, t = _flush_event_batch(conn, event_batch)
                                resolved_count += a
                                terminations_inserted += t
                                event_batch.clear()
                                if verbose and (a or t):
                                    print(f"  [Progress] Inserted {a} appointments, {t} terminations (total: {resolved_count} / {terminations_inserted})")
                        continue

                # Appointments
                # Use a broad appointment cue so we do not miss valid forms like
                # "appointed Mr. X to the Committee on Y" that omit "the following".
                if not _looks_like_committee_appointment_text(text):
                    continue

                appointments = parse_crec_text(text)
                for appt in appointments:
                    comm_name = appt['committee']
                    raw_members = appt['members']
                    
                    try:
                        comm_code = _resolve_committee_code(
                            comm_name,
                            congress=congress,
                            committee_idx=committee_idx,
                            chamber_hint=chamber_hint,
                            chamber_char=chamber_char,
                        )
                        if comm_code not in valid_committee_codes:
                            continue
                    except CommitteeResolutionError:
                        # Suppress expected out-of-scope bodies (commissions, joint/select panels, etc.)
                        if _is_expected_out_of_scope_body(comm_name):
                            if verbose:
                                print(f"  [Skip] Out-of-scope body '{comm_name}' in {fname}")
                            continue
                        if "committee" in comm_name.lower():
                            print(f"  [Warning] Could not resolve committee '{comm_name}' in {fname}")
                        continue

                    # GPO 1524: "vice [departed member]" — emit REMOVED for replaced member
                    replaced_member = appt.get("replaced_member")
                    if replaced_member:
                        crec_state = _extract_state_from_crec_text(text)
                        try:
                            replaced_bioguide = member_resolver.resolve(
                                replaced_member, congress, chamber_char,
                                state=crec_state, event_date=event_date,
                            )
                        except MemberResolutionError:
                            replaced_bioguide = None
                        if replaced_bioguide:
                            source_loc = f"{Path(fpath).name}#content[{content_idx}]"
                            text_span = text[:5000] if text else ""
                            event_id = compute_event_id(
                                congress, chamber_char, replaced_bioguide, comm_code,
                                "REMOVED", event_date, source_document_id, source_loc,
                            )
                            terminations_attempted += 1
                            event_batch.append((
                                event_id, congress, chamber_char, replaced_bioguide, comm_code,
                                "REMOVED", event_date, event_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                            ))
                            if len(event_batch) >= BATCH_SIZE:
                                a, t = _flush_event_batch(conn, event_batch)
                                resolved_count += a
                                terminations_inserted += t
                                event_batch.clear()
                                if verbose and (a or t):
                                    print(f"  [Progress] Inserted {a} appointments, {t} terminations (total: {resolved_count} / {terminations_inserted})")
                            terminations_extracted += 1
                        
                    for raw_m in raw_members:
                        # Skip things that are clearly not names
                        if len(raw_m) <= 2 or " of " not in raw_m and "." not in raw_m and " " not in raw_m:
                            continue
                            
                        extracted_count += 1

                        crec_state = _extract_state_from_crec_text(text, member_raw=raw_m)
                        try:
                            bioguide_id = member_resolver.resolve(
                                raw_m, congress, chamber_char,
                                state=crec_state, event_date=event_date,
                            )
                        except MemberResolutionError as e2:
                            print(f"  [Warning] Could not resolve member '{raw_m}' in '{comm_name}' ({fname}): {e2}")
                            continue

                        source_loc = f"{Path(fpath).name}#content[{content_idx}]"
                        text_span = text[:5000] if text else ""
                        if has_active_appointment(
                            conn, congress, chamber_char, bioguide_id, comm_code, event_date
                        ):
                            continue
                        event_id = compute_event_id(
                            congress, chamber_char, bioguide_id, comm_code,
                            "APPOINTED", event_date, source_document_id, source_loc,
                        )
                        appointments_attempted += 1
                        event_batch.append((
                            event_id, congress, chamber_char, bioguide_id, comm_code,
                            "APPOINTED", event_date, event_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                        ))
                        if len(event_batch) >= BATCH_SIZE:
                            a, t = _flush_event_batch(conn, event_batch)
                            resolved_count += a
                            terminations_inserted += t
                            event_batch.clear()
                            if verbose and (a or t):
                                print(f"  [Progress] Inserted {a} appointments, {t} terminations (total: {resolved_count} / {terminations_inserted})")
                            
    # Final flush
    if event_batch:
        a, t = _flush_event_batch(conn, event_batch)
        resolved_count += a
        terminations_inserted += t
    conn.commit()
    conn.close()
    
    print("\nCREC INGESTION COMPLETE")
    print(f"Extracted Appointees (from recognized committees): {extracted_count}")
    print(f"Appointments attempted (resolved): {appointments_attempted}")
    print(f"Appointments inserted: {resolved_count}")
    print(f"Appointments skipped as duplicates/conflicts: {max(0, appointments_attempted - resolved_count)}")
    print(f"Terminations extracted: {terminations_extracted}")
    print(f"Terminations attempted (resolved): {terminations_attempted}")
    print(f"Terminations inserted: {terminations_inserted}")
    print(f"Terminations skipped as duplicates/conflicts: {max(0, terminations_attempted - terminations_inserted)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest committee events from CREC JSON files")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log skipped events (committee/member resolution)")
    parser.add_argument("-c", "--congress", type=int, default=None, help="Only process this congress (e.g. 113 for 2013-2014)")
    parser.add_argument("-n", "--limit", type=int, default=None, help="Stop after N files (for testing)")
    parser.add_argument(
        "--file",
        dest="file_name_filter",
        help="Only process this exact CREC JSON basename",
    )
    parser.add_argument(
        "--chamber",
        choices=["house", "senate", "both"],
        default="both",
        help="Restrict processing to one chamber (default: both)",
    )
    args = parser.parse_args()
    load_crec_events(
        verbose=args.verbose,
        congress_filter=args.congress,
        file_limit=args.limit,
        chamber_filter=args.chamber,
        file_name_filter=args.file_name_filter,
    )
