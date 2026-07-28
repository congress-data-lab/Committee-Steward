"""Stable schemas for canonical release exports."""

from __future__ import annotations

from dataclasses import dataclass


SCHEMA_VERSION = "1.1.0"
RELEASE_METADATA_SHEET = "Release Metadata"
METADATA_FIELDS_EXCLUDED_FROM_SEMANTIC_HASH = frozenset(
    {
        "generated_at_utc",
        "workbook_semantic_sha256",
    }
)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    data_type: str
    description: str
    nullable: bool = False
    date_semantics: str = ""
    allowed_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatasetSpec:
    key: str
    sheet_name: str
    filename_stem: str | None
    columns: tuple[ColumnSpec, ...]


ASSIGNMENTS_SPEC = DatasetSpec(
    key="assignments",
    sheet_name="Assignments",
    filename_stem="committee_assignments",
    columns=(
        ColumnSpec("release_version", "string", "Release tag or version label."),
        ColumnSpec("schema_version", "string", "Stable export schema version."),
        ColumnSpec("congress_no", "integer", "Congress number."),
        ColumnSpec("chamber", "string", "Legislative chamber.", allowed_values=("H", "S")),
        ColumnSpec("bioguide_id", "string", "Bioguide identifier for the member."),
        ColumnSpec("committee_code", "string", "Canonical committee code."),
        ColumnSpec("committee_name", "string", "Committee name active at assignment start."),
        ColumnSpec(
            "start_date",
            "date",
            "Inclusive first active day of the membership interval.",
            date_semantics="Inclusive assignment start date.",
        ),
        ColumnSpec(
            "last_active_date",
            "date",
            "Inclusive last active day of the membership interval.",
            date_semantics=(
                "Inclusive last active day. Equals termination_effective_date minus one day "
                "for explicit removals, or Congress end boundary minus one day for natural endings."
            ),
        ),
        ColumnSpec(
            "termination_effective_date",
            "date",
            "Exclusive interval boundary date when the assignment stops being active.",
            date_semantics=(
                "Removal effective date for explicit removals. For natural Congress endings, "
                "the Congress end boundary date."
            ),
        ),
        ColumnSpec("ended_early", "boolean", "True when the interval ended before the Congress boundary."),
        ColumnSpec(
            "start_release_event_id",
            "string",
            "Canonical release event identifier for the appointment event.",
            nullable=True,
        ),
        ColumnSpec(
            "end_release_event_id",
            "string",
            "Canonical release event identifier for the removal event.",
            nullable=True,
        ),
        ColumnSpec(
            "internal_start_event_id",
            "string",
            "Noncanonical internal database event identifier for the appointment event.",
            nullable=True,
        ),
        ColumnSpec(
            "internal_end_event_id",
            "string",
            "Noncanonical internal database event identifier for the removal event.",
            nullable=True,
        ),
    ),
)

RANKINGS_SPEC = DatasetSpec(
    key="rankings",
    sheet_name="Rankings",
    filename_stem="committee_rankings",
    columns=(
        ColumnSpec("release_version", "string", "Release tag or version label."),
        ColumnSpec("schema_version", "string", "Stable export schema version."),
        ColumnSpec("congress_no", "integer", "Congress number."),
        ColumnSpec("chamber", "string", "Legislative chamber.", allowed_values=("H", "S")),
        ColumnSpec("bioguide_id", "string", "Bioguide identifier for the ranked member."),
        ColumnSpec("committee_code", "string", "Canonical committee code."),
        ColumnSpec("committee_name", "string", "Committee name active at rank start."),
        ColumnSpec(
            "caucus_party_code",
            "integer",
            "Party or caucus code used for committee seniority.",
        ),
        ColumnSpec("rank_in_party", "integer", "One-based rank within the active party roster."),
        ColumnSpec(
            "unresolved_slots_before",
            "integer",
            "Unresolved source-list slots retained ahead of this member.",
        ),
        ColumnSpec(
            "rank_start_date",
            "date",
            "Inclusive first day of this rank interval.",
            date_semantics="Inclusive rank start date.",
        ),
        ColumnSpec(
            "rank_last_active_date",
            "date",
            "Inclusive last day of this rank interval.",
            date_semantics="Inclusive rank end boundary minus one day.",
        ),
        ColumnSpec(
            "rank_end_boundary",
            "date",
            "Exclusive end boundary of this rank interval.",
            date_semantics="Exclusive rank interval boundary.",
        ),
        ColumnSpec(
            "rank_basis",
            "string",
            "Evidence rule supporting the rank.",
            allowed_values=("resolution_order", "relative_instruction"),
        ),
        ColumnSpec("rank_observation_id", "string", "Stable ordered-source observation identifier."),
        ColumnSpec(
            "release_source_document_id",
            "string",
            "Canonical identifier for the resolution containing the rank evidence.",
        ),
        ColumnSpec("source_locator", "string", "Ordered appointment block within the resolution."),
        ColumnSpec("raw_member_name", "string", "Member label printed in the source resolution."),
        ColumnSpec(
            "rank_after_raw_name",
            "string",
            "Printed predecessor for an explicit relative-rank instruction.",
            nullable=True,
        ),
        ColumnSpec(
            "observation_kind",
            "string",
            "Kind of ordered source evidence.",
            allowed_values=("ORDERED_LIST", "FULL_ROSTER", "RELATIVE_ORDER"),
        ),
    ),
)

