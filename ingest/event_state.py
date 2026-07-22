"""
Helpers for event-state aware dedupe decisions during ingestion.
"""


def _latest_action_as_of(
    conn,
    congress_no: int,
    chamber: str,
    bioguide_id: str,
    committee_code: str,
    as_of_date: str,
) -> str | None:
    """Return latest action at/before as_of_date, or None when no prior event exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT action
            FROM committee_event
            WHERE congress_no = %s
              AND chamber = %s
              AND bioguide_id = %s
              AND committee_code = %s
              AND effective_date <= %s::date
            ORDER BY effective_date DESC, decision_date DESC, event_id DESC
            LIMIT 1
            """,
            (congress_no, chamber, bioguide_id, committee_code, as_of_date),
        )
        row = cur.fetchone()
    return row[0] if row else None


def has_active_appointment(
    conn,
    congress_no: int,
    chamber: str,
    bioguide_id: str,
    committee_code: str,
    as_of_date: str,
) -> bool:
    """
    Return True when the latest event at/before as_of_date is APPOINTED.
    """
    return (
        _latest_action_as_of(
            conn, congress_no, chamber, bioguide_id, committee_code, as_of_date
        )
        == "APPOINTED"
    )


def has_active_removal(
    conn,
    congress_no: int,
    chamber: str,
    bioguide_id: str,
    committee_code: str,
    as_of_date: str,
) -> bool:
    """
    Return True when the latest event at/before as_of_date is REMOVED.
    """
    return (
        _latest_action_as_of(
            conn, congress_no, chamber, bioguide_id, committee_code, as_of_date
        )
        == "REMOVED"
    )
