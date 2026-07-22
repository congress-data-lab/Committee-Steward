"""
Ingest committee appointment events from S.Res. XML files.

Uses core.events.sres_parser.parse_sres_xml; same evidence-first schema as
load_resolution_events (source, source_document, committee_event). Chamber "S",
bill_type "sres" for committee resolution. Free-text (UC/CREC) parsing is available
via parse_senate_appointment_text; wire a separate loader or source type when needed.

Determinism: Files listed in KNOWN_BAD_SRES_FILES are skipped (corrupt at source).
Add new entries when upstream XML is broken so every run produces the same result.
"""

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import re

from db.connection import get_connection
from core.committees.resolver import committee_name_to_id
from core.events.sres_parser import (
    extract_senate_resolution_state,
    normalize_senate_resolution_name,
    senate_resolution_name_candidates,
    parse_sres_xml,
)
from ingest.event_ledger import compute_event_id_canonical
from ingest.event_state import has_active_appointment
from ingest.rank_observations import (
    RankObservationSlot,
    member_caucus_party_code,
    record_rank_observations,
)

PARSER_ID = "ingest/load_senate_resolution_events.py"
EXTRACTION_MODE = "resolution_structured_senate"

# Files known to be corrupt at source (e.g. unescaped '&' in XML). Skipped for deterministic runs.
# Add new entries when upstream (Congress.gov/GPO) provides broken XML; do not modify the source file.
KNOWN_BAD_SRES_FILES: frozenset[str] = frozenset({
    "BILLS-113sres104is.xml",  # invalid token, line 7, column 82
    "BILLS-113sres264ats.xml",  # invalid token (unescaped & in dc:title), line 6
})


def _is_full_roster_reconstitution(raw_xml: str) -> bool:
    """
    Heuristic: resolutions that 'constitute' or 'reconstitute' committee membership
    list full rosters per committee. Only these should trigger roster-diff REMOVED logic.
    """
    text = re.sub(r"\s+", " ", raw_xml.lower())
    return bool(
        re.search(r"\bconstitute[sd]?\b", text)
        or re.search(r"\breconstitute[sd]?\b", text)
    )


def _is_caucus_scoped(raw_xml: str) -> bool:
    text = re.sub(r"\s+", " ", raw_xml.lower())
    return bool(
        re.search(r"\bmajority\s+(?:party|membership)\b", text)
        or re.search(r"\bminority\s+(?:party|membership)\b", text)
    )


def _infer_party_codes_from_resolved(resolved: list[tuple[str, dict]]) -> set[int] | None:
    """Infer caucus party codes from uniquely resolved names in the source roster."""
    codes: set[int] = set()
    for _, debug_info in resolved:
        candidate_parties = debug_info.get("candidate_parties") or []
        if len(candidate_parties) == 1 and candidate_parties[0] is not None:
            codes.add(candidate_parties[0])
    return codes or None


