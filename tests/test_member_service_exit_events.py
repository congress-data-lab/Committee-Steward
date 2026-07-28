from datetime import date

from ingest.load_member_service_exit_events import (
    CommitteeTransition,
    ServiceRange,
    ServiceExitCandidate,
    derived_event_id,
    evidence_payload,
    reconcile_derived_event_ids,
    service_evidence_key,
    service_exit_candidates,
    terminal_service_exits,
)


def _service(
    service_id: int,
    member: str,
    start: str,
    end: str,
) -> ServiceRange:
    return ServiceRange(
        member_service_id=service_id,
        bioguide_id=member,
        chamber="H",
        congress_no=115,
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        parsed_source="YAML",
        parser_id="load_members.py",
        source_content_hash="a" * 64,
    )


def _transition(
    event_id: str,
    member: str,
    committee: str,
    action: str,
    when: str,
) -> CommitteeTransition:
    event_date = date.fromisoformat(when)
    return CommitteeTransition(
        event_id=event_id,
        bioguide_id=member,
        chamber="H",
        committee_code=committee,
        action=action,
        decision_date=event_date,
        effective_date=event_date,
    )


def test_terminal_service_exits_ignores_internal_range_changes():
    congress_end = date(2019, 1, 3)
    ranges = [
        _service(1, "CONTINUES", "2017-01-03", "2018-01-01"),
        _service(2, "CONTINUES", "2018-01-01", "2019-01-03"),
        _service(3, "EXITS", "2017-01-03", "2017-02-10"),
    ]

    exits = terminal_service_exits(ranges, congress_end)

    assert [service.bioguide_id for service in exits] == ["EXITS"]


def test_service_exit_candidates_only_close_active_committees():
    congress_end = date(2019, 1, 3)
    ranges = [_service(3, "EXITS", "2017-01-03", "2017-02-10")]
    transitions = [
        _transition("a", "EXITS", "HSBU", "APPOINTED", "2017-01-10"),
        _transition("b", "EXITS", "HSJU", "APPOINTED", "2017-01-13"),
        _transition("c", "EXITS", "HSJU", "REMOVED", "2017-02-01"),
        _transition("d", "EXITS", "HSWM", "APPOINTED", "2017-02-11"),
    ]

    candidates = service_exit_candidates(ranges, transitions, congress_end)

    assert [(row.service.bioguide_id, row.committee_code) for row in candidates] == [
        ("EXITS", "HSBU")
    ]


def test_service_exit_evidence_payload_is_serializable_and_explicit():
    payload = evidence_payload(
        _service(3, "EXITS", "2017-01-03", "2017-02-10")
    )

    assert payload["start_date"] == "2017-01-03"
    assert payload["end_date"] == "2017-02-10"
    assert payload["derivation"] == (
        "terminal member_service range ends before Congress"
    )
    assert "member_service_id" not in payload


def test_service_exit_evidence_identity_does_not_depend_on_database_row_id():
    first = _service(3, "EXITS", "2017-01-03", "2017-02-10")
    replayed = _service(999, "EXITS", "2017-01-03", "2017-02-10")

    assert evidence_payload(first) == evidence_payload(replayed)
    assert service_evidence_key(first) == service_evidence_key(replayed)
    assert derived_event_id(ServiceExitCandidate(first, "HSBU")) == derived_event_id(
        ServiceExitCandidate(replayed, "HSBU")
    )


def test_reconcile_derived_event_ids_replaces_stale_results():
    stale, missing = reconcile_derived_event_ids(
        {"unchanged", "old-date"}, {"unchanged", "new-date"}
    )

    assert stale == {"old-date"}
    assert missing == {"new-date"}
