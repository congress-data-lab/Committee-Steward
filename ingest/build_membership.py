"""
Derive committee_membership from committee_event (APPOINTED/REMOVED).

Chronological Event Ledger: pool APPOINTED and REMOVED events, sort by date,
replay sequentially to build valid_daterange intervals. Supports leave-rejoin
(multiple intervals per bioguide_id, committee_code, congress_no).
Satisfies the EXCLUDE constraint (no overlapping valid_daterange).
"""

import argparse
from datetime import date, timedelta

from db.connection import get_connection

PARSER_ID = "ingest/build_membership.py"


def _merge_duplicate_source_one_day_intervals(
    intervals: list[tuple[str, str, int, date, date, str, str | None]],
) -> list[tuple[str, str, int, date, date, str, str | None]]:
    """
    Merge adjacent one-day intervals that are artifacts of duplicate sourcing
    (Journal + Resolution same appointment). When interval A is [D, D+1), has
    end_event_id=None, and next interval B is [D+1, end), replace A and B with
    one interval [D, end) keeping A's start_event_id and B's end_event_id.
    """
    if not intervals:
        return intervals
    key = (intervals[0][0], intervals[0][1], intervals[0][2])  # bioguide_id, committee_code, cno
    merged: list[tuple[str, str, int, date, date, str, str | None]] = []
    i = 0
    while i < len(intervals):
        bioguide_id, committee_code, cno, range_start, range_end, start_event_id, end_event_id = intervals[i]
        # Look for next interval that starts at range_end and this one is one-day with no end_event
        one_day = (range_end - range_start) == timedelta(days=1)
        if one_day and end_event_id is None and i + 1 < len(intervals):
            nxt = intervals[i + 1]
            if (nxt[0], nxt[1], nxt[2]) == key and nxt[3] == range_end:
                # Merge: keep this start_event_id and range_start; take next's range_end and end_event_id
                merged.append((bioguide_id, committee_code, cno, range_start, nxt[4], start_event_id, nxt[6]))
                i += 2
                continue
        merged.append(intervals[i])
        i += 1
    return merged


def _event_sort_key(event: tuple[str, date, str]) -> tuple[date, int, str]:
    """Apply appointments before removals on the same effective date."""
    event_id, event_date, action = event
    return event_date, 0 if action == "APPOINTED" else 1, event_id


def _group_event_rows(
    rows,
    valid_congresses: set[int],
    congress: int | None = None,
) -> dict[tuple[str, str, int], list[tuple[str, date, str]]]:
    groups: dict[tuple[str, str, int], list[tuple[str, date, str]]] = {}
    for event_id, effective_date, action, bioguide_id, committee_code, congress_no in rows:
        if congress_no not in valid_congresses:
            continue
        if congress is not None and congress_no != congress:
            continue
        key = (bioguide_id, committee_code, congress_no)
        groups.setdefault(key, []).append((event_id, effective_date, action))
    return groups


def _replay_events(
    events: list[tuple[str, date, str]],
    congress_end: date,
) -> list[tuple[str, date, date, str | None]]:
    """
    Replay sorted events (event_id, effective_date, action) to produce intervals.
    Returns list of (start_event_id, range_start, range_end, end_event_id).
    action is 'APPOINTED' or 'REMOVED'. range_start is always date (never None).
    """
    intervals: list[tuple[str, date, date, str | None]] = []
    open_start_event_id: str | None = None
    open_start_date: date | None = None

    for event_id, event_date, event_type in events:
        start_id = open_start_event_id
        start_date = open_start_date
        if event_type == "APPOINTED":
            if start_id is not None and start_date is not None:
                # Close previous interval at this date (exclusive end)
                if event_date > start_date:
                    intervals.append((start_id, start_date, event_date, None))
            open_start_event_id = event_id
            open_start_date = event_date
        elif event_type == "REMOVED":
            if start_id is not None and start_date is not None:
                if event_date > start_date:
                    intervals.append((start_id, start_date, event_date, event_id))
                open_start_event_id = None
                open_start_date = None

    start_id = open_start_event_id
    start_date = open_start_date
    if start_id is not None and start_date is not None:
        intervals.append((start_id, start_date, congress_end, None))

    return intervals


def build_membership(congress: int | None = None):
    """
    Build committee_membership from committee_event for the given congress
    (or all congresses if None). Uses Chronological Event Ledger: pool
    APPOINTED + REMOVED, sort by date, replay to produce intervals.
    """
    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT congress_no, start_date, end_date FROM congress ORDER BY congress_no"
        )
        congress_dates = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    # Get APPOINTED and REMOVED events (evidence-first schema: event_id, effective_date, action)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ce.event_id, ce.effective_date, ce.action, ce.bioguide_id,
                   ce.committee_code, ce.congress_no
            FROM committee_event ce
            WHERE ce.action IN ('APPOINTED', 'REMOVED')
              AND ce.bioguide_id IS NOT NULL AND ce.committee_code IS NOT NULL
            ORDER BY ce.effective_date,
                     CASE ce.action WHEN 'APPOINTED' THEN 0 ELSE 1 END,
                     ce.event_id
            """
        )
        rows = cur.fetchall()

    # Group by (bioguide_id, committee_code, congress_no), filter by congress
    groups = _group_event_rows(rows, set(congress_dates), congress)

    # Replay each group to produce intervals, then merge duplicate-source one-day intervals
    intervals_to_insert: list[tuple[str, str, int, date, date, str, str | None]] = []
    for (bioguide_id, committee_code, cno), evs in groups.items():
        evs.sort(key=_event_sort_key)
        _, congress_end = congress_dates[cno]
        group_intervals: list[tuple[str, str, int, date, date, str, str | None]] = []
        for start_event_id, range_start, range_end, end_event_id in _replay_events(
            evs, congress_end
        ):
            # Clamp range_start to congress start
            congress_start, _ = congress_dates[cno]
            if range_start < congress_start:
                range_start = congress_start
            group_intervals.append(
                (bioguide_id, committee_code, cno, range_start, range_end, start_event_id, end_event_id)
            )
        merged = _merge_duplicate_source_one_day_intervals(group_intervals)
        intervals_to_insert.extend(merged)

    # Delete existing membership for congress(es) we're rebuilding
    congresses_to_rebuild = {cno for (_, _, cno, *_) in intervals_to_insert}
    if congress is not None:
        congresses_to_rebuild.add(congress)

    with conn.cursor() as cur:
        for cno in congresses_to_rebuild:
            cur.execute(
                "DELETE FROM committee_membership WHERE congress_no = %s",
                (cno,),
            )

    # Insert intervals
    inserted = 0
    with conn.cursor() as cur:
        for bioguide_id, committee_code, cno, range_start, range_end, start_event_id, end_event_id in intervals_to_insert:
            valid_range = f"[{range_start!s},{range_end!s})"
            cur.execute(
                """
                INSERT INTO committee_membership
                (bioguide_id, committee_code, congress_no, valid_daterange, start_event_id, end_event_id, parsed_source, parser_id)
                VALUES (%s, %s, %s, %s::daterange, %s, %s, 'committee_event', %s)
                """,
                (bioguide_id, committee_code, cno, valid_range, start_event_id, end_event_id, PARSER_ID),
            )
            inserted += 1

    conn.commit()
    conn.close()
    print("\nBUILD MEMBERSHIP COMPLETE")
    print(f"Membership rows inserted: {inserted}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--congress", type=int, nargs="+")
    args = parser.parse_args()
    if args.congress:
        for congress_no in sorted(set(args.congress)):
            build_membership(congress_no)
    else:
        build_membership()
