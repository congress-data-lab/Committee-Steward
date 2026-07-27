from datetime import date

from core.ranking import (
    MembershipRecord,
    PartyPeriod,
    RankObservation,
    derive_rank_intervals,
)


START = date(2023, 1, 3)
END = date(2025, 1, 3)


def _membership(identifier: int, member: str, *, end: date = END) -> MembershipRecord:
    return MembershipRecord(identifier, member, 118, "H", "HSFA", START, end)


def _party(member: str) -> PartyPeriod:
    return PartyPeriod(member, 118, "H", START, END, 200)


def _observation(
    identifier: str,
    ordinal: int,
    member: str | None,
    *,
    observed: date = START,
    raw: str | None = None,
    after: str | None = None,
) -> RankObservation:
    return RankObservation(
        identifier,
        118,
        "H",
        "HSFA",
        observed,
        7,
        1,
        ordinal,
        raw or member or "Unresolved Member",
        member,
        200,
        after,
        None,
        "RELATIVE_ORDER" if after else "ORDERED_LIST",
    )


def test_unresolved_source_slot_preserves_rank_gap() -> None:
    rows = derive_rank_intervals(
        [_membership(1, "A000001"), _membership(2, "C000003")],
        [_party("A000001"), _party("C000003")],
        [
            _observation("o1", 1, "A000001"),
            _observation("o2", 2, None, raw="Mr. Unresolved"),
            _observation("o3", 3, "C000003"),
        ],
    )

    by_member = {row.bioguide_id: row for row in rows}
    assert by_member["A000001"].rank_in_party == 1
    assert by_member["C000003"].rank_in_party == 3
    assert by_member["C000003"].unresolved_slots_before == 1


def test_departure_does_not_renumber_later_active_ranks() -> None:
    departure = date(2024, 3, 1)
    rows = derive_rank_intervals(
        [
            _membership(1, "A000001"),
            _membership(2, "B000002", end=departure),
            _membership(3, "C000003"),
        ],
        [_party("A000001"), _party("B000002"), _party("C000003")],
        [
            _observation("o1", 1, "A000001"),
            _observation("o2", 2, "B000002"),
            _observation("o3", 3, "C000003"),
        ],
    )

    c_rows = [row for row in rows if row.bioguide_id == "C000003"]
    assert [(row.start_date, row.end_date, row.rank_in_party) for row in c_rows] == [
        (START, END, 3),
    ]


def test_relative_instruction_inserts_after_named_predecessor() -> None:
    appointment = date(2023, 6, 1)
    rows = derive_rank_intervals(
        [
            _membership(1, "A000001"),
            _membership(2, "B000002"),
            MembershipRecord(3, "D000004", 118, "H", "HSFA", appointment, END),
        ],
        [_party("A000001"), _party("B000002"), _party("D000004")],
        [
            _observation("o1", 1, "A000001"),
            _observation("o2", 2, "B000002"),
            _observation(
                "o3", 1, "D000004", observed=appointment, after="A000001"
            ),
        ],
    )

    d_row = next(row for row in rows if row.bioguide_id == "D000004")
    assert d_row.rank_in_party == 2
    assert d_row.rank_basis == "relative_instruction"
    b_rows = [row for row in rows if row.bioguide_id == "B000002"]
    assert [(row.start_date, row.end_date, row.rank_in_party) for row in b_rows] == [
        (START, appointment, 2),
        (appointment, END, 3),
    ]


def test_full_roster_replaces_prior_party_order() -> None:
    reconstitution = date(2023, 7, 1)
    observations = [
        _observation("o1", 1, "A000001"),
        _observation("o2", 2, "B000002"),
        RankObservation(
            "o3",
            118,
            "H",
            "HSFA",
            reconstitution,
            20,
            1,
            1,
            "C000003",
            "C000003",
            200,
            None,
            None,
            "FULL_ROSTER",
        ),
        RankObservation(
            "o4",
            118,
            "H",
            "HSFA",
            reconstitution,
            20,
            1,
            2,
            "A000001",
            "A000001",
            200,
            None,
            None,
            "FULL_ROSTER",
        ),
    ]

    rows = derive_rank_intervals(
        [
            _membership(1, "A000001"),
            _membership(2, "B000002"),
            _membership(3, "C000003"),
        ],
        [_party("A000001"), _party("B000002"), _party("C000003")],
        observations,
    )

    after = {
        row.bioguide_id: row.rank_in_party
        for row in rows
        if row.start_date == reconstitution
    }
    assert after == {"C000003": 1, "A000001": 2}
