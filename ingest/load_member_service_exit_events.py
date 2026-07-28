"""Derive committee removals when a member's chamber service ends early."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date

from psycopg2.extras import Json

from db.connection import get_connection
EXTRACTION_MODE = "member_service_exit"
SOURCE_NAME = "Member service ranges"


@dataclass(frozen=True)
class ServiceRange:
    member_service_id: int
    bioguide_id: str
    chamber: str
    congress_no: int
    start_date: date
    end_date: date
    parsed_source: str
    parser_id: str
    source_content_hash: str | None = None


@dataclass(frozen=True)
class CommitteeTransition:
    event_id: str
    bioguide_id: str
    chamber: str
    committee_code: str
    action: str
    decision_date: date
    effective_date: date


@dataclass(frozen=True)
class ServiceExitCandidate:
    service: ServiceRange
    committee_code: str

    @property
    def exit_date(self) -> date:
        return self.service.end_date


def terminal_service_exits(
    ranges: list[ServiceRange], congress_end: date
) -> list[ServiceRange]:
    """Return one terminal, early-ending service range per member and chamber."""
    terminal: dict[tuple[str, str, int], ServiceRange] = {}
    for service in ranges:
        key = (service.bioguide_id, service.chamber, service.congress_no)
        current = terminal.get(key)
        if current is None or (service.end_date, service.member_service_id) > (
            current.end_date,
            current.member_service_id,
        ):
            terminal[key] = service
    return sorted(
        (service for service in terminal.values() if service.end_date < congress_end),
        key=lambda service: (
            service.end_date,
            service.chamber,
            service.bioguide_id,
            service.member_service_id,
        ),
    )


def service_exit_candidates(
    ranges: list[ServiceRange],
    transitions: list[CommitteeTransition],
    congress_end: date,
) -> list[ServiceExitCandidate]:
    """Find committees whose latest state at an early service exit is APPOINTED."""
    exits = terminal_service_exits(ranges, congress_end)
    by_member: dict[tuple[str, str], list[CommitteeTransition]] = {}
    for transition in transitions:
        by_member.setdefault(
            (transition.bioguide_id, transition.chamber), []
        ).append(transition)

    candidates: list[ServiceExitCandidate] = []
    for service in exits:
        latest: dict[str, CommitteeTransition] = {}
        for transition in by_member.get((service.bioguide_id, service.chamber), []):
            if transition.effective_date > service.end_date:
                continue
            current = latest.get(transition.committee_code)
            transition_key = (
                transition.effective_date,
                transition.decision_date,
                transition.action == "REMOVED",
                transition.event_id,
            )
            current_key = (
                current.effective_date,
                current.decision_date,
                current.action == "REMOVED",
                current.event_id,
            ) if current else None
            if current_key is None or transition_key > current_key:
                latest[transition.committee_code] = transition
        candidates.extend(
            ServiceExitCandidate(service, committee_code)
            for committee_code, transition in latest.items()
            if transition.action == "APPOINTED"
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.exit_date,
            candidate.service.chamber,
            candidate.service.bioguide_id,
            candidate.committee_code,
        ),
    )


def evidence_payload(service: ServiceRange) -> dict[str, object]:
    """Build the canonical evidence payload stored with a service-exit event."""
    return {
        "bioguide_id": service.bioguide_id,
        "chamber": service.chamber,
        "congress_no": service.congress_no,
        "start_date": service.start_date.isoformat(),
        "end_date": service.end_date.isoformat(),
        "parsed_source": service.parsed_source,
        "parser_id": service.parser_id,
        "source_content_hash": service.source_content_hash,
        "derivation": "terminal member_service range ends before Congress",
    }


def service_evidence_key(service: ServiceRange) -> str:
    """Return a database-independent key for one canonical service range."""
    canonical = json.dumps(
        evidence_payload(service), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derived_event_id(candidate: ServiceExitCandidate) -> str:
    """Return a stable internal ID without database surrogate identifiers."""
    payload = {
        "congress_no": candidate.service.congress_no,
        "chamber": candidate.service.chamber,
        "bioguide_id": candidate.service.bioguide_id,
        "committee_code": candidate.committee_code,
        "action": "REMOVED",
        "effective_date": candidate.exit_date.isoformat(),
        "extraction_mode": EXTRACTION_MODE,
        "evidence_key": service_evidence_key(candidate.service),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reconcile_derived_event_ids(
    existing_ids: set[str], desired_ids: set[str]
) -> tuple[set[str], set[str]]:
    """Return ``(stale_ids, missing_ids)`` for desired-state reconciliation."""
    return existing_ids - desired_ids, desired_ids - existing_ids


def _load_inputs(conn, congress_no: int) -> tuple[date, list[ServiceRange], list[CommitteeTransition]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT end_date FROM congress WHERE congress_no=%s",
            (congress_no,),
        )
        congress_row = cur.fetchone()
        if congress_row is None:
            raise ValueError(f"Unknown Congress: {congress_no}")
        congress_end = congress_row[0]
        cur.execute(
            """
            SELECT member_service_id, bioguide_id, chamber, congress_no,
                   lower(valid_daterange)::date, upper(valid_daterange)::date,
                   ms.parsed_source, ms.parser_id, sd.content_hash
            FROM member_service ms
            LEFT JOIN source_document sd
              ON sd.source_document_id = ms.source_document_id
            WHERE ms.congress_no=%s AND NOT upper_inf(ms.valid_daterange)
            ORDER BY ms.bioguide_id, ms.chamber, lower(ms.valid_daterange),
                     ms.member_service_id
            """,
            (congress_no,),
        )
        ranges = [ServiceRange(*row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT event_id, bioguide_id, chamber, committee_code, action,
                   decision_date, effective_date
            FROM committee_event
            WHERE congress_no=%s
              AND extraction_mode IS DISTINCT FROM %s
            ORDER BY effective_date, decision_date, event_id
            """,
            (congress_no, EXTRACTION_MODE),
        )
        transitions = [CommitteeTransition(*row) for row in cur.fetchall()]
    return congress_end, ranges, transitions