EVENTS_SPEC = DatasetSpec(
    key="events",
    sheet_name="Events",
    filename_stem="committee_events",
    columns=(
        ColumnSpec("release_version", "string", "Release tag or version label."),
        ColumnSpec("schema_version", "string", "Stable export schema version."),
        ColumnSpec(
            "release_event_id",
            "string",
            "Canonical event identifier derived from logical event fields plus stable evidence identity.",
        ),
        ColumnSpec(
            "internal_event_id",
            "string",
            "Noncanonical internal database event identifier.",
        ),
        ColumnSpec("congress_no", "integer", "Congress number."),
        ColumnSpec("chamber", "string", "Legislative chamber.", allowed_values=("H", "S")),
        ColumnSpec("bioguide_id", "string", "Bioguide identifier for the member."),
        ColumnSpec("committee_code", "string", "Canonical committee code."),
        ColumnSpec("committee_name", "string", "Committee name active on the event effective date."),
        ColumnSpec("action", "string", "Event action.", allowed_values=("APPOINTED", "REMOVED")),
        ColumnSpec(
            "decision_date",
            "date",
            "Decision date recorded on the event row.",
            date_semantics="Logical date used in the canonical release_event_id.",
        ),
        ColumnSpec(
            "effective_date",
            "date",
            "Effective date recorded on the event row.",
            date_semantics="Membership interval boundaries use effective_date, not decision_date.",
        ),
        ColumnSpec(
            "release_source_document_id",
            "string",
            "Canonical source-document identifier derived from stable source-document fields.",
        ),
        ColumnSpec(
            "internal_source_document_id",
            "integer",
            "Noncanonical internal database source-document identifier.",
        ),
        ColumnSpec("source_locator", "string", "Source locator or page span within the source document."),
        ColumnSpec("text_span", "string", "Extracted evidence text span."),
        ColumnSpec("extraction_mode", "string", "Extraction mode recorded by the parser."),
        ColumnSpec("note_types", "string", "Sorted semicolon-delimited event note types.", nullable=True),
        ColumnSpec(
            "interpretation_basis",
            "string",
            "Sorted delimiter-joined interpretation basis text.",
            nullable=True,
        ),
    ),
)

MEMBERS_SPEC = DatasetSpec(
    key="members",
    sheet_name="Members",
    filename_stem="committee_members",
    columns=(
        ColumnSpec("release_version", "string", "Release tag or version label."),
        ColumnSpec("schema_version", "string", "Stable export schema version."),
        ColumnSpec("bioguide_id", "string", "Bioguide identifier."),
        ColumnSpec("congress_no", "integer", "Congress number."),
        ColumnSpec("chamber", "string", "Legislative chamber.", allowed_values=("H", "S")),
        ColumnSpec(
            "service_start",
            "date",
            "Inclusive start date of the service interval used by the release.",
            date_semantics="Inclusive service interval lower bound.",
        ),
        ColumnSpec(
            "service_last_active_date",
            "date",
            "Inclusive last active day of the service interval used by the release.",
            date_semantics="Inclusive service interval upper bound minus one day.",
        ),
        ColumnSpec("first_name", "string", "Member first name.", nullable=True),
        ColumnSpec("last_name", "string", "Member last name.", nullable=True),
        ColumnSpec("official_full_name", "string", "Official full name.", nullable=True),
        ColumnSpec("nickname", "string", "Nickname or preferred name.", nullable=True),
        ColumnSpec("state", "string", "Two-letter state or jurisdiction code."),
        ColumnSpec("district", "integer", "District number when applicable.", nullable=True),
        ColumnSpec("party_code", "integer", "Party code when available.", nullable=True),
        ColumnSpec(
            "caucus_party_code",
            "integer",
            "Party or caucus code used for committee seniority.",
            nullable=True,
        ),
        ColumnSpec("exit_reason", "string", "Recorded service exit reason.", nullable=True),
    ),
)

