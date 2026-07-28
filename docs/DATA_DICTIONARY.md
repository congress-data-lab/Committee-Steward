# Data Dictionary

This is the authoritative schema reference for Committee Steward export schema `1.1.0`. It covers the eight canonical UTF-8 CSV files released for Congresses 113 through 118. Replace `<range>` in the filenames below with the release range, such as `113_118`.

## Normalized model and joins

The release separates reconstructed intervals, evidence, identity, and validation rather than repeating all provenance on every assignment row:

- Join `committee_assignments.start_release_event_id` and `end_release_event_id` to `committee_events.release_event_id`.
- Join `committee_events.release_source_document_id` and `committee_rankings.release_source_document_id` to `committee_sources.release_source_document_id`.
- Join assignments or rankings to members on `congress_no`, `chamber`, and `bioguide_id`.
- Join assignments or rankings to committees on `committee_code`; use the committee validity dates when selecting a historical name.

Identifiers beginning with `release_` are canonical public identifiers derived from stable logical fields. Identifiers beginning with `internal_` expose diagnostic database keys for traceability within a particular reconstruction; consumers must not treat them as portable identifiers across rebuilds or releases.

All `*_last_active_date` fields are inclusive. All `*_end_boundary` fields and `termination_effective_date` are exclusive boundaries. Event `decision_date` records when the documented decision occurred; `effective_date` records when the membership change takes effect. Those dates can differ.

## Canonical CSV schemas

### `committee_assignments_<range>.csv`

One row per continuous committee-membership interval.

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `release_version` | string | Release tag or version label. |
| `schema_version` | string | Stable export schema version. |
| `congress_no` | integer | Congress number. |
| `chamber` | string | `H` for House or `S` for Senate. |
| `bioguide_id` | string | Bioguide identifier for the member. |
| `committee_code` | string | Canonical committee code. |
| `committee_name` | string | Committee name active at assignment start. |
| `start_date` | date | Inclusive first active day of the assignment. |
| `last_active_date` | date | Inclusive last active day of the assignment. |
| `termination_effective_date` | date | Exclusive boundary on which the assignment is no longer active. For natural endings, this is the Congress end boundary. |
| `ended_early` | boolean | `true` when the interval ended before the Congress boundary. |
| `start_release_event_id` | string, nullable | Canonical appointment-event identifier. |
| `end_release_event_id` | string, nullable | Canonical removal-event identifier. |
| `internal_start_event_id` | string, nullable | Noncanonical database identifier for the appointment event. |
| `internal_end_event_id` | string, nullable | Noncanonical database identifier for the removal event. |

### `committee_rankings_<range>.csv`

One row per dated within-party or within-caucus rank interval. Rank is not whole-committee rank.

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `release_version` | string | Release tag or version label. |
| `schema_version` | string | Stable export schema version. |
| `congress_no` | integer | Congress number. |
| `chamber` | string | `H` for House or `S` for Senate. |
| `bioguide_id` | string | Bioguide identifier for the ranked member. |
| `committee_code` | string | Canonical committee code. |
| `committee_name` | string | Committee name active at rank start. |
| `caucus_party_code` | integer | Party or caucus code used for committee ordering. |
| `rank_in_party` | integer | One-based rank among active members in that party or caucus. |
| `unresolved_slots_before` | integer | Unresolved source-list positions retained ahead of this member. |
| `rank_start_date` | date | Inclusive first day of the rank interval. |
| `rank_last_active_date` | date | Inclusive last day of the rank interval. |
| `rank_end_boundary` | date | Exclusive end boundary of the rank interval. |
| `rank_basis` | string | Evidence rule: `resolution_order` or `relative_instruction`. |
| `rank_observation_id` | string | Stable identifier for the ordered-source observation supporting the rank. |
| `release_source_document_id` | string | Canonical identifier for the resolution containing the rank evidence. |
| `source_locator` | string | Ordered appointment block or other locator within the resolution. |
| `raw_member_name` | string | Member label printed in the source resolution. |
| `rank_after_raw_name` | string, nullable | Printed predecessor in an explicit relative-rank instruction. |
| `observation_kind` | string | `ORDERED_LIST`, `FULL_ROSTER`, or `RELATIVE_ORDER`. |

