from ingest.rank_observations import (
    RankObservationSlot,
    compute_rank_observation_id,
    infer_block_party,
    resolution_number,
)


def test_unresolved_slots_inherit_unambiguous_block_caucus() -> None:
    slots = infer_block_party(
        [
            RankObservationSlot("Mr. Known", 1, "K000001", 100),
            RankObservationSlot("Mr. Unresolved", 2, None, None),
        ]
    )

    assert [slot.caucus_party_code for slot in slots] == [100, 100]


def test_mixed_party_block_does_not_guess_unresolved_slot_party() -> None:
    slots = infer_block_party(
        [
            RankObservationSlot("Mr. One", 1, "O000001", 100),
            RankObservationSlot("Mr. Two", 2, "T000002", 200),
            RankObservationSlot("Mr. Unknown", 3, None, None),
        ]
    )

    assert slots[-1].caucus_party_code is None


def test_rank_observation_identity_is_stable_and_source_scoped() -> None:
    kwargs = {
        "congress_no": 118,
        "chamber": "H",
        "committee_code": "HSFA",
        "citation": "H. RES. 76",
        "source_locator": "BILLS-118hres76eh.xml#appointment[0]",
        "source_ordinal": 1,
        "raw_member_name": "Ms. Omar",
    }

    assert compute_rank_observation_id(**kwargs) == compute_rank_observation_id(**kwargs)
    assert resolution_number("H. RES. 76") == 76
