"""Derive dated, party-specific committee ranks from ordered source evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from itertools import groupby


@dataclass(frozen=True)
class MembershipRecord:
    committee_membership_id: int
    bioguide_id: str
    congress_no: int
    chamber: str
    committee_code: str
    start_date: date
    end_date: date


@dataclass(frozen=True)
class PartyPeriod:
    bioguide_id: str
    congress_no: int
    chamber: str
    start_date: date
    end_date: date
    caucus_party_code: int


@dataclass(frozen=True)
class RankObservation:
    rank_observation_id: str
    congress_no: int
    chamber: str
    committee_code: str
    decision_date: date
    resolution_number: int | None
    source_block_ordinal: int
    source_member_ordinal: int
    raw_member_name: str
    bioguide_id: str | None
    caucus_party_code: int | None
    rank_after_bioguide_id: str | None
    rank_after_raw_name: str | None
    observation_kind: str


@dataclass(frozen=True)
class RankInterval:
    committee_membership_id: int
    bioguide_id: str
    congress_no: int
    chamber: str
    committee_code: str
    caucus_party_code: int
    rank_in_party: int
    unresolved_slots_before: int
    start_date: date
    end_date: date
    rank_basis: str
    source_rank_observation_id: str


@dataclass(frozen=True)
class _OrderEntry:
    key: str
    bioguide_id: str | None
    raw_member_name: str
    caucus_party_code: int
    rank_basis: str
    source_rank_observation_id: str


def _scope_key(item: MembershipRecord | RankObservation) -> tuple[int, str, str]:
    return item.congress_no, item.chamber, item.committee_code


def _raw_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _party_on_date(
    periods: list[PartyPeriod], bioguide_id: str, boundary: date
) -> int | None:
    for period in periods:
        if (
            period.bioguide_id == bioguide_id
            and period.start_date <= boundary < period.end_date
        ):
            return period.caucus_party_code
    return None


def _apply_observation_block(
    order_by_party: dict[int, list[_OrderEntry]],
    observations: list[RankObservation],
) -> None:
    full_roster_parties = {
        observation.caucus_party_code
        for observation in observations
        if observation.observation_kind == "FULL_ROSTER"
        and observation.caucus_party_code is not None
    }
    for party_code in full_roster_parties:
        order_by_party[party_code] = []

    for observation in observations:
        party_code = observation.caucus_party_code
        if party_code is None:
            continue
        if observation.bioguide_id:
            for existing_party, entries in order_by_party.items():
                order_by_party[existing_party] = [
                    entry
                    for entry in entries
                    if entry.bioguide_id != observation.bioguide_id
                ]
        sequence = order_by_party.setdefault(party_code, [])
        key = observation.bioguide_id or f"unresolved:{observation.rank_observation_id}"
        basis = (
            "relative_instruction"
            if observation.rank_after_bioguide_id or observation.rank_after_raw_name
            else "resolution_order"
        )
        entry = _OrderEntry(
            key,
            observation.bioguide_id,
            observation.raw_member_name,
            party_code,
            basis,
            observation.rank_observation_id,
        )
        anchor_index = None
        if observation.rank_after_bioguide_id:
            anchor_index = next(
                (
                    index
                    for index, existing in enumerate(sequence)
                    if existing.bioguide_id == observation.rank_after_bioguide_id
                ),
                None,
            )
        if anchor_index is None and observation.rank_after_raw_name:
            anchor_identity = _raw_identity(observation.rank_after_raw_name)
            anchor_index = next(
                (
                    index
                    for index, existing in enumerate(sequence)
                    if _raw_identity(existing.raw_member_name) == anchor_identity
                ),
                None,
            )
        if anchor_index is None:
            sequence.append(entry)
        else:
            sequence.insert(anchor_index + 1, entry)


def _merge_adjacent(rows: list[RankInterval]) -> list[RankInterval]:
    merged: list[RankInterval] = []
    for row in sorted(
        rows,
        key=lambda item: (
            item.committee_membership_id,
            item.start_date,
            item.end_date,
        ),
    ):
        if merged:
            prior = merged[-1]
            if (
                prior.committee_membership_id == row.committee_membership_id
                and prior.end_date == row.start_date
                and prior.caucus_party_code == row.caucus_party_code
                and prior.rank_in_party == row.rank_in_party
                and prior.unresolved_slots_before == row.unresolved_slots_before
                and prior.rank_basis == row.rank_basis
                and prior.source_rank_observation_id
                == row.source_rank_observation_id
            ):
                merged[-1] = replace(prior, end_date=row.end_date)
                continue
        merged.append(row)
    return merged


def derive_rank_intervals(
    memberships: list[MembershipRecord],
    party_periods: list[PartyPeriod],
    observations: list[RankObservation],
) -> list[RankInterval]:
    """Replay ordered evidence and return ranks for intervals with known ordering."""
    results: list[RankInterval] = []
    scope_keys = sorted({_scope_key(item) for item in memberships})
    for scope in scope_keys:
        scoped_memberships = [item for item in memberships if _scope_key(item) == scope]
        scoped_observations = [item for item in observations if _scope_key(item) == scope]
        scoped_parties = [
            period
            for period in party_periods
            if (period.congress_no, period.chamber) == scope[:2]
        ]
        boundaries = sorted(
            {
                boundary
                for membership in scoped_memberships
                for boundary in (membership.start_date, membership.end_date)
            }
            | {observation.decision_date for observation in scoped_observations}
            | {
                boundary
                for period in scoped_parties
                for boundary in (period.start_date, period.end_date)
            }
        )
        observations_by_date: dict[date, list[RankObservation]] = {}
        for observation in scoped_observations:
            observations_by_date.setdefault(observation.decision_date, []).append(observation)
        order_by_party: dict[int, list[_OrderEntry]] = {}

        for start_date, end_date in zip(boundaries, boundaries[1:]):
            daily = sorted(
                observations_by_date.get(start_date, []),
                key=lambda item: (
                    item.resolution_number if item.resolution_number is not None else 10**9,
                    item.source_block_ordinal,
                    item.source_member_ordinal,
                    item.rank_observation_id,
                ),
            )
            block_key = lambda item: (
                item.resolution_number,
                item.source_block_ordinal,
            )
            for _, block_iter in groupby(daily, key=block_key):
                _apply_observation_block(order_by_party, list(block_iter))

            active_memberships = {
                membership.bioguide_id: membership
                for membership in scoped_memberships
                if membership.start_date <= start_date < membership.end_date
            }
            for party_code, sequence in sorted(order_by_party.items()):
                rank = 0
                unresolved_slots = 0
                for entry in sequence:
                    if entry.bioguide_id is None:
                        rank += 1
                        unresolved_slots += 1
                        continue
                    membership = active_memberships.get(entry.bioguide_id)
                    if not membership:
                        continue
                    active_party = _party_on_date(
                        scoped_parties, entry.bioguide_id, start_date
                    )
                    if active_party != party_code:
                        continue
                    rank += 1
                    results.append(
                        RankInterval(
                            membership.committee_membership_id,
                            entry.bioguide_id,
                            membership.congress_no,
                            membership.chamber,
                            membership.committee_code,
                            party_code,
                            rank,
                            unresolved_slots,
                            start_date,
                            end_date,
                            entry.rank_basis,
                            entry.source_rank_observation_id,
                        )
                    )
    return _merge_adjacent(results)
