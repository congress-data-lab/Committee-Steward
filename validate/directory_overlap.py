"""Tuple-level validation against Congressional Directory roster snapshots."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from core.committees.resolver import (
    CommitteeResolutionError,
    build_committee_index,
    committee_name_to_id,
    resolve_from_index,
)
from core.members.resolver import MemberResolutionError, MemberResolver


@dataclass(frozen=True, order=True)
class SnapshotAssignment:
    bioguide_id: str
    committee_code: str


@dataclass(frozen=True)
class DirectoryScore:
    congress_no: int
    snapshot_date: date
    chamber: str
    committee_scope: str
    directory_member_entries: int
    resolved_directory_assignments: int
    unresolved_member_entries: int
    unmapped_committee_entries: int
    unmapped_committees: int
    observed_assignments: int
    overlap_assignments: int
    directory_only_assignments: int
    observed_only_assignments: int
    member_resolution_pct: float | None
    directory_coverage_pct: float | None
    observed_overlap_pct: float | None
    gate_status: str

    def as_row(self) -> dict:
        row = asdict(self)
        row["snapshot_date"] = self.snapshot_date.isoformat()
        return row


@dataclass(frozen=True)
class DirectoryMismatch:
    congress_no: int
    snapshot_date: date
    chamber: str
    committee_scope: str
    side: str
    raw_member_name: str
    bioguide_id: str
    committee_text: str
    committee_code: str
    detail: str

    def as_row(self) -> dict:
        row = asdict(self)
        row["snapshot_date"] = self.snapshot_date.isoformat()
        return row


@dataclass
class _ReferenceSnapshot:
    assignments: set[SnapshotAssignment]
    names: dict[SnapshotAssignment, str]
    committee_names: dict[str, str]
    committee_codes: set[str]
    directory_member_entries: int = 0
    unresolved_member_entries: int = 0
    unmapped_committee_entries: int = 0
    unmapped_committees: int = 0


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round((numerator / denominator) * 100, 3)


def parse_directory_member_label(raw_name: str) -> tuple[str, str | None] | None:
    """Return a resolver-ready member name and optional state qualifier."""
    value = re.sub(r"\s+", " ", raw_name).strip()
    if not value or re.fullmatch(
        r"[\[({]?\s*vacan(?:t|cy)\.?\s*[\])}]?", value, re.IGNORECASE
    ) or re.search(r"\(ph\)|\b\d{3}[\s\-\u2013]\d{4}\b", value, re.IGNORECASE):
        return None

    state: str | None = None
    state_match = re.search(r",?\s+of\s+([A-Za-z ]+)\.?$", value, re.IGNORECASE)
    if state_match:
        state = state_match.group(1).strip()
        value = value[: state_match.start()].rstrip(" ,.")
    else:
        state_match = re.search(r"\(([A-Z]{2})(?:-[A-Z0-9]+)?\)?", value)
        if state_match:
            state = state_match.group(1)
            value = (value[: state_match.start()] + value[state_match.end() :]).strip()
        else:
            state_match = re.search(r"\s([A-Z]{2})\)$", value)
            if state_match:
                state = state_match.group(1)
                value = value[: state_match.start()].strip()

    value = re.sub(
        r"\s+(?:CHAIR(?:MAN|WOMAN)?|RANKING MEMBER|VICE CHAIR(?:MAN|WOMAN)?|EX OFFICIO)\.?$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" ,.")
    value = re.sub(r"\s+T\d+$", "", value).strip()
    return (value, state) if value else None


def resolve_directory_committee(
    committee_text: str,
    congress_no: int,
    chamber: str,
    committee_index,
) -> str:
    """Resolve directory headings through the shared index and generic fallback."""
    chamber_name = "house" if chamber == "H" else "senate"
    bill_type = "hres" if chamber == "H" else "sres"
    stripped = re.sub(r"^(?:House|Senate)\s+", "", committee_text, flags=re.IGNORECASE)
    candidates = list(dict.fromkeys((committee_text, stripped)))

    for candidate in candidates:
        try:
            return resolve_from_index(candidate, congress_no, committee_index, chamber_name)
        except CommitteeResolutionError:
            pass
    for candidate in candidates:
        committee_code = committee_name_to_id(candidate, bill_type)
        if committee_code:
            return committee_code
    raise CommitteeResolutionError(f"no directory committee match: {committee_text}")


def _committee_scope(raw_chamber: str, committee_text: str, committee_code: str = "") -> str:
    if raw_chamber.lower() == "joint" or committee_code.startswith("J"):
        return "joint"
    if (
        re.search(r"\b(?:select|special)\b", committee_text, re.IGNORECASE)
        or committee_code in {"HLIG", "HSZS", "SLIN", "SLET", "SPAG"}
    ):
        return "select_special"
    return "standing"


def _resolve_directory_member(
    resolver: MemberResolver,
    raw_name: str,
    congress_no: int,
    chamber: str,
    *,
    party: str,
    state: str | None,
) -> str:
    candidate_chambers = ("H", "S") if chamber == "J" else (chamber,)
    resolved: set[str] = set()
    errors: list[str] = []
    for candidate_chamber in candidate_chambers:
        try:
            resolved.add(
                resolver.resolve(
                    raw_name,
                    congress_no,
                    candidate_chamber,
                    party=party,
                    state=state,
                )
            )
        except MemberResolutionError as exc:
            errors.append(str(exc))
    if len(resolved) == 1:
        return next(iter(resolved))
    if len(resolved) > 1:
        raise MemberResolutionError(
            f"Ambiguous joint-committee member: {raw_name} ({sorted(resolved)})"
        )
    raise MemberResolutionError("; ".join(errors))


def score_snapshot(
    reference: set[SnapshotAssignment],
    observed: set[SnapshotAssignment],
    *,
    congress_no: int,
    snapshot_date: date,
    chamber: str,
    committee_scope: str,
    directory_member_entries: int,
    unresolved_member_entries: int,
    unmapped_committee_entries: int,
    unmapped_committees: int,
    minimum_directory_coverage: float,
    minimum_member_resolution: float,
) -> DirectoryScore:
    overlap = reference & observed
    resolution_denominator = directory_member_entries - unmapped_committee_entries
    resolution_pct = _percent(len(reference), resolution_denominator)
    coverage_pct = _percent(len(overlap), directory_member_entries)
    resolution_ratio = 1.0 if resolution_denominator == 0 else len(reference) / resolution_denominator
    coverage_ratio = 1.0 if directory_member_entries == 0 else len(overlap) / directory_member_entries
    gate_status = (
        "NO_REFERENCE"
        if directory_member_entries == 0
        else "PASS"
        if coverage_ratio >= minimum_directory_coverage
        and resolution_ratio >= minimum_member_resolution
        and unmapped_committee_entries == 0
        else "FAIL"
    )
    return DirectoryScore(
        congress_no=congress_no,
        snapshot_date=snapshot_date,
        chamber=chamber,
        committee_scope=committee_scope,
        directory_member_entries=directory_member_entries,
        resolved_directory_assignments=len(reference),
        unresolved_member_entries=unresolved_member_entries,
        unmapped_committee_entries=unmapped_committee_entries,
        unmapped_committees=unmapped_committees,
        observed_assignments=len(observed),
        overlap_assignments=len(overlap),
        directory_only_assignments=len(reference - observed),
        observed_only_assignments=len(observed - reference),
        member_resolution_pct=resolution_pct,
        directory_coverage_pct=coverage_pct,
        observed_overlap_pct=_percent(len(overlap), len(observed)),
        gate_status=gate_status,
    )


def _load_directory_rows(conn, congresses: list[int]):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT congress_no, publication_date, chamber, committee_text,
                   normalized_committees
            FROM congressional_directory_entry
            WHERE congress_no = ANY(%s) AND publication_date IS NOT NULL
            ORDER BY 1,2,3,4
            """,
            (congresses,),
        )
        return cur.fetchall()


