# Export Schema

The release publishes normalized standing-committee data for the 113th through 118th Congresses as canonical UTF-8 CSV files. `scripts/export_release.py` generates every CSV and the analyst-convenience XLSX workbook from one repeatable-read database snapshot. The export schema version is `1.1.0`.

## Canonical artifacts

- `committee_assignments_<range>.csv`: dated membership intervals
- `committee_rankings_<range>.csv`: dated within-party or within-caucus rank intervals
- `committee_events_<range>.csv`: appointment and removal events
- `committee_members_<range>.csv`: member service and affiliation intervals
- `committee_committees_<range>.csv`: committee names and identifiers
- `committee_sources_<range>.csv`: source-document provenance
- `validation_summary_<range>.csv`: release-gate results
- `directory_mismatches_<range>.csv`: row-level Directory validation differences

The scope is House and Senate standing committees only. Joint, select-only, temporary, and subcommittee assignments are excluded unless they map to the standing-committee whitelist. The XLSX workbook contains the seven primary datasets plus Data Dictionary and Release Metadata sheets; it is derivative, not canonical.

## Ranking columns

`committee_rankings_<range>.csv` contains one row per dated rank interval:

| Column | Meaning |
| :--- | :--- |
| `congress_no`, `chamber` | Congress and chamber. |
| `bioguide_id` | Resolved member identifier. |
| `committee_code`, `committee_name` | Canonical committee identity and name active at interval start. |
| `caucus_party_code` | Party group used for committee seniority. |
| `rank_in_party` | One-based rank among active members of that party or caucus. |
| `unresolved_slots_before` | Unresolved source-list positions retained ahead of this member. |
| `rank_start_date` | Inclusive first day of the rank interval. |
| `rank_last_active_date` | Inclusive last day of the rank interval. |
| `rank_end_boundary` | Exclusive interval boundary. |
| `rank_basis` | `resolution_order` or `relative_instruction`. |
| `rank_observation_id` | Stable identifier for the supporting ordered-source observation. |
| `release_source_document_id`, `source_locator` | Resolution provenance. |
| `raw_member_name` | Member label printed in the resolution. |
| `rank_after_raw_name` | Printed predecessor from an explicit relative-rank instruction, when present. |
| `observation_kind` | `ORDERED_LIST`, `FULL_ROSTER`, or `RELATIVE_ORDER`. |

Rank is within party or caucus, not across the full committee. Missing names remain explicit source slots; they are not discarded to make later resolved members appear more senior.

## Date semantics

- Assignment and rank start dates are inclusive.
- `rank_end_boundary` and the underlying PostgreSQL range upper bound are exclusive.
- `rank_last_active_date` is the exclusive boundary minus one day.
- Event decision dates and interval-effective dates may differ; consumers should retain the source identifiers and locators.

## Release companions

Each release also carries `release_metadata.json`, `SHA256SUMS`, per-Congress source manifests, and frozen source bundles referenced by `manifests/source-bundles.json`. The repository safety check rejects working raw-corpus directories, local logs, dumps, caches, and agent state from assembled public trees.
