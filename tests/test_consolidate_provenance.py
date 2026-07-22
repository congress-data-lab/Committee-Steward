from datetime import date

import pytest

import scripts.consolidate_provenance as deduper
from scripts.consolidate_provenance import (
    DocumentRecord,
    ForeignKeyReference,
    SourceRecord,
    UpdateTrigger,
    UnsafeConsolidation,
    build_document_mapping,
    build_source_mapping,
    validate_foreign_keys,
    validate_update_triggers,
)


class _FakeCursor:
    def __init__(self) -> None:
        self.executions: list[object] = []
        self.rowcount = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: object, params: object = None) -> None:
        self.executions.append(query)
        query_text = query if isinstance(query, str) else repr(query)
        if "DELETE FROM source_document" in query_text:
            self.rowcount = 1
        elif "DELETE FROM source AS source_row" in query_text:
            self.rowcount = 1


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _CatalogCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.query = ""

    def execute(self, query: str) -> None:
        self.query = query

    def fetchall(self) -> list[tuple]:
        return self.rows


def _core_references() -> list[ForeignKeyReference]:
    return [
        ForeignKeyReference(
            "public",
            "source_document",
            "source_id",
            "source",
            "source_id",
            "sd_source_fk",
            1,
            1,
        ),
        ForeignKeyReference(
            "public",
            "committee_event",
            "source_document_id",
            "source_document",
            "source_document_id",
            "ce_doc_fk",
            1,
            1,
        ),
        ForeignKeyReference(
            "public",
            "member_service",
            "source_id",
            "source",
            "source_id",
            "ms_source_fk",
            1,
            1,
        ),
        ForeignKeyReference(
            "public",
            "member_service",
            "source_document_id",
            "source_document",
            "source_document_id",
            "ms_doc_fk",
            1,
            1,
        ),
    ]


def _patch_transaction_helpers(monkeypatch, references) -> None:
    monkeypatch.setattr(deduper, "discover_foreign_keys", lambda cursor: references)
    monkeypatch.setattr(
        deduper,
        "discover_update_triggers",
        lambda cursor: [
            UpdateTrigger(
                "public",
                "committee_event",
                "trg_committee_event_parsed",
                "O",
                "public",
                "trg_set_parsed_at",
            ),
            UpdateTrigger(
                "public",
                "member_service",
                "trg_member_service_parsed",
                "O",
                "public",
                "trg_set_parsed_at",
            ),
        ],
    )
    monkeypatch.setattr(
        deduper,
        "_load_sources",
        lambda cursor: [
            SourceRecord(1, "resolution", "H.Res. Congress XML", "congress_118"),
            SourceRecord(2, "resolution", "H.Res. Congress XML", "congress_118"),
        ],
    )
    monkeypatch.setattr(deduper, "_create_mapping_table", lambda *args: None)
    monkeypatch.setattr(
        deduper,
        "_load_documents",
        lambda cursor: [
            DocumentRecord(10, 1, "doc.xml", date(2023, 1, 1), None, None, None),
            DocumentRecord(11, 1, "doc.xml", date(2023, 1, 1), None, None, None),
        ],
    )
    monkeypatch.setattr(deduper, "_count_references", lambda *args: 1)


def test_source_mapping_uses_exact_identity_and_oldest_row() -> None:
    rows = [
        SourceRecord(8, "resolution", "H.Res. Congress XML", "congress_118"),
        SourceRecord(3, "resolution", "H.Res. Congress XML", "congress_118"),
        SourceRecord(4, "resolution", "H.Res. Congress XML", "congress_117"),
        SourceRecord(9, "resolution", "Another source", "congress_118"),
    ]

    mapping, group_count = build_source_mapping(rows)

    assert mapping == {8: 3}
    assert group_count == 1


def test_source_mapping_treats_null_version_as_an_exact_value() -> None:
    mapping, group_count = build_source_mapping(
        [
            SourceRecord(2, "CR", "Congressional Record JSON", None),
            SourceRecord(6, "CR", "Congressional Record JSON", None),
        ]
    )

    assert mapping == {6: 2}
    assert group_count == 1


def test_document_mapping_uses_canonical_source_and_legacy_identity() -> None:
    rows = [
        DocumentRecord(
            20, 3, "BILLS-118hres76eh.xml", date(2023, 2, 2), None, None, None
        ),
        DocumentRecord(
            11, 3, "BILLS-118hres76eh.xml", date(2023, 2, 2), None, None, None
        ),
        DocumentRecord(
            30, 4, "BILLS-118hres76eh.xml", date(2023, 2, 2), None, None, None
        ),
    ]

    mapping, group_count = build_document_mapping(rows)

    assert mapping == {20: 11}
    assert group_count == 1


def test_document_mapping_uses_content_hash_when_metadata_agrees() -> None:
    rows = [
        DocumentRecord(10, 3, "doc.xml", date(2023, 1, 1), "u", "raw", "abc"),
        DocumentRecord(12, 3, "doc.xml", date(2023, 1, 1), "u", "raw", "abc"),
    ]

    mapping, group_count = build_document_mapping(rows)

    assert mapping == {12: 10}
    assert group_count == 1


