# Data Dictionary

This dictionary covers the public release surface for the standing-committee dataset scoped to Congresses 113 through 118.

## Dataset-level terms

| Term | Definition |
| :--- | :--- |
| Standing committee | A committee included in the project whitelist in `docs/SCOPE.md`. The release does not claim coverage for joint committees, temporary committees, or subcommittees. |
| Membership interval | A continuous span of service on one committee for one member, stored in `committee_membership.valid_daterange` and exported as a single row. |
| Rank observation | An ordered source slot captured from an official assignment resolution. The slot is retained even when its printed member name cannot be resolved. |
| Rank interval | A continuous span during which a member held one within-party committee rank, stored in `committee_membership_rank.valid_daterange`. |
| Canonical CSV | One of the release-grade tabular exports emitted by `scripts/export_release.py`. |
| Derivative XLSX | A workbook containing the same canonical datasets plus data-dictionary and release-metadata sheets. It is for convenience only and should not diverge from the CSVs. |
| Source manifest | A deterministic GovInfo inventory such as `data/manifests/manifest_118.csv`, with identifiers, source URLs, canonical local paths, hashes, and retrieval status. |
| Frozen source bundle | A release companion artifact prepared outside the working repository when downstream users need fixed source files without taking the full raw corpus tree. |

## Key identifiers

| Field | Definition |
| :--- | :--- |
| `congress_no` | Numeric congress identifier. |
| `chamber` | `H` for House, `S` for Senate. |
| `bioguide_id` | Canonical member identifier shared across loaders and exports. |
| `committee_code` | Stable committee identifier used internally and in exports. |
| `caucus_party_code` | Party group used for committee ranking. It normally equals `party_code`; Independents may caucus with another party. |
| `rank_in_party` | One-based rank among active members in the same committee party or caucus. It is not whole-committee rank. |
| `unresolved_slots_before` | Count of unresolved source slots retained ahead of the member. This makes uncertainty visible without silently advancing later members. |
| `appointment_citation` / `termination_citation` | Preferred human-readable citation string for the opening or closing event. |

## Date fields

| Field | Definition |
| :--- | :--- |
| `start_date` | Inclusive first day of service in the exported interval. |
| `end_date` | Inclusive last day of service in the exported interval; blank when the interval remains open through the congress boundary. |
| `appointment_date` | Decision date on the interval-opening event when a source event exists. |
| `termination_date` | Decision date on the interval-closing event when a source event exists; otherwise the inclusive `end_date`. |
| `congress_end_date` | End boundary of the congress from the `congress` table. |
| `rank_start_date` | Inclusive first day on which the exported rank applies. |
| `rank_last_active_date` | Inclusive last day on which the exported rank applies. |
| `rank_end_boundary` | Exclusive end boundary of the rank interval. |

## Provenance fields

| Field | Definition |
| :--- | :--- |
| `appointment_source_document` | External identifier for the source document that opened the interval. |
| `appointment_source_locator` | Page, locator, or source anchor for the opening event. |
| `termination_source_document` | External identifier for the source document that closed the interval. |
| `termination_source_locator` | Page, locator, or source anchor for the closing event. |
| `termination_note_types` | Aggregated explanatory note categories on the closing event. |
| `termination_interpretation_basis` | Aggregated explanatory note text on the closing event. |
| `rank_basis` | Evidence rule used to derive the rank: source-list order or an explicit relative-rank instruction. |
| `rank_observation_id` | Stable identifier for the ordered source observation supporting the rank. |
| `rank_after_raw_name` | Printed predecessor named by an instruction to rank a member immediately after another member. |

## Release safety terms

| Term | Definition |
| :--- | :--- |
| Raw corpus | Working-repository source trees such as `data/resolutions`, `data/crec`, `data/journals`, `data/congressional_directories`, and `data/primary`. These are intentionally blocked by `scripts/check_release_tree.py`. |
| Agent state | Local automation state such as `.omx`, `.claude`, `.cursor`, or `.agents`. These are never releasable artifacts. |
| Forbidden generated output | Dumps, logs, caches, virtual environments, and scratch outputs that would make the release non-reproducible or leak local state. |
