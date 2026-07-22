"""Build dated committee party-rank intervals from ordered resolution evidence."""

from __future__ import annotations

import argparse
from datetime import date

from core.ranking import (
    MembershipRecord,
    PartyPeriod,
    RankObservation,
    derive_rank_intervals,
)
from db.connection import get_connection


PARSER_ID = "ingest/build_membership_ranks.py"


def _load_inputs(conn, congresses: list[int]):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              cm.committee_membership_id,
              cm.bioguide_id,
              cm.congress_no,
              c.chamber,
              cm.committee_code,
              lower(cm.valid_daterange)::date,
              upper(cm.valid_daterange)::date
            FROM committee_membership cm
            JOIN committee c ON c.committee_code = cm.committee_code
            WHERE cm.congress_no = ANY(%s)
            ORDER BY cm.congress_no, c.chamber, cm.committee_code,
                     lower(cm.valid_daterange), cm.bioguide_id
            """,
            (congresses,),
        )
        memberships = [MembershipRecord(*row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT DISTINCT
              ms.bioguide_id,
              ms.congress_no,
              ms.chamber,
              lower(ms.valid_daterange)::date,
              upper(ms.valid_daterange)::date,
              COALESCE(ms.caucus_party_code, ms.party_code)
            FROM member_service ms
            WHERE ms.congress_no = ANY(%s)
              AND COALESCE(ms.caucus_party_code, ms.party_code) IS NOT NULL
            ORDER BY ms.congress_no, ms.chamber, ms.bioguide_id,
                     lower(ms.valid_daterange)
            """,
            (congresses,),
        )
        party_periods = [PartyPeriod(*row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT
              rank_observation_id,
              congress_no,
              chamber,
              committee_code,
              decision_date,
              resolution_number,
              source_block_ordinal,
              source_member_ordinal,
              raw_member_name,
              bioguide_id,
              caucus_party_code,
              rank_after_bioguide_id,
              rank_after_raw_name,
              observation_kind
            FROM committee_rank_observation
            WHERE congress_no = ANY(%s)
            ORDER BY congress_no, chamber, committee_code, decision_date,
                     resolution_number NULLS LAST, source_block_ordinal,
                     source_member_ordinal, rank_observation_id
            """,
            (congresses,),
        )
        observations = [RankObservation(*row) for row in cur.fetchall()]
    return memberships, party_periods, observations


def build_membership_ranks(
    congresses: list[int], *, dry_run: bool = False, conn=None
) -> int:
    if not congresses:
        raise ValueError("At least one Congress is required")
    congresses = sorted(set(congresses))
    owns_connection = conn is None
    conn = conn or get_connection()
    try:
        memberships, party_periods, observations = _load_inputs(conn, congresses)
        intervals = derive_rank_intervals(memberships, party_periods, observations)
        if dry_run:
            conn.rollback()
            return len(intervals)

        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM committee_membership_rank cmr
                USING committee_membership cm
                WHERE cmr.committee_membership_id = cm.committee_membership_id
                  AND cm.congress_no = ANY(%s)
                """,
                (congresses,),
            )
            for interval in intervals:
                cur.execute(
                    """
                    INSERT INTO committee_membership_rank (
                      committee_membership_id, caucus_party_code, rank_in_party,
                      unresolved_slots_before, valid_daterange, rank_basis,
                      source_rank_observation_id, parsed_source, parser_id
                    ) VALUES (
                      %s, %s, %s, %s, daterange(%s, %s, '[)'), %s, %s,
                      'committee_rank_observation', %s
                    )
                    """,
                    (
                        interval.committee_membership_id,
                        interval.caucus_party_code,
                        interval.rank_in_party,
                        interval.unresolved_slots_before,
                        interval.start_date,
                        interval.end_date,
                        interval.rank_basis,
                        interval.source_rank_observation_id,
                        PARSER_ID,
                    ),
                )
        conn.commit()
        return len(intervals)
    finally:
        if owns_connection:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--congress", type=int, nargs="+", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    inserted = build_membership_ranks(args.congress, dry_run=args.dry_run)
    print(f"Committee rank intervals {'derived' if args.dry_run else 'inserted'}: {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
