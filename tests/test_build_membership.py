from datetime import date

from ingest.build_membership import _event_sort_key, _group_event_rows, _replay_events


def test_same_day_removal_wins_independently_of_event_id():
    day = date(2025, 1, 3)
    events = [
        ("000-remove", day, "REMOVED"),
        ("999-appoint", day, "APPOINTED"),
    ]

    assert _replay_events(sorted(events, key=_event_sort_key), date(2027, 1, 3)) == []


def test_grouping_uses_event_congress_instead_of_inferring_it_from_date():
    rows = [
        ("event-1", date(2025, 1, 3), "APPOINTED", "A000001", "HSAG", 118),
    ]

    groups = _group_event_rows(rows, {118, 119})

    assert list(groups) == [("A000001", "HSAG", 118)]
