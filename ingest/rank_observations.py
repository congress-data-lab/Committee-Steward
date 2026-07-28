"""Persist ordered committee-roster evidence without mutating appointment events."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RankObservationSlot:
    raw_member_name: str
    source_ordinal: int
    bioguide_id: str | None
    caucus_party_code: int | None
    rank_after_raw_name: str | None = None
    rank_after_bioguide_id: str | None = None


def member_caucus_party_code(
    conn,
    *,
    bioguide_id: str,
    congress_no: int,
    chamber: str,
    decision_date: str,
) -> int | None:
    """Return the committee-seniority party, falling back to ballot party."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(caucus_party_code, party_code)
            FROM member_service
            WHERE bioguide_id = %s
              AND congress_no = %s
              AND chamber = %s
              AND %s::date <@ valid_daterange
            ORDER BY lower(valid_daterange) DESC
            LIMIT 1
            """,
            (bioguide_id, congress_no, chamber, decision_date),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def infer_block_party(slots: list[RankObservationSlot]) -> list[RankObservationSlot]:
    """Fill unresolved source slots only when every resolved slot has one caucus."""
    known = {slot.caucus_party_code for slot in slots if slot.caucus_party_code is not None}
    if len(known) != 1:
        return slots
    party_code = next(iter(known))
    return [
        replace(slot, caucus_party_code=party_code)
        if slot.caucus_party_code is None
        else slot
        for slot in slots
    ]


def resolution_number(citation: str) -> int | None:
    match = re.search(r"\bRES\.?\s*(\d+)\b", citation, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def compute_rank_observation_id(
    *,
    congress_no: int,
    chamber: str,
    committee_code: str,
    citation: str,
    source_locator: str,
    source_ordinal: int,
    raw_member_name: str,
) -> str:
    payload = {
        "congress_no": congress_no,
        "chamber": chamber,
        "committee_code": committee_code,
        "citation": citation,
        "source_locator": source_locator,
        "source_ordinal": source_ordinal,
        "raw_member_name": raw_member_name,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_rank_observations(
    conn,
    *,
    congress_no: int,
    chamber: str,
    committee_code: str,
    decision_date: str,
    citation: str,
    source_document_id: int,
    source_locator: str,
    source_block_ordinal: int,
    slots: list[RankObservationSlot],
    full_roster: bool = False,
    extraction_mode: str,
) -> int:
    """Insert idempotent ordered slots, retaining unresolved names and rank gaps."""
    inserted = 0
    for slot in infer_block_party(slots):
        observation_kind = (
            "RELATIVE_ORDER"
            if slot.rank_after_raw_name
            else "FULL_ROSTER"
            if full_roster
            else "ORDERED_LIST"
        )
        observation_id = compute_rank_observation_id(
            congress_no=congress_no,
            chamber=chamber,
            committee_code=committee_code,
            citation=citation,
            source_locator=source_locator,
            source_ordinal=slot.source_ordinal,
            raw_member_name=slot.raw_member_name,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO committee_rank_observation (
                    rank_observation_id, congress_no, chamber, committee_code,
                    bioguide_id, raw_member_name, caucus_party_code, decision_date,
                    source_document_id, source_locator, source_block_ordinal,
                    source_member_ordinal, resolution_number, rank_after_raw_name,
                    rank_after_bioguide_id, observation_kind, extraction_mode
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (source_document_id, source_locator, source_member_ordinal)
                DO UPDATE SET
                    bioguide_id = COALESCE(
                        committee_rank_observation.bioguide_id,
                        EXCLUDED.bioguide_id
                    ),
                    caucus_party_code = COALESCE(
                        committee_rank_observation.caucus_party_code,
                        EXCLUDED.caucus_party_code
                    ),
                    rank_after_bioguide_id = COALESCE(
                        committee_rank_observation.rank_after_bioguide_id,
                        EXCLUDED.rank_after_bioguide_id
                    )
                """,
                (
                    observation_id,
                    congress_no,
                    chamber,
                    committee_code,
                    slot.bioguide_id,
                    slot.raw_member_name,
                    slot.caucus_party_code,
                    decision_date,
                    source_document_id,
                    source_locator,
                    source_block_ordinal,
                    slot.source_ordinal,
                    resolution_number(citation),
                    slot.rank_after_raw_name,
                    slot.rank_after_bioguide_id,
                    observation_kind,
                    extraction_mode,
                ),
            )
            inserted += max(cur.rowcount, 0)
    return inserted