def test_document_mapping_refuses_conflicting_metadata_for_same_hash() -> None:
    rows = [
        DocumentRecord(10, 3, "doc-a.xml", date(2023, 1, 1), None, None, "abc"),
        DocumentRecord(12, 3, "doc-b.xml", date(2023, 1, 1), None, None, "abc"),
    ]

    with pytest.raises(UnsafeConsolidation, match="conflicting metadata"):
        build_document_mapping(rows)


def test_document_mapping_skips_rows_without_safe_legacy_identity() -> None:
    rows = [
        DocumentRecord(10, 3, None, None, None, None, None),
        DocumentRecord(12, 3, None, None, None, None, None),
        DocumentRecord(14, None, "doc.xml", None, None, None, None),
        DocumentRecord(16, None, "doc.xml", None, None, None, None),
    ]

    assert build_document_mapping(rows) == ({}, 0)


def test_foreign_key_validation_requires_core_provenance_references() -> None:
    references = [
        *_core_references(),
        ForeignKeyReference(
            "audit",
            "citation",
            "document_id",
            "source_document",
            "source_document_id",
            "citation_doc_fk",
            1,
            1,
        ),
    ]

    validate_foreign_keys(references)


def test_foreign_key_validation_refuses_composite_reference() -> None:
    references = [
        *_core_references(),
        ForeignKeyReference(
            "audit",
            "citation",
            "document_id",
            "source_document",
            "source_document_id",
            "citation_doc_fk",
            2,
            2,
        ),
    ]

    with pytest.raises(UnsafeConsolidation, match="composite"):
        validate_foreign_keys(references)


def test_trigger_validation_accepts_only_known_timestamp_triggers() -> None:
    triggers = [
        UpdateTrigger(
            "public",
            "committee_event",
            "trg_committee_event_parsed",
            "O",
            "public",
            "trg_set_parsed_at",
        ),
        UpdateTrigger(
            "public",
            "member_service",
            "disabled_custom_trigger",
            "D",
            "public",
            "custom_function",
        ),
    ]

    assert validate_update_triggers(triggers) == [triggers[0]]


def test_trigger_catalog_query_excludes_internal_and_non_update_triggers() -> None:
    row = (
        "public",
        "committee_event",
        "trg_committee_event_parsed",
        "O",
        "public",
        "trg_set_parsed_at",
    )
    cursor = _CatalogCursor([row])

    assert deduper.discover_update_triggers(cursor) == [UpdateTrigger(*row)]
    assert "NOT trigger_row.tgisinternal" in cursor.query
    assert "(trigger_row.tgtype & 16) <> 0" in cursor.query
    assert "target_table.relname IN ('source', 'source_document')" in cursor.query


def test_trigger_validation_refuses_unknown_enabled_update_trigger() -> None:
    trigger = UpdateTrigger(
        "audit",
        "citation",
        "citation_audit_trigger",
        "O",
        "audit",
        "record_citation_change",
    )

    with pytest.raises(UnsafeConsolidation, match="citation_audit_trigger"):
        validate_update_triggers([trigger])


def test_trigger_validation_refuses_known_name_with_wrong_function() -> None:
    trigger = UpdateTrigger(
        "public",
        "committee_event",
        "trg_committee_event_parsed",
        "O",
        "public",
        "unexpected_function",
    )

    with pytest.raises(UnsafeConsolidation, match="trg_committee_event_parsed"):
        validate_update_triggers([trigger])


def test_trigger_restore_preserves_replica_enablement_mode() -> None:
    cursor = _FakeCursor()
    trigger = UpdateTrigger(
        "public",
        "committee_event",
        "trg_committee_event_parsed",
        "R",
        "public",
        "trg_set_parsed_at",
    )

    deduper._set_triggers_enabled(cursor, [trigger], enabled=False)
    deduper._set_triggers_enabled(cursor, [trigger], enabled=True)

    assert "DISABLE" in repr(cursor.executions[0])
    assert "ENABLE REPLICA" in repr(cursor.executions[1])


def test_dry_run_rolls_back_without_locking_or_repointing(monkeypatch) -> None:
    connection = _FakeConnection()
    references = _core_references()
    _patch_transaction_helpers(monkeypatch, references)
    monkeypatch.setattr(
        deduper,
        "_lock_provenance_tables",
        lambda *args: (_ for _ in ()).throw(AssertionError("dry run must not lock")),
    )
    monkeypatch.setattr(
        deduper,
        "_repoint_reference",
        lambda *args: (_ for _ in ()).throw(AssertionError("dry run must not write")),
    )
    monkeypatch.setattr(
        deduper,
        "_set_triggers_enabled",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("dry run must not alter triggers")
        ),
    )
    messages: list[str] = []

    summary = deduper.consolidate_provenance(
        connection, allow_write=False, emit=messages.append
    )

    assert summary.wrote is False
    assert summary.sources_removed == 1
    assert summary.documents_removed == 1
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert any("trigger audit" in message for message in messages)
    assert any("handled" in message for message in messages)