def _get_committee_members_on_date(
    conn,
    congress_no: int,
    chamber: str,
    committee_code: str,
    as_of_date: str,
    party_codes: set[int] | None = None,
) -> set[str]:
    """
    Return bioguide_ids of members on the committee as of as_of_date.
    Uses committee_event: member is on committee if last event before/on date is APPOINTED.
    If party_codes is set, restrict to members whose party (in member_service) is in that set.
    """
    party_filter = ""
    params: tuple = (congress_no, chamber, committee_code, as_of_date)
    if party_codes:
        placeholders = ",".join("%s" for _ in party_codes)
        party_filter = f"""
            AND ce.bioguide_id IN (
                SELECT bioguide_id FROM member_service
                WHERE congress_no = %s AND chamber = 'S' AND party_code IN ({placeholders})
            )"""
        params = (congress_no, chamber, committee_code, as_of_date, congress_no, *party_codes)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH ranked AS (
                SELECT ce.bioguide_id, ce.action,
                    ROW_NUMBER() OVER (
                        PARTITION BY ce.bioguide_id
                        ORDER BY ce.effective_date DESC, ce.event_id DESC
                    ) AS rn
                FROM committee_event ce
                WHERE ce.congress_no = %s
                  AND ce.chamber = %s
                  AND ce.committee_code = %s
                  AND ce.effective_date <= %s::date
                  AND ce.action IN ('APPOINTED', 'REMOVED')
                  {party_filter}
            )
            SELECT bioguide_id FROM ranked
            WHERE rn = 1 AND action = 'APPOINTED'
            """,
            params,
        )
        rows = cur.fetchall()
    return {r[0] for r in rows}


def _day_before(iso_date: str) -> str:
    """Return ISO date string for the day before the given date."""
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return (d - timedelta(days=1)).isoformat()


# Project-relative: data/resolutions/115th/bills/sres (used when running from repository_root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESOLUTIONS_BASE = _PROJECT_ROOT / "data" / "resolutions"

RESOLVE_WITH_STATE_SQL = """
SELECT DISTINCT m.bioguide_id, s.party_code, {gender_select} AS gender
FROM member_service s
JOIN member m ON m.bioguide_id = s.bioguide_id
WHERE s.congress_no = %s
  AND s.chamber = 'S'
  AND m.last_name = %s
  AND s.state = %s
"""

RESOLVE_WITHOUT_STATE_SQL = """
SELECT DISTINCT m.bioguide_id, s.party_code, {gender_select} AS gender
FROM member_service s
JOIN member m ON m.bioguide_id = s.bioguide_id
WHERE s.congress_no = %s
  AND s.chamber = 'S'
  AND m.last_name = %s
"""

# Deterministic caucus mapping for Senate appointment resolutions.
# 100=Democratic, 200=Republican, 328=Independent caucusing with Democrats.
MAJORITY_PARTY_CODES_BY_CONGRESS: dict[int, set[int]] = {
    113: {100, 328},
    114: {200},  # 114th: Republican majority
    115: {200},  # 115th: Republican majority
    116: {200},  # 116th: Republican majority
    117: {100, 328},  # 117th: Democratic majority, including caucusing independents
    118: {100, 328},  # 118th: Democratic majority, including caucusing independents
}
MINORITY_PARTY_CODES_BY_CONGRESS: dict[int, set[int]] = {
    113: {200},
    114: {100, 328},
    115: {100, 328},
    116: {100, 328},
    117: {200},
    118: {200},
}


def _get_sres_path(congress: int) -> Path:
    """Path to S.Res. directory for a congress. Tries data/resolutions/{congress}th/bills/sres then legacy {congress}/sres."""
    # Layout: data/resolutions/115th/bills/sres
    ordinal = f"{congress}th"
    candidate = DEFAULT_RESOLUTIONS_BASE / ordinal / "bills" / "sres"
    if candidate.exists():
        return candidate
    # Legacy: raw/resolutions/115/sres
    return DEFAULT_RESOLUTIONS_BASE / str(congress) / "sres"


def _get_or_create_source(conn, congress: int) -> int:
    """Reuse the Senate resolution provenance source across ingest replays."""
    version_tag = f"congress_{congress}"
    source_select_sql = """
        SELECT source_id
        FROM source
        WHERE source_type = 'resolution'
          AND source_name = 'S.Res. Congress XML'
          AND version_tag IS NOT DISTINCT FROM %s
        ORDER BY source_id
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(source_select_sql, (version_tag,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """
            INSERT INTO source (source_type, source_name, version_tag)
            VALUES ('resolution', 'S.Res. Congress XML', %s)
            ON CONFLICT ON CONSTRAINT source_identity_key DO NOTHING
            RETURNING source_id
            """,
            (version_tag,),
        )
        row = cur.fetchone()
        if row:
            return row[0]

        cur.execute(source_select_sql, (version_tag,))
        row = cur.fetchone()
        if row:
            return row[0]
        raise RuntimeError(
            "Failed to create or reselect Senate resolution source record"
        )


def _get_or_create_source_document(
    conn, source_id: int, fpath: Path, doc_date: str
) -> int:
    """Reuse a Senate resolution document by content or legacy source identity."""
    content_hash = hashlib.sha256(fpath.read_bytes()).hexdigest()
    document_select_sql = """
        SELECT source_document_id
        FROM source_document
        WHERE content_hash = %s
           OR (
                content_hash IS NULL
                AND source_id = %s
                AND external_id = %s
           )
        ORDER BY (content_hash = %s) DESC NULLS LAST, source_document_id
        LIMIT 1
    """
    select_params = (content_hash, source_id, fpath.name, content_hash)
    with conn.cursor() as cur:
        cur.execute(document_select_sql, select_params)
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """
            INSERT INTO source_document (
                source_id, external_id, doc_date, content_hash
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (content_hash) WHERE content_hash IS NOT NULL DO NOTHING
            RETURNING source_document_id
            """,
            (source_id, fpath.name, doc_date, content_hash),
        )
        row = cur.fetchone()
        if row:
            return row[0]

        cur.execute(document_select_sql, select_params)
        row = cur.fetchone()
        if row:
            return row[0]
        raise RuntimeError(
            f"Failed to create or reselect source_document for {fpath.name}"
        )


