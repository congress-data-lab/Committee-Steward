# Methods

## Release scope

Version 0.1 reconstructs parent-committee membership for Congresses 113 through 118. It does not claim coverage for joint committees, subcommittees, or committee codes outside the released set below. Select and temporary committees outside that set are excluded.

The configured parser whitelist is broader than the realized release: it also recognizes `HLIG` and `SLIN`, but the 113th–118th assignment artifacts contain no rows for those codes. They are therefore not claimed as released coverage. The table below is the realized committee-code coverage in `committee_assignments_113_118.csv` and `committee_committees_113_118.csv`, not a claim that every listed committee retained the same formal classification or name throughout the period.

## Released committee coverage

| Chamber | Code | Committee |
| :--- | :--- | :--- |
| `H` | `HSAG` | Agriculture |
| `H` | `HSAP` | Appropriations |
| `H` | `HSAS` | Armed Services |
| `H` | `HSBA` | Financial Services |
| `H` | `HSBU` | Budget |
| `H` | `HSED` | Education and the Workforce |
| `H` | `HSFA` | Foreign Affairs |
| `H` | `HSGO` | Oversight and Government Reform / Oversight and Accountability |
| `H` | `HSHA` | House Administration |
| `H` | `HSHM` | Homeland Security |
| `H` | `HSIF` | Energy and Commerce |
| `H` | `HSII` | Natural Resources |
| `H` | `HSJU` | Judiciary |
| `H` | `HSPW` | Transportation and Infrastructure |
| `H` | `HSRU` | Rules |
| `H` | `HSSM` | Small Business |
| `H` | `HSSO` | Ethics |
| `H` | `HSSY` | Science, Space, and Technology |
| `H` | `HSVR` | Veterans' Affairs |
| `H` | `HSWM` | Ways and Means |
| `S` | `SLIA` | Indian Affairs |
| `S` | `SSAF` | Agriculture, Nutrition, and Forestry |
| `S` | `SSAP` | Appropriations |
| `S` | `SSAS` | Armed Services |
| `S` | `SSBK` | Banking, Housing, and Urban Affairs |
| `S` | `SSBU` | Budget |
| `S` | `SSCM` | Commerce, Science, and Transportation |
| `S` | `SSEG` | Energy and Natural Resources |
| `S` | `SSEV` | Environment and Public Works |
| `S` | `SSFI` | Finance |
| `S` | `SSFR` | Foreign Relations |
| `S` | `SSGA` | Homeland Security and Governmental Affairs |
| `S` | `SSHR` | Health, Education, Labor, and Pensions |
| `S` | `SSJU` | Judiciary |
| `S` | `SSRA` | Rules and Administration |
| `S` | `SSSB` | Small Business and Entrepreneurship |
| `S` | `SSVA` | Veterans' Affairs |

Historical committee names are exported as dated rows in `committee_committees_113_118.csv`. The labels above are navigation aids rather than replacements for those dated names.

## Reconstruction approach

Committee Steward treats membership as an event-sourced interval problem:

1. Official documents are discovered and inventoried in deterministic GovInfo manifests.
2. House and Senate assignment resolutions and Congressional Record material are parsed into source-supported appointment and removal events.
3. Member service, party/caucus affiliation, and committee-name history are loaded from checksummed `unitedstates/congress-legislators` snapshots.
4. Appointment and removal events are replayed into continuous committee-assignment intervals.
5. Ordered resolution slots and explicit relative-rank instructions are retained as rank observations.
6. Active membership, caucus affiliation, source order, and termination boundaries are replayed into dated within-party rank intervals.
7. Congressional Directory snapshots are compared with reconstructed membership at documented dates as independent validation evidence.
8. Canonical CSVs and the derivative workbook are exported from one repeatable-read database snapshot.

House Journals may be ingested for diagnostic comparison, but Journal-derived events are not canonical release inputs. Frozen-bundle replay reproduces the accepted source corpus and does not perform new web searches or substitute newly available documents.

## Assignment and event semantics

An assignment interval begins on `committee_assignments.start_date`. It remains active through `last_active_date`; `termination_effective_date` is the exclusive boundary on which it stops being active. For an explicit mid-Congress removal, the closing event's `effective_date` supplies that boundary. For a natural Congress ending, the boundary is the end of the congressional term and `ended_early` is false.

Events preserve both `decision_date` and `effective_date`. A resignation, death, expulsion, appointment, or resolution may be decided, recorded, and made effective on different dates. Consumers reconstructing membership on a date should use interval boundaries rather than assuming publication date, decision date, and effective date are interchangeable.

## Ranking semantics

Committee rank is reconstructed within each party or caucus, not across the whole committee. Initial ordered appointment lists establish sequence. Later instructions placing a member immediately after another member update that sequence. A Senate resolution containing a full roster can reset the order for that party and committee.

When a member's assignment ends, remaining active members compress upward on the following day. The system does not infer rank from age, chamber seniority, or periodically maintained third-party roster files. An unresolved printed name remains an unresolved source slot; `unresolved_slots_before` reports how many such positions remain ahead of each resolved member.

`rank_start_date` and `rank_last_active_date` are inclusive. `rank_end_boundary` is exclusive. Each ranking row retains the supporting source-document identifier, locator, printed member name, observation type, and derivation basis.

## Source coverage and limitations

The per-Congress manifests and frozen-bundle index define the corpus used by the release. They are authoritative for source availability; the release does not infer uniform coverage merely because a source family exists for part of the range.

- Congressional Record and resolution inputs are frozen at the exact accepted bytes and hashes used in reconstruction.
- Congressional Directory snapshots provide point-in-time validation rather than a complete daily event ledger.
- House Journals are validation-only and are not required for canonical replay.
- Senate Journals may be preserved when available but are not canonical event inputs.
- Title-based document classification is a discovery aid, not by itself an exhaustive replay boundary.
- No uncited manual corrections are inserted into the public reconstruction.

## Validation posture

Release validation has two independent layers:

1. Integrity checks reject duplicate, overlapping, empty, out-of-Congress, and out-of-service intervals.
2. Directory comparisons measure member resolution and assignment overlap for each required Congress, chamber, and snapshot cell, while preserving row-level mismatches.

Thresholds and required cells are versioned in `config/release-validation-policy.json`. A missing required cell or failed required threshold blocks release generation.

Separately obtained historical reference data may be used for optional maintainer audits after reconstruction. It is not distributed, loaded by the public pipeline, or used as a public release gate.

## Artifact policy

Canonical data artifacts are the eight CSV files documented in the [data dictionary](DATA_DICTIONARY.md). The XLSX workbook is derived from seven of those datasets and adds Data Dictionary and Release Metadata sheets; directory mismatches remain CSV-only. Per-Congress manifests, release metadata, checksums, and frozen source bundles provide the evidence needed for exact replay.

The assembled source tree excludes working raw-corpus directories, credentials, local databases, logs, dumps, caches, agent state, and optional internal audit tooling.