def _load_observed(
    conn,
    congress_no: int,
    snapshot_date: date,
    committee_codes: set[str],
) -> set[SnapshotAssignment]:
    if not committee_codes:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT bioguide_id, committee_code
            FROM committee_membership
            WHERE congress_no=%s AND committee_code = ANY(%s)
              AND %s::date <@ valid_daterange
            """,
            (congress_no, sorted(committee_codes), snapshot_date),
        )
        return {SnapshotAssignment(row[0], row[1]) for row in cur.fetchall()}


def score_database(
    conn,
    congresses: list[int],
    *,
    minimum_directory_coverage: float = 0.95,
    minimum_member_resolution: float = 0.99,
    committee_scopes: set[str] | None = None,
) -> tuple[list[DirectoryScore], list[DirectoryMismatch]]:
    allowed_scopes = committee_scopes if committee_scopes is not None else {"standing"}
    committee_index = build_committee_index(
        [
            Path("data/reference/committees-current.yaml"),
            Path("data/reference/committees-historical.yaml"),
        ]
    )
    member_resolver = MemberResolver(conn)
    snapshots: dict[tuple[int, date, str, str], _ReferenceSnapshot] = {}
    mismatches: list[DirectoryMismatch] = []

    for congress_no, snapshot_date, chamber, committee_text, normalized in _load_directory_rows(conn, congresses):
        raw_chamber = str(normalized.get("type") or normalized.get("chamber") or chamber)
        effective_chamber = "J" if raw_chamber.lower() == "joint" else chamber
        parties = (
            ("D", normalized.get("democrats") or []),
            ("R", normalized.get("republicans") or []),
        )
        labels = [(party, raw) for party, values in parties for raw in values if parse_directory_member_label(raw)]
        try:
            committee_code = resolve_directory_committee(
                committee_text,
                congress_no,
                "H" if effective_chamber == "J" else effective_chamber,
                committee_index,
            )
        except CommitteeResolutionError as exc:
            scope = _committee_scope(raw_chamber, committee_text)
            if scope not in allowed_scopes:
                continue
            key = (congress_no, snapshot_date, effective_chamber, scope)
            snapshot = snapshots.setdefault(key, _ReferenceSnapshot(set(), {}, {}, set()))
            snapshot.directory_member_entries += len(labels)
            snapshot.unmapped_committees += 1
            snapshot.unmapped_committee_entries += len(labels)
            for _, raw_name in labels:
                mismatches.append(
                    DirectoryMismatch(
                        congress_no, snapshot_date, effective_chamber, scope, "unmapped_committee",
                        raw_name, "", committee_text, "", str(exc),
                    )
                )
            continue

        scope = _committee_scope(raw_chamber, committee_text, committee_code)
        if scope not in allowed_scopes:
            continue
        key = (congress_no, snapshot_date, effective_chamber, scope)
        snapshot = snapshots.setdefault(key, _ReferenceSnapshot(set(), {}, {}, set()))
        snapshot.directory_member_entries += len(labels)
        snapshot.committee_codes.add(committee_code)
        snapshot.committee_names[committee_code] = committee_text
        for party, raw_name in labels:
            parsed = parse_directory_member_label(raw_name)
            if parsed is None:
                continue
            resolver_name, state = parsed
            try:
                bioguide_id = _resolve_directory_member(
                    member_resolver,
                    resolver_name,
                    congress_no,
                    effective_chamber,
                    party=party,
                    state=state,
                )
            except MemberResolutionError as exc:
                snapshot.unresolved_member_entries += 1
                mismatches.append(
                    DirectoryMismatch(
                        congress_no, snapshot_date, effective_chamber, scope, "unresolved_member",
                        raw_name, "", committee_text, committee_code, str(exc),
                    )
                )
                continue
            assignment = SnapshotAssignment(bioguide_id, committee_code)
            snapshot.assignments.add(assignment)
            snapshot.names[assignment] = raw_name

    scores: list[DirectoryScore] = []
    for (congress_no, snapshot_date, chamber, scope), reference in sorted(snapshots.items()):
        observed = _load_observed(conn, congress_no, snapshot_date, reference.committee_codes)
        score = score_snapshot(
            reference.assignments,
            observed,
            congress_no=congress_no,
            snapshot_date=snapshot_date,
            chamber=chamber,
            committee_scope=scope,
            directory_member_entries=reference.directory_member_entries,
            unresolved_member_entries=reference.unresolved_member_entries,
            unmapped_committee_entries=reference.unmapped_committee_entries,
            unmapped_committees=reference.unmapped_committees,
            minimum_directory_coverage=minimum_directory_coverage,
            minimum_member_resolution=minimum_member_resolution,
        )
        scores.append(score)
        for assignment in sorted(reference.assignments - observed):
            mismatches.append(
                DirectoryMismatch(
                    congress_no, snapshot_date, chamber, scope, "directory_only",
                    reference.names.get(assignment, ""), assignment.bioguide_id,
                    reference.committee_names.get(assignment.committee_code, ""),
                    assignment.committee_code, "",
                )
            )
        for assignment in sorted(observed - reference.assignments):
            mismatches.append(
                DirectoryMismatch(
                    congress_no, snapshot_date, chamber, scope, "observed_only", "",
                    assignment.bioguide_id,
                    reference.committee_names.get(assignment.committee_code, ""),
                    assignment.committee_code, "",
                )
            )
    return scores, mismatches
