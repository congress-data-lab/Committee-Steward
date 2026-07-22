# Methods

## Release scope

The public release lane is limited to **House and Senate standing committees for the 113th through 118th Congresses**. This is narrower than the full working repository. The release does not claim joint-committee, temporary-committee, subcommittee, or open-ended post-118 coverage.

## Reconstruction approach

The database reconstructs committee service as an event-sourced interval problem:

1. Official documents are discovered and inventoried in deterministic GovInfo manifests.
2. Loaders parse appointment and removal evidence from House and Senate resolutions, House Journals, Congressional Record JSON, and Congressional Directory snapshots.
3. Parsed `committee_event` rows are replayed into `committee_membership` intervals.
4. Ordered resolution slots and explicit relative-rank instructions are retained as `committee_rank_observation` evidence.
5. Active membership, caucus affiliation, source order, and removals are replayed into dated `committee_membership_rank` intervals.
6. Release CSVs are exported from those intervals rather than manually curated downstream tables.

This means the release artifacts are reproducible from the same inputs and code, but they are not hand-maintained spreadsheets.

## Ranking semantics

Committee rank is reconstructed within each party or caucus. Initial ordered appointment lists establish the sequence; later instructions that place a member immediately after another member update that sequence. When a member's committee service ends, the remaining active members compress upward on the following day. A Senate resolution that supplies a full roster can reset the order for that party and committee.

The system does not infer rank from age, chamber seniority, or periodic third-party roster files. An unresolved printed name remains an unresolved source slot, and exported rows report how many such slots precede each resolved member. Rankings therefore express the strongest order supported by the captured official documents while preserving visible uncertainty.

## Source coverage limitations

The frozen manifests describe the corpus actually available to the release; they do not imply uniform source coverage across all six Congresses.

- CREC packages are complete in the local manifests for the 113th through 118th Congresses.
- House Journals are available for the 113th through 116th Congresses. The 117th manifest is missing one House Journal and the 118th manifest is missing both listed House Journals; Journals are validation-only and are not canonical event inputs.
- Senate Journals are preserved when available but are not ingested by the current House-only journal loader.
- All retrieved House and Senate resolution renditions are bundled. Title-based `committee_assignment` classification alone is not an exhaustive replay boundary.

Accordingly, a frozen-bundle replay can reproduce the published dataset, but the release does not claim uniform CREC or journal coverage for the 113th through 118th Congresses. The per-Congress manifests are authoritative for those coverage boundaries.

## Validation posture

- Directory snapshots provide point-in-time external checks throughout the release range.
- Separately obtained historical reference data may be used for optional maintainer audits, but it is not loaded by the public pipeline or distributed with public artifacts.
- The release lane ships manifests and curated outputs, not the full raw corpora used during development and replay.

## Release artifact policy

- Canonical data artifacts: CSV files for each release dataset
- Optional convenience artifact: XLSX derived from those canonical datasets
- Provenance companions: per-congress manifests and, when needed, frozen source bundles prepared outside the repository-safety gate

The repository safety check is strict on this point: a release tree may contain manifests and curated bundles, but it should not contain the repository's raw corpora, local logs, dumps, caches, or agent state.

## Date semantics

The exported dates have distinct meanings:

- `start_date` and `end_date` represent the interval of service.
- `appointment_date` and `termination_date` are source-event decision dates when those events exist.
- `end_date` is inclusive because the underlying Postgres range uses an exclusive upper bound.

Users comparing rows to document publication dates should rely on the citation fields rather than assuming those dates are identical.
