# Committee Steward

Committee Steward reconstructs who served on each House and Senate standing committee, when that service began and ended, and which official documents support each change. The initial production release covers the 113th through 118th Congresses.

The project publishes both continuous assignment intervals and the underlying appointment and removal events. It is designed for deterministic replay from frozen, checksummed source files rather than from a preexisting database or a manually maintained spreadsheet.

> [!IMPORTANT]
> **Pre-release status:** two independent full-range clean-room databases produced byte-identical licensing-safe reconstruction artifacts in July 2026. The v0.1 public release remains a draft; exact historical replay requires the frozen source-bundle index and archives distributed with that release.

The current 116th–118th source-acquisition checkpoint, root-cause analysis, and exact resume commands are recorded in [docs/SOURCE_COMPLETION_HANDOFF.md](docs/SOURCE_COMPLETION_HANDOFF.md).

## Scope

| Included | Not included |
|---|---|
| 113th-118th Congresses | Congresses outside the tagged release range |
| House and Senate | Joint committees |
| Standing committees | Select, special, and temporary committees |
| Assignment intervals | Subcommittee assignments |
| Appointment and removal events | Informal or speculative assignments |
| Source citations and validation evidence | Uncited manual corrections |

The public reconstruction pipeline uses official congressional sources and Congressional Directory snapshots. Historical Stewart assignment data is not distributed, loaded, or required by the public reproduction path. Maintainers may compare completed reconstructions with separately obtained reference data as an optional internal audit.

Member identity, service, party/caucus, and committee-name snapshots come from [`unitedstates/congress-legislators`](https://github.com/unitedstates/congress-legislators), which dedicates its data to the public domain under CC0. The exact YAML bytes used by a release are checksummed inputs to the reproduction ledger.

## Release Contents

Each tagged release is designed to provide:

- Canonical UTF-8 CSV files for assignments, dated within-party committee rankings, events, members, committees, sources, and validation results.
- A master Excel workbook generated from the same rows as the canonical CSV files.
- Congressional Directory mismatch files rather than aggregate scores alone.
- Per-Congress source manifests and SHA-256 checksums.
- Frozen source archives referenced by `manifests/source-bundles.json`.
- A machine-readable reproduction ledger and release metadata.

Large raw source archives are release assets, not ordinary Git-tracked files. The frozen archives contain the exact Congressional Record JSON, committee-assignment resolutions, normalized Directory material, and other required inputs used for the release. House Journals are validation-only and are not canonical event inputs. Reproducing a tagged release therefore does not require rerunning the external Congressional Record parser, searching GovInfo, or using an MCP server.

Rankings are derived from the order in official House and Senate assignment resolutions, explicit House instructions to place a member immediately after another member, and membership termination dates. They are ranks within the member's committee party or caucus, not whole-committee rank. Unresolved names retain their source-list positions as explicit gaps rather than silently promoting later members.

The canonical output files for the initial release are:

```text
committee_assignments_113_118.csv
committee_events_113_118.csv
committee_members_113_118.csv
committee_committees_113_118.csv
committee_sources_113_118.csv
validation_summary_113_118.csv
directory_mismatches_113_118.csv
committee_membership_113_118.xlsx
release_metadata.json
SHA256SUMS
```

CSV files are the machine-readable source of truth. The workbook is a convenience representation with matching data and eight documented sheets.

## Reproduce With Docker

Docker is the recommended release-reproduction path. It supplies Python 3.11 and an isolated PostgreSQL 16 database.

```bash
cp .env.example .env
```

Set a local database password in `.env` and leave the release source mode unchanged:

```dotenv
POSTGRES_PASSWORD=choose-a-local-password
SOURCE_MODE=frozen-bundle
SOURCE_BUNDLE_INDEX=manifests/source-bundles.json
CONGRESS_FROM=113
CONGRESS_TO=118
```

Then run:

```bash
mkdir -p release-output
docker compose up --build --abort-on-container-exit --exit-code-from pipeline pipeline
```

The database is accessible only on the private Compose network. Reproduction ledgers, validation reports, CSV files, and the workbook are written below `release-output/reproduction/`.

Frozen-bundle reproduction requires no API key. Until the source archives are published with the first release, this command will stop at source hydration rather than silently substitute different inputs.

## Reproduce Without Docker

Native reproduction requires Python 3.11+, `uv`, and a clean PostgreSQL database:

```bash
uv sync
export NEON_DATABASE_URL="postgresql://user:password@host:5432/database"
uv run python scripts/reproduce.py \
  --congress-from 113 \
  --congress-to 118 \
  --source-mode frozen-bundle
```

Despite the historical environment-variable name, the database can be standard PostgreSQL and does not need to be hosted by Neon.

For all source modes, resume behavior, and release assembly instructions, see [REPRODUCING.md](REPRODUCING.md). For the container lifecycle and reset commands, see [docs/DOCKER.md](docs/DOCKER.md).

## Updating The Dataset

Exact reproduction and source acquisition are separate workflows:

- `frozen-bundle` hydrates the exact inputs used by a tagged release. This is the default for users and clean-room verification.
- `govinfo` performs deterministic GovInfo REST discovery and download for maintainers. It requires `GOVINFO_API_KEY`.
- `local` verifies an existing source tree and supports migration from older working data dumps.

GovInfo MCP access is optional and is used only for document discovery, parser research, and discrepancy investigation. It is not part of the production runtime or reproducibility contract.

The `unitedstates/congressional-record` parser is also a maintenance tool, not a user prerequisite. Maintainers may use a pinned parser revision to create new CREC JSON, compare its inventory against GovInfo manifests, review gaps, and then freeze the accepted files into the next source bundle.

A future Congress is not releasable merely because documents were downloaded. Its manifests, normalized inputs, parser results, validation evidence, and frozen archive must all pass the release gates first.

## Data Model

Official documents are parsed into a `committee_event` ledger. Appointment and removal events are then replayed into continuous `committee_membership` intervals. Every released interval retains links to its supporting events and source documents where those events exist.

Dates have distinct meanings:

- `appointment_date` and `termination_date` describe source-supported events.
- `start_date` and `end_date` describe the resulting service interval.
- `end_date` is inclusive in release exports.
- A natural end of Congress is not classified as an early departure.

See [DATA_DICTIONARY.md](DATA_DICTIONARY.md) for column definitions and [METHODS.md](METHODS.md) for the reconstruction and validation method.

## Validation

Release generation runs two independent classes of checks:

1. Membership integrity checks reject duplicate, overlapping, empty, out-of-Congress, and out-of-service intervals.
2. Congressional Directory comparisons evaluate standing-committee rosters at documented snapshot dates for the 113th-118th Congresses.

Separately licensed historical datasets may be used by maintainers for optional post-reconstruction audits. They are not public release inputs, release gates, or release artifacts.

Validation thresholds are versioned in `config/release-validation-policy.json`. A missing validation cell or failed required threshold blocks release generation.

## Development

Install the locked environment and run the test suite:

```bash
uv sync
uv run python -m pytest tests -q
```

Raw corpora, credentials, local databases, logs, caches, and agent state are forbidden from the production Git tree. `scripts/assemble_release_tree.py` copies only the allowlisted production files, and `scripts/check_release_tree.py` audits the result.

## Citation

Citation metadata is provided in [CITATION.cff](CITATION.cff). Each release's `release_metadata.json` records the code revision, schema version, input-manifest hashes, output row counts, and artifact hashes needed to identify the exact dataset used in downstream work.
