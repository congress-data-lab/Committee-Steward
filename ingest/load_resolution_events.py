"""
Ingest committee appointment events from H.Res. XML files.

Creates source/source_document records, parses resolution XML, resolves
committee and member names, and inserts committee_event rows (evidence-first schema).
"""

import hashlib
import os
from pathlib import Path

from db.connection import get_connection
from core.committees.resolver import committee_name_to_id
from core.events.hres_parser import parse_hres_xml
from core.members.resolver import MemberResolver, MemberResolutionError
from ingest.event_ledger import compute_event_id_canonical
from ingest.event_state import has_active_appointment
from ingest.rank_observations import (
    RankObservationSlot,
    member_caucus_party_code,
    record_rank_observations,
)

PARSER_ID = "ingest/load_resolution_events.py"
EXTRACTION_MODE = "resolution_structured"

# Base path for resolution files (configurable via env)
DEFAULT_RESOLUTIONS_BASE = Path(
    os.environ.get("RESOLUTIONS_BASE", "data/resolutions")
)


def _get_resolution_path(congress: int) -> Path:
    """Get path to resolution directory for a congress."""
    congress_label = f"{congress}th"
    return DEFAULT_RESOLUTIONS_BASE / congress_label / "bills" / "hres"


def _get_or_create_source(conn, congress: int) -> int:
    """Reuse the provenance source for a congress across ingest replays."""
    version_tag = f"congress_{congress}"
    source_select_sql = """
        SELECT source_id
        FROM source
        WHERE source_type = 'resolution'
          AND source_name = 'H.Res. Congress XML'
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
            VALUES ('resolution', 'H.Res. Congress XML', %s)
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
        raise RuntimeError("Failed to create or reselect resolution source record")


def _get_or_create_source_document(
    conn, source_id: int, fpath: Path, doc_date: str
) -> int:
    """Reuse a resolution document by content or its legacy source identity."""
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


def load_resolution_events(congress: int = 113, base_path: Path | None = None, dry_run: bool = False):
    """
    Ingest committee events from H.Res. files for the given congress.

    Walks *eh.xml (Engrossed in House) files, filters by "electing" in title,
    parses each file, creates source/source_document, and inserts committee_event.

    If dry_run is True, print what would be inserted but don't commit to DB.
    """
    base = base_path or _get_resolution_path(congress)
    if not base.exists():
        print(f"Resolution path does not exist: {base}")
        return

    conn = get_connection()
    member_resolver = MemberResolver(conn)

    # Pre-fetch valid committee codes
    valid_committee_codes = set()
    with conn.cursor() as cur:
        cur.execute("SELECT committee_code FROM committee")
        for row in cur.fetchall():
            valid_committee_codes.add(row[0])

    source_id = _get_or_create_source(conn, congress)

    inserted_count = 0
    files_processed = 0
    skipped_already_active = 0
    rank_observations_recorded = 0
    seen: set[tuple[int, str, str, str, str, str]] = set()

    for fpath in sorted(base.glob("*eh.xml")):
        fpath = Path(fpath)
        try:
            appointments = list(parse_hres_xml(fpath))
        except Exception as e:
            print(f"  [Warning] Failed to parse {fpath.name}: {e}")
            continue

        if not appointments:
            continue

        # Get event_date and citation from first appointment (same for all in file)
        event_date = appointments[0]["event_date"]
        citation = appointments[0]["citation"]

        source_document_id = _get_or_create_source_document(
            conn, source_id, fpath, event_date
        )

        for appt_idx, appt in enumerate(appointments):
            committee = appt["committee"]
            raw_members = appt["members"]
            action = appt["action"]

            comm_code = committee_name_to_id(committee, bill_type="hres")
            if not comm_code or comm_code not in valid_committee_codes:
                continue

            source_loc = f"{fpath.name}#appointment[{appt_idx}]"
            text_span = f"{citation}: {committee} - {', '.join(raw_members)}"

            observations = appt.get("member_observations") or [
                {"name": raw_m, "source_ordinal": ordinal, "rank_after": None}
                for ordinal, raw_m in enumerate(raw_members, start=1)
            ]
            resolved_slots: list[RankObservationSlot] = []
            for observation in observations:
                raw_m = observation["name"]
                bioguide_id = None
                if len(raw_m) > 2:
                    try:
                        bioguide_id = member_resolver.resolve(
                            raw_m, congress, "H", event_date=event_date
                        )
                    except MemberResolutionError:
                        pass
                rank_after_raw = observation.get("rank_after")
                rank_after_bioguide_id = None
                if rank_after_raw:
                    try:
                        rank_after_bioguide_id = member_resolver.resolve(
                            rank_after_raw, congress, "H", event_date=event_date
                        )
                    except MemberResolutionError:
                        pass
                caucus_party_code = (
                    member_caucus_party_code(
                        conn,
                        bioguide_id=bioguide_id,
                        congress_no=congress,
                        chamber="H",
                        decision_date=event_date,
                    )
                    if bioguide_id
                    else None
                )
                resolved_slots.append(
                    RankObservationSlot(
                        raw_member_name=raw_m,
                        source_ordinal=observation["source_ordinal"],
                        bioguide_id=bioguide_id,
                        caucus_party_code=caucus_party_code,
                        rank_after_raw_name=rank_after_raw,
                        rank_after_bioguide_id=rank_after_bioguide_id,
                    )
                )

            if action == "APPOINTED":
                rank_observations_recorded += record_rank_observations(
                    conn,
                    congress_no=congress,
                    chamber="H",
                    committee_code=comm_code,
                    decision_date=event_date,
                    citation=citation,
                    source_document_id=source_document_id,
                    source_locator=source_loc,
                    source_block_ordinal=appt_idx,
                    slots=resolved_slots,
                    extraction_mode=EXTRACTION_MODE,
                )

            for slot in resolved_slots:
                raw_m = slot.raw_member_name
                if len(raw_m) <= 2:
                    continue
                bioguide_id = slot.bioguide_id
                if not bioguide_id:
                    continue

                key = (congress, "H", bioguide_id, comm_code, action, event_date)
                if key in seen:
                    continue
                seen.add(key)
                if action == "APPOINTED" and has_active_appointment(
                    conn, congress, "H", bioguide_id, comm_code, event_date
                ):
                    skipped_already_active += 1
                    continue
                event_id = compute_event_id_canonical(
                    congress, "H", bioguide_id, comm_code, action, event_date
                )
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO committee_event (
                            event_id, congress_no, chamber, bioguide_id, committee_code,
                            action, decision_date, effective_date,
                            source_document_id, source_locator, text_span, extraction_mode
                        ) VALUES (%s, %s, 'H', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (
                            event_id, congress, bioguide_id, comm_code, action,
                            event_date, event_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                        ),
                    )
                    if cur.rowcount:
                        inserted_count += 1

        files_processed += 1

    if dry_run:
        print(f"\n[DRY RUN] Not committing to database")
    else:
        conn.commit()
    conn.close()

    print(f"\nRESOLUTION INGESTION COMPLETE (congress {congress})")
    print(f"Files processed: {files_processed}")
    print(f"Events inserted: {inserted_count}")
    print(f"Rank observations recorded: {rank_observations_recorded}")
    print(f"Skipped (already active appointment): {skipped_already_active}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Ingest House committee events from H.Res. XML for a congress.")
    p.add_argument("-c", "--congress", type=int, default=113, help="Congress number (e.g. 114)")
    p.add_argument("--base", type=Path, default=None, help="Override path to congress/bills/hres directory")
    p.add_argument("--dry-run", action="store_true", help="Don't write to database; just show what would be inserted")
    args = p.parse_args()
    load_resolution_events(
        congress=args.congress,
        base_path=args.base if args.base is not None else None,
        dry_run=args.dry_run,
    )
