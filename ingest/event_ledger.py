"""
Evidence-first committee_event helpers.

event_id = sha256 of invariant fields for idempotent upsert.
"""

import hashlib


def compute_event_id(
    congress_no: int,
    chamber: str,
    bioguide_id: str,
    committee_code: str,
    action: str,
    decision_date: str,
    source_document_id: int,
    source_locator: str,
) -> str:
    """Content-addressed event_id for idempotent upsert (includes source)."""
    payload = "|".join(
        str(x)
        for x in [
            congress_no,
            chamber,
            bioguide_id,
            committee_code,
            action,
            decision_date,
            source_document_id,
            source_locator,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_event_id_canonical(
    congress_no: int,
    chamber: str,
    bioguide_id: str,
    committee_code: str,
    action: str,
    decision_date: str,
) -> str:
    """Event_id from logical appointment only (no source). One row per (member, committee, action, date)."""
    payload = "|".join(
        str(x)
        for x in [
            congress_no,
            chamber,
            bioguide_id,
            committee_code,
            action,
            decision_date,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()