COMMITTEES_SPEC = DatasetSpec(
    key="committees",
    sheet_name="Committees",
    filename_stem="committee_committees",
    columns=(
        ColumnSpec("release_version", "string", "Release tag or version label."),
        ColumnSpec("schema_version", "string", "Stable export schema version."),
        ColumnSpec("committee_code", "string", "Canonical committee code."),
        ColumnSpec("chamber", "string", "Legislative chamber.", allowed_values=("H", "S")),
        ColumnSpec("committee_name", "string", "Historical committee name for this validity interval."),
        ColumnSpec(
            "valid_start",
            "date",
            "Inclusive start of the committee-name validity interval.",
            date_semantics="Inclusive validity start date for the committee name row.",
        ),
        ColumnSpec(
            "valid_last_active_date",
            "date",
            "Inclusive last active day of the committee-name validity interval.",
            nullable=True,
            date_semantics="Inclusive validity end date when known; blank for open-ended name intervals.",
        ),
        ColumnSpec("is_joint", "boolean", "Whether the underlying committee row is joint."),
    ),
)

SOURCES_SPEC = DatasetSpec(
    key="sources",
    sheet_name="Sources",
    filename_stem="committee_sources",
    columns=(
        ColumnSpec("release_version", "string", "Release tag or version label."),
        ColumnSpec("schema_version", "string", "Stable export schema version."),
        ColumnSpec(
            "release_source_id",
            "string",
            "Canonical source identifier derived from stable source fields.",
        ),
        ColumnSpec(
            "internal_source_id",
            "integer",
            "Noncanonical internal database source identifier.",
            nullable=True,
        ),
        ColumnSpec(
            "release_source_document_id",
            "string",
            "Canonical source-document identifier derived from stable source-document fields.",
        ),
        ColumnSpec(
            "internal_source_document_id",
            "integer",
            "Noncanonical internal database source-document identifier.",
        ),
        ColumnSpec("source_type", "string", "Source type recorded in `source`."),
        ColumnSpec("source_name", "string", "Source name recorded in `source`."),
        ColumnSpec("version_tag", "string", "Version tag recorded in `source`.", nullable=True),
        ColumnSpec("external_id", "string", "External source identifier.", nullable=True),
        ColumnSpec(
            "doc_date",
            "date",
            "Document date for the source document.",
            nullable=True,
            date_semantics="Date recorded on the source document row.",
        ),
        ColumnSpec("url", "string", "Source URL.", nullable=True),
        ColumnSpec("content_hash", "string", "Content SHA or equivalent document hash.", nullable=True),
        ColumnSpec(
            "retrieved_at_utc",
            "string",
            "Runtime retrieval timestamp; omitted from deterministic release exports.",
            nullable=True,
        ),
        ColumnSpec(
            "created_at_utc",
            "string",
            "Runtime database creation timestamp; omitted from deterministic release exports.",
            nullable=True,
        ),
    ),
)

VALIDATION_SPEC = DatasetSpec(
    key="validation",
    sheet_name="Validation",
    filename_stem="validation_summary",
    columns=(
        ColumnSpec("release_version", "string", "Release tag or version label."),
        ColumnSpec("schema_version", "string", "Stable export schema version."),
        ColumnSpec("validation_policy_version", "string", "Version tag for validation policy semantics."),
        ColumnSpec(
            "validation_type",
            "string",
            "Validation source family.",
            allowed_values=("directory_overlap",),
        ),
        ColumnSpec("congress_no", "integer", "Congress number."),
        ColumnSpec("chamber", "string", "Legislative chamber.", allowed_values=("H", "S")),
        ColumnSpec(
            "snapshot_date",
            "date",
            "Snapshot date for the Directory validation row.",
        ),
        ColumnSpec("committee_scope", "string", "Committee scope for the Directory row."),
        ColumnSpec("gate_status", "string", "Pass/fail gate status."),
        ColumnSpec("reference_count", "integer", "Reference row count used for comparison.", nullable=True),
        ColumnSpec("observed_count", "integer", "Observed row count used for comparison.", nullable=True),
        ColumnSpec("overlap_count", "integer", "Overlap row count used for comparison.", nullable=True),
        ColumnSpec("reference_only_count", "integer", "Reference-only row count.", nullable=True),
        ColumnSpec("observed_only_count", "integer", "Observed-only row count.", nullable=True),
        ColumnSpec("directory_member_entries", "integer", "Directory member-entry denominator.", nullable=True),
        ColumnSpec(
            "resolved_directory_assignments",
            "integer",
            "Resolved directory assignments used as reference tuples.",
            nullable=True,
        ),
        ColumnSpec("unresolved_member_entries", "integer", "Unresolved Directory member rows.", nullable=True),
        ColumnSpec("unmapped_committee_entries", "integer", "Directory rows with unresolved committees.", nullable=True),
        ColumnSpec("unmapped_committees", "integer", "Distinct unmapped committees.", nullable=True),
        ColumnSpec("member_resolution_pct", "float", "Directory member-resolution percentage.", nullable=True),
        ColumnSpec("directory_coverage_pct", "float", "Directory overlap percentage.", nullable=True),
        ColumnSpec("observed_overlap_pct", "float", "Observed overlap percentage.", nullable=True),
    ),
)