def _source_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_id FROM source
            WHERE source_type='YAML' AND source_name=%s
            ORDER BY source_id LIMIT 1
            """,
            (SOURCE_NAME,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """
            INSERT INTO source (source_type, source_name, version_tag)
            VALUES ('YAML', %s, 'member_service_terminal_range_v1')
            RETURNING source_id
            """,
            (SOURCE_NAME,),
        )
        return cur.fetchone()[0]


def _source_document_id(conn, source_id: int, service: ServiceRange) -> int:
    payload = evidence_payload(service)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    external_id = f"member-service-{service_evidence_key(service)}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_document (
                source_id, external_id, doc_date, raw_json, content_hash
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (content_hash) WHERE content_hash IS NOT NULL
            DO UPDATE SET content_hash=EXCLUDED.content_hash
            RETURNING source_document_id
            """,
            (source_id, external_id, service.end_date, Json(payload), content_hash),
        )
        return cur.fetchone()[0]


def _existing_derived_event_ids(conn, congress_no: int) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id
            FROM committee_event
            WHERE congress_no = %s AND extraction_mode = %s
            """,
            (congress_no, EXTRACTION_MODE),
        )
        return {row[0] for row in cur.fetchall()}


def _delete_stale_derived_events(conn, congress_no: int, stale_ids: set[str]) -> int:
    if not stale_ids:
        return 0
    ids = sorted(stale_ids)
    with conn.cursor() as cur:
        # A downstream membership rebuild restores the affected intervals from
        # the reconciled ledger. Remove dependent rows so stale evidence cannot
        # block replacement.
        cur.execute(
            """
            DELETE FROM committee_membership_role
            WHERE committee_membership_id IN (
                SELECT committee_membership_id
                FROM committee_membership
                WHERE congress_no = %s
                  AND (start_event_id = ANY(%s) OR end_event_id = ANY(%s))
            )
            """,
            (congress_no, ids, ids),
        )
        cur.execute(
            """
            DELETE FROM committee_membership
            WHERE congress_no = %s
              AND (start_event_id = ANY(%s) OR end_event_id = ANY(%s))
            """,
            (congress_no, ids, ids),
        )
        cur.execute(
            """
            DELETE FROM committee_event
            WHERE congress_no = %s
              AND extraction_mode = %s
              AND event_id = ANY(%s)
            """,
            (congress_no, EXTRACTION_MODE, ids),
        )
        return cur.rowcount


def load_member_service_exit_events(
    congress_no: int,
    *,
    allow_write: bool = False,
) -> tuple[int, int]:
    """Return ``(candidate_count, inserted_count)`` for one Congress."""
    conn = get_connection()
    try:
        congress_end, ranges, transitions = _load_inputs(conn, congress_no)
        candidates = service_exit_candidates(ranges, transitions, congress_end)
        if not allow_write:
            conn.rollback()
            return len(candidates), 0

        source_id = _source_id(conn)
        documents: dict[str, int] = {}
        desired_ids = {derived_event_id(candidate) for candidate in candidates}
        existing_ids = _existing_derived_event_ids(conn, congress_no)
        stale_ids, _missing_ids = reconcile_derived_event_ids(
            existing_ids, desired_ids
        )
        deleted = _delete_stale_derived_events(conn, congress_no, stale_ids)
        inserted = 0
        with conn.cursor() as cur:
            for candidate in candidates:
                service = candidate.service
                evidence_key = service_evidence_key(service)
                source_document_id = documents.get(evidence_key)
                if source_document_id is None:
                    source_document_id = _source_document_id(conn, source_id, service)
                    documents[evidence_key] = source_document_id
                exit_date = candidate.exit_date.isoformat()
                source_locator = f"member_service[{evidence_key}]#upper(valid_daterange)"
                event_id = derived_event_id(candidate)
                text_span = (
                    f"{service.bioguide_id} {service.chamber} service ended "
                    f"{exit_date} before the end of Congress {service.congress_no}."
                )
                cur.execute(
                    """
                    INSERT INTO committee_event (
                        event_id, congress_no, chamber, bioguide_id, committee_code,
                        action, decision_date, effective_date, source_document_id,
                        source_locator, text_span, extraction_mode
                    ) VALUES (%s,%s,%s,%s,%s,'REMOVED',%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        event_id,
                        service.congress_no,
                        service.chamber,
                        service.bioguide_id,
                        candidate.committee_code,
                        exit_date,
                        exit_date,
                        source_document_id,
                        source_locator,
                        text_span,
                        EXTRACTION_MODE,
                    ),
                )
                inserted += int(cur.fetchone() is not None)
        conn.commit()
        print(f"Stale derived events removed: {deleted}")
        return len(candidates), inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Derive committee removals from early member-service exits."
    )
    parser.add_argument("--congress", "-c", type=int, required=True)
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Write events; otherwise report candidates only.",
    )
    args = parser.parse_args()
    candidates, inserted = load_member_service_exit_events(
        args.congress,
        allow_write=args.allow_write,
    )
    mode = "write" if args.allow_write else "dry-run"
    print(
        f"MEMBER SERVICE EXIT INGESTION ({mode}): "
        f"congress={args.congress} candidates={candidates} inserted={inserted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