An unresolved printed name retains its source-list slot. `unresolved_slots_before` therefore exposes uncertainty without silently promoting later resolved members.

### `committee_events_<range>.csv`

One row per source-supported appointment or removal event.

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `release_version` | string | Release tag or version label. |
| `schema_version` | string | Stable export schema version. |
| `release_event_id` | string | Canonical event identifier derived from logical event and evidence fields. |
| `internal_event_id` | string | Noncanonical database event identifier. |
| `congress_no` | integer | Congress number. |
| `chamber` | string | `H` for House or `S` for Senate. |
| `bioguide_id` | string | Bioguide identifier for the member. |
| `committee_code` | string | Canonical committee code. |
| `committee_name` | string | Committee name active on the event effective date. |
| `action` | string | `APPOINTED` or `REMOVED`. |
| `decision_date` | date | Date recorded for the documented decision. |
| `effective_date` | date | Date on which the membership change takes effect. Interval reconstruction uses this field. |
| `release_source_document_id` | string | Canonical identifier for the supporting source document. |
| `internal_source_document_id` | integer | Noncanonical database source-document identifier. |
| `source_locator` | string | Page, block, or source anchor within the document. |
| `text_span` | string | Extracted evidence text. |
| `extraction_mode` | string | Parser extraction mode. |
| `note_types` | string, nullable | Sorted, semicolon-delimited explanatory note categories. |
| `interpretation_basis` | string, nullable | Delimiter-joined explanation of any interpretive rule applied. |

### `committee_members_<range>.csv`

One row per member service interval used by the release.

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `release_version` | string | Release tag or version label. |
| `schema_version` | string | Stable export schema version. |
| `bioguide_id` | string | Bioguide identifier. |
| `congress_no` | integer | Congress number. |
| `chamber` | string | `H` for House or `S` for Senate. |
| `service_start` | date | Inclusive start of the service interval used by the release. |
| `service_last_active_date` | date | Inclusive last active day of that service interval. |
| `first_name` | string, nullable | Member first name. |
| `last_name` | string, nullable | Member last name. |
| `official_full_name` | string, nullable | Official full name. |
| `nickname` | string, nullable | Nickname or preferred name. |
| `state` | string | Two-letter state or jurisdiction code. |
| `district` | integer, nullable | House district number; blank where not applicable. |
| `party_code` | integer, nullable | Party code when available. |
| `caucus_party_code` | integer, nullable | Party or caucus code used for committee ordering. This may differ from `party_code` for an Independent caucusing with another party. |
| `exit_reason` | string, nullable | Recorded reason the member's chamber service ended. |

### `committee_committees_<range>.csv`

One row per historical committee-name validity interval.

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `release_version` | string | Release tag or version label. |
| `schema_version` | string | Stable export schema version. |
| `committee_code` | string | Canonical committee code. |
| `chamber` | string | `H` for House or `S` for Senate. |
| `committee_name` | string | Historical committee name for this validity interval. |
| `valid_start` | date | Inclusive start of the committee-name validity interval. |
| `valid_last_active_date` | date, nullable | Inclusive last active date of the name; blank for an open-ended interval. |
| `is_joint` | boolean | Whether the underlying committee record is joint. Released assignments exclude joint committees. |

### `committee_sources_<range>.csv`

One row per source document referenced by released events or rankings.

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `release_version` | string | Release tag or version label. |
| `schema_version` | string | Stable export schema version. |
| `release_source_id` | string | Canonical identifier for the source collection or ingest source. |
| `internal_source_id` | integer, nullable | Noncanonical database source identifier. |
| `release_source_document_id` | string | Canonical source-document identifier. |
| `internal_source_document_id` | integer | Noncanonical database source-document identifier. |
| `source_type` | string | Source type recorded by the ingest pipeline. |
| `source_name` | string | Source name recorded by the ingest pipeline. |
| `version_tag` | string, nullable | Source version tag. |
| `external_id` | string, nullable | External document or package identifier. |
| `doc_date` | date, nullable | Date recorded for the source document. |
| `url` | string, nullable | Source URL. |
| `content_hash` | string, nullable | Content SHA or equivalent document hash. |
| `retrieved_at_utc` | string, nullable | Runtime retrieval timestamp; blank in deterministic release exports. |
| `created_at_utc` | string, nullable | Runtime database-creation timestamp; blank in deterministic release exports. |