DIRECTORY_MISMATCHES_SPEC = DatasetSpec(
    key="directory_mismatches",
    sheet_name="CSV only: Directory mismatches",
    filename_stem="directory_mismatches",
    columns=(
        ColumnSpec("release_version", "string", "Release tag or version label."),
        ColumnSpec("schema_version", "string", "Stable export schema version."),
        ColumnSpec("congress_no", "integer", "Congress number."),
        ColumnSpec("snapshot_date", "date", "Congressional Directory snapshot date."),
        ColumnSpec("chamber", "string", "Legislative chamber.", allowed_values=("H", "S")),
        ColumnSpec("committee_scope", "string", "Committee scope; production exports standing only."),
        ColumnSpec("side", "string", "Mismatch or resolution-failure category."),
        ColumnSpec("raw_member_name", "string", "Member label printed in the Directory.", nullable=True),
        ColumnSpec("bioguide_id", "string", "Resolved Bioguide identifier.", nullable=True),
        ColumnSpec("committee_text", "string", "Committee heading printed in the Directory.", nullable=True),
        ColumnSpec("committee_code", "string", "Resolved canonical committee code.", nullable=True),
        ColumnSpec("detail", "string", "Resolution error or other mismatch detail.", nullable=True),
    ),
)

DATA_DICTIONARY_SPEC = DatasetSpec(
    key="data_dictionary",
    sheet_name="Data Dictionary",
    filename_stem=None,
    columns=(
        ColumnSpec("sheet_name", "string", "Workbook sheet name."),
        ColumnSpec("column_name", "string", "Column name within the sheet."),
        ColumnSpec("csv_file", "string", "CSV filename for the sheet when one exists.", nullable=True),
        ColumnSpec("data_type", "string", "Logical data type."),
        ColumnSpec("nullable", "boolean", "Whether the column allows blank values."),
        ColumnSpec("description", "string", "Plain-language column definition."),
        ColumnSpec("null_rule", "string", "Explanation for when values are blank."),
        ColumnSpec("date_semantics", "string", "Date-boundary semantics for date columns.", nullable=True),
        ColumnSpec("allowed_values", "string", "Delimited allowed values when enumerated.", nullable=True),
    ),
)

RELEASE_METADATA_SPEC = DatasetSpec(
    key="release_metadata",
    sheet_name=RELEASE_METADATA_SHEET,
    filename_stem=None,
    columns=(
        ColumnSpec("field", "string", "Metadata field name."),
        ColumnSpec("value", "string", "Metadata field value."),
    ),
)

DATASET_SPECS = {
    spec.key: spec
    for spec in (
        ASSIGNMENTS_SPEC,
        RANKINGS_SPEC,
        EVENTS_SPEC,
        MEMBERS_SPEC,
        COMMITTEES_SPEC,
        SOURCES_SPEC,
        VALIDATION_SPEC,
        DIRECTORY_MISMATCHES_SPEC,
        DATA_DICTIONARY_SPEC,
        RELEASE_METADATA_SPEC,
    )
}

WORKBOOK_SHEET_ORDER = tuple(
    DATASET_SPECS[key].sheet_name
    for key in (
        "assignments",
        "rankings",
        "events",
        "members",
        "committees",
        "sources",
        "validation",
        "data_dictionary",
        "release_metadata",
    )
)