def _resolve_senate_member_deterministic(
    conn,
    raw_member: str,
    congress: int,
    has_member_gender_column: bool,
    expected_party_codes: set[int] | None = None,
) -> tuple[str | None, dict]:
    """
    Deterministic S.Res. member resolution by congress/chamber/last_name/(optional state).
    Accept only when exactly one candidate is returned.
    """
    normalized = normalize_senate_resolution_name(raw_member)
    name_candidates = senate_resolution_name_candidates(raw_member)
    state = extract_senate_resolution_state(raw_member)
    honorific_gender = _infer_gender_from_honorific(raw_member)
    if not normalized:
        return None, {
            "raw": raw_member,
            "normalized": normalized,
            "state": state,
            "honorific_gender": honorific_gender,
            "candidate_count": 0,
        }

    rows = []
    chosen_candidate = normalized
    gender_select = "m.gender" if has_member_gender_column else "NULL::text"
    with conn.cursor() as cur:
        for cand in name_candidates:
            sql = (
                RESOLVE_WITH_STATE_SQL.format(gender_select=gender_select)
                if state
                else RESOLVE_WITHOUT_STATE_SQL.format(gender_select=gender_select)
            )
            if state:
                cur.execute(sql, (congress, cand, state))
            else:
                cur.execute(sql, (congress, cand))
            rows = cur.fetchall()
            chosen_candidate = cand
            if rows:
                break

    candidate_count = len(rows)
    candidates = [
        {"bioguide_id": r[0], "party_code": r[1], "gender": _normalize_gender_value(r[2])}
        for r in rows
    ]
    if candidate_count == 1:
        return rows[0][0], {
            "raw": raw_member,
            "normalized": chosen_candidate,
            "state": state,
            "honorific_gender": honorific_gender,
            "candidate_count": candidate_count,
            "gender_tiebreak_used": False,
            "party_tiebreak_used": False,
            "candidate_parties": [rows[0][1]],
            "candidate_genders": [_normalize_gender_value(rows[0][2])],
            "expected_party_codes": sorted(expected_party_codes) if expected_party_codes else None,
        }

    # Deterministic tie-break when token contains explicit honorific ("Mr.", "Ms.", etc.)
    if candidate_count > 1 and honorific_gender:
        filtered = [c for c in candidates if c["gender"] == honorific_gender]
        if len(filtered) == 1:
            return filtered[0]["bioguide_id"], {
                "raw": raw_member,
                "normalized": chosen_candidate,
                "state": state,
                "honorific_gender": honorific_gender,
                "candidate_count": candidate_count,
                "gender_tiebreak_used": True,
                "party_tiebreak_used": False,
                "candidate_parties": [c["party_code"] for c in candidates],
                "candidate_genders": [c["gender"] for c in candidates],
                "expected_party_codes": sorted(expected_party_codes) if expected_party_codes else None,
            }
        if filtered:
            candidates = filtered

    # Deterministic tie-break when bill/section explicitly scopes appointments to a caucus.
    if candidate_count > 1 and expected_party_codes:
        filtered = [c for c in candidates if c["party_code"] in expected_party_codes]
        if len(filtered) == 1:
            return filtered[0]["bioguide_id"], {
                "raw": raw_member,
                "normalized": chosen_candidate,
                "state": state,
                "honorific_gender": honorific_gender,
                "candidate_count": candidate_count,
                "gender_tiebreak_used": False,
                "party_tiebreak_used": True,
                "candidate_parties": [c["party_code"] for c in candidates],
                "candidate_genders": [c["gender"] for c in candidates],
                "expected_party_codes": sorted(expected_party_codes),
            }

    return None, {
        "raw": raw_member,
        "normalized": chosen_candidate,
        "state": state,
        "honorific_gender": honorific_gender,
        "candidate_count": candidate_count,
        "gender_tiebreak_used": False,
        "party_tiebreak_used": False,
        "candidate_parties": [c["party_code"] for c in candidates],
        "candidate_genders": [c["gender"] for c in candidates],
        "expected_party_codes": sorted(expected_party_codes) if expected_party_codes else None,
    }