### `validation_summary_<range>.csv`

One row per versioned release-validation cell.

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `release_version` | string | Release tag or version label. |
| `schema_version` | string | Stable export schema version. |
| `validation_policy_version` | string | Version of the validation policy. |
| `validation_type` | string | Validation source family, currently `directory_overlap`. |
| `congress_no` | integer | Congress number. |
| `chamber` | string | `H` for House or `S` for Senate. |
| `snapshot_date` | date | Congressional Directory snapshot date. |
| `committee_scope` | string | Committee scope used for the validation cell. |
| `gate_status` | string | Pass/fail gate result. |
| `reference_count` | integer, nullable | Reference row count. |
| `observed_count` | integer, nullable | Reconstructed row count. |
| `overlap_count` | integer, nullable | Rows present in both reference and reconstruction. |
| `reference_only_count` | integer, nullable | Rows present only in the reference. |
| `observed_only_count` | integer, nullable | Rows present only in the reconstruction. |
| `directory_member_entries` | integer, nullable | Directory member-entry denominator. |
| `resolved_directory_assignments` | integer, nullable | Directory assignments resolved to comparison tuples. |
| `unresolved_member_entries` | integer, nullable | Directory member rows that could not be resolved. |
| `unmapped_committee_entries` | integer, nullable | Directory rows whose committee could not be mapped. |
| `unmapped_committees` | integer, nullable | Distinct unmapped committees. |
| `member_resolution_pct` | float, nullable | Directory member-resolution percentage. |
| `directory_coverage_pct` | float, nullable | Percentage of Directory reference rows observed in the reconstruction. |
| `observed_overlap_pct` | float, nullable | Percentage of reconstructed rows observed in the Directory reference. |

### `directory_mismatches_<range>.csv`

Row-level evidence behind Congressional Directory comparisons. This dataset is CSV-only and does not have a workbook sheet.

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `release_version` | string | Release tag or version label. |
| `schema_version` | string | Stable export schema version. |
| `congress_no` | integer | Congress number. |
| `snapshot_date` | date | Congressional Directory snapshot date. |
| `chamber` | string | `H` for House or `S` for Senate. |
| `committee_scope` | string | Committee scope used for the comparison. |
| `side` | string | Mismatch or resolution-failure category. |
| `raw_member_name` | string, nullable | Member name printed in the Directory. |
| `bioguide_id` | string, nullable | Resolved Bioguide identifier. |
| `committee_text` | string, nullable | Committee heading printed in the Directory. |
| `committee_code` | string, nullable | Resolved canonical committee code. |
| `detail` | string, nullable | Resolution error or other mismatch detail. |

## Companion artifacts

| Artifact | Role |
| :--- | :--- |
| `committee_membership_<range>.xlsx` | Convenience workbook generated from the same rows as the canonical CSVs. It contains Assignments, Rankings, Events, Members, Committees, Sources, Validation, Data Dictionary, and Release Metadata sheets. |
| `release_metadata.json` | Machine-readable release version, schema version, code revision, input-manifest hashes, row counts, and artifact hashes. |
| `SHA256SUMS` | Checksums for the published release artifacts. |
| `manifests/<congress>.csv` | Deterministic source inventory for a Congress. |
| `manifests/source-bundles.json` | Index of the frozen, checksummed source archives required for exact replay. |

The CSV files are canonical. The workbook must not be treated as an independent data source.

## Release-safety terms

| Term | Definition |
| :--- | :--- |
| Raw corpus | Working source trees such as `data/resolutions`, `data/crec`, `data/journals`, `data/congressional_directories`, and `data/primary`. They are intentionally blocked from the source-only release tree. |
| Frozen source bundle | Checksummed release companion containing the fixed source files needed for historical replay. |
| Agent state | Local automation state such as `.omx`, `.claude`, `.cursor`, or `.agents`; never a release artifact. |
| Forbidden generated output | Dumps, logs, caches, virtual environments, and scratch outputs that would make the release non-reproducible or expose local state. |