def test_write_repoints_all_discovered_fks_before_commit(monkeypatch) -> None:
    connection = _FakeConnection()
    extra_reference = ForeignKeyReference(
        "audit",
        "citation",
        "document_id",
        "source_document",
        "source_document_id",
        "citation_doc_fk",
        1,
        1,
    )
    references = [*_core_references(), extra_reference]
    _patch_transaction_helpers(monkeypatch, references)
    locked: list[list[ForeignKeyReference]] = []
    repointed: list[str] = []
    verified: list[str] = []
    trigger_states: list[tuple[bool, list[str]]] = []
    timeline: list[str] = []
    monkeypatch.setattr(
        deduper,
        "_lock_provenance_tables",
        lambda cursor, refs: locked.append(list(refs)),
    )
    monkeypatch.setattr(
        deduper,
        "_repoint_reference",
        lambda cursor, reference: (
            timeline.append(f"repoint:{reference.label}"),
            repointed.append(reference.label),
            1,
        )[-1],
    )
    monkeypatch.setattr(
        deduper,
        "_assert_no_dropped_references",
        lambda cursor, reference: (
            timeline.append(f"verify:{reference.label}"),
            verified.append(reference.label),
        )[-1],
    )
    monkeypatch.setattr(
        deduper,
        "_set_triggers_enabled",
        lambda cursor, triggers, enabled: (
            timeline.append("triggers:enabled" if enabled else "triggers:disabled"),
            trigger_states.append(
                (enabled, [trigger.label for trigger in triggers])
            ),
        )[-1],
    )

    summary = deduper.consolidate_provenance(
        connection, allow_write=True, emit=lambda message: None
    )

    expected_labels = [reference.label for reference in references]
    assert locked == [references]
    assert repointed == expected_labels
    assert verified == expected_labels
    assert timeline.index("triggers:disabled") < next(
        index for index, item in enumerate(timeline) if item.startswith("repoint:")
    )
    assert timeline.index("triggers:enabled") < next(
        index for index, item in enumerate(timeline) if item.startswith("verify:")
    )
    assert trigger_states == [
        (
            False,
            [
                "public.committee_event.trg_committee_event_parsed",
                "public.member_service.trg_member_service_parsed",
            ],
        ),
        (
            True,
            [
                "public.committee_event.trg_committee_event_parsed",
                "public.member_service.trg_member_service_parsed",
            ],
        ),
    ]
    assert summary.wrote is True
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_write_failure_rolls_back_transaction(monkeypatch) -> None:
    connection = _FakeConnection()
    references = _core_references()
    _patch_transaction_helpers(monkeypatch, references)
    monkeypatch.setattr(deduper, "_lock_provenance_tables", lambda *args: None)
    monkeypatch.setattr(
        deduper,
        "_load_documents",
        lambda cursor: (_ for _ in ()).throw(
            UnsafeConsolidation("ambiguous document")
        ),
    )

    with pytest.raises(UnsafeConsolidation, match="ambiguous document"):
        deduper.consolidate_provenance(
            connection, allow_write=True, emit=lambda message: None
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_repoint_failure_rolls_back_after_triggers_are_disabled(monkeypatch) -> None:
    connection = _FakeConnection()
    references = _core_references()
    _patch_transaction_helpers(monkeypatch, references)
    timeline: list[str] = []
    monkeypatch.setattr(deduper, "_lock_provenance_tables", lambda *args: None)
    monkeypatch.setattr(
        deduper,
        "_set_triggers_enabled",
        lambda cursor, triggers, enabled: timeline.append(
            "triggers:enabled" if enabled else "triggers:disabled"
        ),
    )

    def fail_repoint(cursor, reference):
        timeline.append(f"repoint:{reference.label}")
        raise RuntimeError("injected repoint failure")

    monkeypatch.setattr(deduper, "_repoint_reference", fail_repoint)

    with pytest.raises(RuntimeError, match="injected repoint failure"):
        deduper.consolidate_provenance(
            connection, allow_write=True, emit=lambda message: None
        )

    assert timeline[0] == "triggers:disabled"
    assert timeline[1].startswith("repoint:")
    assert "triggers:enabled" not in timeline
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_unknown_trigger_is_reported_then_refused_before_lock(monkeypatch) -> None:
    connection = _FakeConnection()
    references = _core_references()
    _patch_transaction_helpers(monkeypatch, references)
    unsafe_trigger = UpdateTrigger(
        "audit",
        "citation",
        "citation_audit_trigger",
        "O",
        "audit",
        "record_citation_change",
    )
    monkeypatch.setattr(
        deduper, "discover_update_triggers", lambda cursor: [unsafe_trigger]
    )
    monkeypatch.setattr(
        deduper,
        "_lock_provenance_tables",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("unsafe trigger must be refused before locking")
        ),
    )
    messages: list[str] = []

    with pytest.raises(UnsafeConsolidation, match="citation_audit_trigger"):
        deduper.consolidate_provenance(
            connection, allow_write=True, emit=messages.append
        )

    assert any("citation_audit_trigger: UNSAFE" in message for message in messages)
    assert connection.commits == 0
    assert connection.rollbacks == 1