def _member_has_gender_column(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'member'
              AND column_name = 'gender'
            LIMIT 1
            """
        )
        return cur.fetchone() is not None


def _normalize_gender_value(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"m", "male", "man"}:
        return "M"
    if s in {"f", "female", "woman"}:
        return "F"
    return None


def _infer_gender_from_honorific(raw_member: str) -> str | None:
    if re.search(r"\bMr\.?\b", raw_member, flags=re.IGNORECASE):
        return "M"
    if re.search(r"\b(Mrs\.?|Ms\.?|Miss)\b", raw_member, flags=re.IGNORECASE):
        return "F"
    return None


def _infer_expected_party_codes(raw_xml: str, congress: int) -> set[int] | None:
    """
    Infer deterministic caucus scope from explicit bill language.
    Returns expected party codes when text clearly indicates majority-only or minority-only.
    """
    text = re.sub(r"\s+", " ", raw_xml.lower())
    majority_hit = bool(
        re.search(r"\bmajority\s+party\b", text)
        or re.search(r"\bmajority\s+membership\b", text)
    )
    minority_hit = bool(
        re.search(r"\bminority\s+party\b", text)
        or re.search(r"\bminority\s+membership\b", text)
    )
    if majority_hit and not minority_hit:
        return MAJORITY_PARTY_CODES_BY_CONGRESS.get(congress)
    if minority_hit and not majority_hit:
        return MINORITY_PARTY_CODES_BY_CONGRESS.get(congress)
    return None


def load_senate_resolution_events(
    congress: int = 119,
    base_path: Path | None = None,
    debug_unresolved: bool = False,
    debug_roster_diff: bool = False,
):
    """
    Ingest committee events from S.Res. XML for the given congress.

    Walks *.xml in sres dir, parses each with parse_sres_xml; inserts
    committee_event for each resolved (committee_code, bioguide_id). Chamber "S".

    Path: use base_path to pass a directory directly, or set RESOLUTIONS_BASE to
    the parent of congress dirs (e.g. raw/resolutions). Then 113th/bills/sres
    or 113/sres is used, whichever exists.
    """
    base = base_path or _get_sres_path(congress)
    if not base.exists():
        print(f"S.Res. path does not exist: {base}")
        return

    conn = get_connection()
    valid_committee_codes = set()
    with conn.cursor() as cur:
        cur.execute("SELECT committee_code FROM committee")
        for row in cur.fetchall():
            valid_committee_codes.add(row[0])

    source_id = _get_or_create_source(conn, congress)

    inserted_count = 0
    rank_observations_recorded = 0
    removed_count = 0
    file_removed_counts: dict[str, int] = {}
    files_processed = 0
    skipped_already_active = 0
    unresolved_count = 0
    skipped_untracked_committee_refs = 0
    party_tiebreak_resolved_count = 0
    gender_tiebreak_resolved_count = 0
    # One event per (member, committee, action, date); DB will enforce via UNIQUE.
    seen: set[tuple[int, str, str, str, str, str]] = set()
    has_member_gender_column = _member_has_gender_column(conn)

    sres_file_re = re.compile(rf"^BILLS-{congress}sres\d+[a-z]*\.xml$", re.IGNORECASE)
    for fpath in sorted(base.glob("*.xml")):
        fpath = Path(fpath)
        if not sres_file_re.match(fpath.name):
            continue
        if fpath.name in KNOWN_BAD_SRES_FILES:
            print(f"  [Skip] Known-bad source (corrupt at upstream): {fpath.name}")
            continue
        raw_xml = fpath.read_text()
        try:
            appointments = list(parse_sres_xml(fpath))
        except Exception as e:
            print(f"  [Parse error] {fpath.name}: {e}")
            continue
        if not appointments:
            continue
        expected_party_codes = _infer_expected_party_codes(raw_xml, congress)
        is_full_roster = _is_full_roster_reconstitution(raw_xml)
        is_caucus_scoped = _is_caucus_scoped(raw_xml)

        event_date = appointments[0]["event_date"]
        citation = appointments[0].get("citation") or fpath.stem
        source_document_id = _get_or_create_source_document(
            conn, source_id, fpath, event_date
        )

        for appt_idx, appt in enumerate(appointments):
            committee = appt["committee"]
            raw_members = appt["members"]
            comm_code = committee_name_to_id(committee, bill_type="sres")
            if not comm_code or comm_code not in valid_committee_codes:
                skipped_untracked_committee_refs += len(raw_members)
                if debug_unresolved:
                    print(
                        "  [Skip] Untracked/invalid committee "
                        f"{fpath.name}#appointment[{appt_idx}] "
                        f"committee='{committee}' code={comm_code or '-'} refs={len(raw_members)}"
                    )
                continue
            source_loc = f"{fpath.name}#appointment[{appt_idx}]"
            text_span = f"{citation}: {committee} - {', '.join(raw_members)}"

            # Resolve all members first to build new roster
            resolved: list[tuple[str, dict]] = []
            unresolved: list[tuple[str, dict]] = []
            for raw_m in raw_members:
                if len(raw_m) <= 2:
                    continue
                bioguide_id, debug_info = _resolve_senate_member_deterministic(
                    conn,
                    raw_m,
                    congress,
                    has_member_gender_column=has_member_gender_column,
                    expected_party_codes=expected_party_codes,
                )
                if not bioguide_id:
                    unresolved.append((raw_m, debug_info))
                    continue
                resolved.append((bioguide_id, debug_info))

            if is_caucus_scoped and expected_party_codes is None:
                expected_party_codes = _infer_party_codes_from_resolved(resolved)
                if expected_party_codes and unresolved:
                    retry = unresolved
                    unresolved = []
                    for raw_m, _ in retry:
                        bioguide_id, debug_info = _resolve_senate_member_deterministic(
                            conn,
                            raw_m,
                            congress,
                            has_member_gender_column=has_member_gender_column,
                            expected_party_codes=expected_party_codes,
                        )
                        if bioguide_id:
                            resolved.append((bioguide_id, debug_info))
                        else:
                            unresolved.append((raw_m, debug_info))

            resolved_by_raw = {
                debug_info["raw"]: bioguide_id
                for bioguide_id, debug_info in resolved
            }
            expected_caucus_codes = {
                100 if code == 328 else code
                for code in (expected_party_codes or set())
            }
            inferred_caucus_code = (
                next(iter(expected_caucus_codes))
                if len(expected_caucus_codes) == 1
                else None
            )
            observations = appt.get("member_observations") or [
                {"name": raw_m, "source_ordinal": ordinal, "rank_after": None}
                for ordinal, raw_m in enumerate(raw_members, start=1)
            ]
            rank_slots: list[RankObservationSlot] = []
            for observation in observations:
                raw_m = observation["name"]
                bioguide_id = resolved_by_raw.get(raw_m)
                caucus_party_code = (
                    member_caucus_party_code(
                        conn,
                        bioguide_id=bioguide_id,
                        congress_no=congress,
                        chamber="S",
                        decision_date=event_date,
                    )
                    if bioguide_id
                    else inferred_caucus_code
                )
                rank_slots.append(
                    RankObservationSlot(
                        raw_member_name=raw_m,
                        source_ordinal=observation["source_ordinal"],
                        bioguide_id=bioguide_id,
                        caucus_party_code=caucus_party_code,
                    )
                )
            rank_observations_recorded += record_rank_observations(
                conn,
                congress_no=congress,
                chamber="S",
                committee_code=comm_code,
                decision_date=event_date,
                citation=citation,
                source_document_id=source_document_id,
                source_locator=source_loc,
                source_block_ordinal=appt_idx,
                slots=rank_slots,
                full_roster=is_full_roster,
                extraction_mode=EXTRACTION_MODE,
            )

            for _, debug_info in unresolved:
                unresolved_count += 1
                if debug_unresolved:
                    print(
                        "  [Unresolved] "
                        f"{fpath.name}#appointment[{appt_idx}] "
                        f"raw='{debug_info['raw']}' "
                        f"normalized='{debug_info['normalized']}' "
                        f"state={debug_info['state'] or '-'} "
                        f"honorific_gender={debug_info.get('honorific_gender') or '-'} "
                        f"candidates={debug_info['candidate_count']} "
                        f"candidate_parties={debug_info['candidate_parties']} "
                        f"candidate_genders={debug_info.get('candidate_genders')} "
                        f"expected_parties={debug_info['expected_party_codes']}"
                    )

            new_roster = {bid for bid, _ in resolved}

            # Roster-diff: emit REMOVED for prior members absent from new roster.
            # When party-scoped (majority/minority only), filter prior to same party to avoid
            # incorrectly removing the other party's members.
            if is_full_roster and new_roster:
                prior_roster = _get_committee_members_on_date(
                    conn,
                    congress,
                    "S",
                    comm_code,
                    _day_before(event_date),
                    party_codes=expected_party_codes,
                )
                removed_bioguides = prior_roster - new_roster
                for bid in removed_bioguides:
                    rem_key = (congress, "S", bid, comm_code, "REMOVED", event_date)
                    if rem_key in seen:
                        continue
                    seen.add(rem_key)
                    if debug_roster_diff:
                        file_removed_counts[fpath.name] = file_removed_counts.get(fpath.name, 0) + 1
                    rem_event_id = compute_event_id_canonical(
                        congress, "S", bid, comm_code, "REMOVED", event_date
                    )
                    rem_loc = f"{source_loc}#roster-diff"
                    rem_span = f"{citation}: {committee} roster reconstituted; {bid} absent from new roster"
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO committee_event (
                                event_id, congress_no, chamber, bioguide_id, committee_code,
                                action, decision_date, effective_date,
                                source_document_id, source_locator, text_span, extraction_mode
                            ) VALUES (%s, %s, 'S', %s, %s, 'REMOVED', %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (congress_no, chamber, bioguide_id, committee_code, action, decision_date) DO NOTHING
                            """,
                            (
                                rem_event_id, congress, bid, comm_code,
                                event_date, event_date, source_document_id, rem_loc,
                                rem_span, EXTRACTION_MODE,
                            ),
                        )
                        if cur.rowcount:
                            removed_count += 1

            # APPOINTED events for resolved members
            for bioguide_id, debug_info in resolved:
                if debug_info.get("gender_tiebreak_used"):
                    gender_tiebreak_resolved_count += 1
                if debug_info.get("party_tiebreak_used"):
                    party_tiebreak_resolved_count += 1
                    if debug_unresolved:
                        print(
                            "  [ResolvedByParty] "
                            f"{fpath.name}#appointment[{appt_idx}] "
                            f"raw='{debug_info['raw']}' "
                            f"normalized='{debug_info['normalized']}' "
                            f"state={debug_info['state'] or '-'} "
                            f"honorific_gender={debug_info.get('honorific_gender') or '-'} "
                            f"candidate_parties={debug_info['candidate_parties']} "
                            f"candidate_genders={debug_info.get('candidate_genders')} "
                            f"expected_parties={debug_info['expected_party_codes']}"
                        )
                key = (congress, "S", bioguide_id, comm_code, "APPOINTED", event_date)
                if key in seen:
                    continue
                seen.add(key)
                if has_active_appointment(
                    conn, congress, "S", bioguide_id, comm_code, event_date
                ):
                    skipped_already_active += 1
                    continue
                event_id = compute_event_id_canonical(
                    congress, "S", bioguide_id, comm_code, "APPOINTED", event_date
                )
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO committee_event (
                            event_id, congress_no, chamber, bioguide_id, committee_code,
                            action, decision_date, effective_date,
                            source_document_id, source_locator, text_span, extraction_mode
                        ) VALUES (%s, %s, 'S', %s, %s, 'APPOINTED', %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (congress_no, chamber, bioguide_id, committee_code, action, decision_date) DO NOTHING
                        """,
                        (
                            event_id, congress, bioguide_id, comm_code,
                            event_date, event_date, source_document_id, source_loc,
                            text_span, EXTRACTION_MODE,
                        ),
                    )
                    if cur.rowcount:
                        inserted_count += 1
        files_processed += 1

    conn.commit()
    conn.close()
    print(f"\nS.RES. INGESTION COMPLETE (congress {congress})")
    print(f"Files processed: {files_processed}")
    print(f"Events inserted: {inserted_count}")
    print(f"Rank observations recorded: {rank_observations_recorded}")
    print(f"Removed (roster-diff): {removed_count}")
    if debug_roster_diff and file_removed_counts:
        for fname in sorted(file_removed_counts):
            print(f"  [roster-diff] {fname}: {file_removed_counts[fname]} REMOVED")
    print(f"Skipped (already active appointment): {skipped_already_active}")
    print(f"Unresolved member tokens: {unresolved_count}")
    print(f"Skipped member refs (untracked/invalid committees): {skipped_untracked_committee_refs}")
    print(f"Ambiguities resolved by gender tie-break: {gender_tiebreak_resolved_count}")
    print(f"Ambiguities resolved by party tie-break: {party_tiebreak_resolved_count}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--congress", type=int, default=119)
    ap.add_argument(
        "--base",
        type=Path,
        default=None,
        help="Path to congress sres directory (e.g. data/resolutions/114th/bills/sres)",
    )
    ap.add_argument(
        "--debug-unresolved",
        action="store_true",
        help="Print unresolved Senate member tokens with normalization/state/candidate counts",
    )
    ap.add_argument(
        "--debug-roster-diff",
        action="store_true",
        help="Print per-file REMOVED counts from roster-diff",
    )
    args = ap.parse_args()
    load_senate_resolution_events(
        congress=args.congress,
        base_path=args.base,
        debug_unresolved=args.debug_unresolved,
        debug_roster_diff=args.debug_roster_diff,
    )
