# Committee Steward

Committee Steward reconstructs who served on each House and Senate committee in the released scope, when that service and within-party committee rank began and ended, and which official documents support each change. The initial production release covers the 113th through 118th Congresses.

The project publishes normalized assignment intervals, dated within-party rankings, underlying appointment and removal events, source provenance, and validation results. It is designed for deterministic replay from frozen, checksummed source files rather than from a preexisting database or manually maintained spreadsheet.

> [!IMPORTANT]
> **Pre-release status:** two independent full-range clean-room databases produced byte-identical licensing-safe reconstruction artifacts in July 2026. The v0.1 public release remains a draft; exact historical replay requires the frozen source-bundle index and archives distributed with that release.

## Scope

The v0.1 release covers 37 House and Senate committee codes represented in the reconstructed 113th–118th assignment data. It excludes joint committees, temporary and select committees outside that released code set, and all subcommittee assignments. [Methods](docs/METHODS.md) defines the exact committee coverage, source limitations, reconstruction rules, ranking semantics, and date model.

Member identity, service, party/caucus, and committee-name snapshots come from [`unitedstates/congress-legislators`](https://github.com/unitedstates/congress-legislators), which dedicates its data to the public domain under CC0. The exact YAML bytes used by a release are checksummed inputs to the reproduction ledger.

Historical Stewart assignment data is not distributed, loaded, or required by the public reproduction path. Maintainers may compare completed reconstructions with separately obtained reference data as an optional internal audit.

## Release contents

The canonical machine-readable files are:

```text
committee_assignments_113_118.csv
committee_rankings_113_118.csv
committee_events_113_118.csv
committee_members_113_118.csv
committee_committees_113_118.csv
committee_sources_113_118.csv
validation_summary_113_118.csv
directory_mismatches_113_118.csv
```

Each release also contains `committee_membership_113_118.xlsx`, `release_metadata.json`, and `SHA256SUMS`. The workbook is a convenience representation with nine sheets: the seven primary datasets, Data Dictionary, and Release Metadata. Directory mismatches remain CSV-only.

Large raw source archives are release assets rather than ordinary Git-tracked files. Frozen archives contain the exact Congressional Record JSON, committee-assignment resolutions, normalized Congressional Directory material, and other required inputs. House Journals are validation-only and are not canonical event inputs.

See the [data dictionary](docs/DATA_DICTIONARY.md) for every exported column and the normalized join model.

## Quickstart

Docker is the recommended reproduction path and requires no GovInfo API key when the release source bundles are available:

```bash
cp .env.example .env
mkdir -p release-output
docker compose up --build --abort-on-container-exit --exit-code-from pipeline pipeline
```

Set a local `POSTGRES_PASSWORD` in `.env` before running the command. Outputs are written below `release-output/reproduction/`.

For the complete replay, export, and release-assembly workflow, see [Reproducing the release](docs/REPRODUCING.md). For container configuration and reset behavior, see [Docker reproduction](docs/DOCKER.md).

## Validation

Release generation rejects duplicate, overlapping, empty, out-of-Congress, and out-of-service assignment intervals. It also compares reconstructed membership with Congressional Directory snapshots throughout the release range. Required thresholds are versioned in `config/release-validation-policy.json`; a missing validation cell or failed required threshold blocks release generation.

Separately licensed historical datasets may be used for optional maintainer audits. They are not public release inputs, release gates, or release artifacts.

## Development

Install the locked environment and run the test suite:

```bash
uv sync
uv run python -m pytest tests -q
```

Raw corpora, credentials, local databases, logs, caches, and agent state are forbidden from the production Git tree. `scripts/assemble_release_tree.py` copies only allowlisted production files, and `scripts/check_release_tree.py` audits the result.

Citation metadata is provided in [CITATION.cff](CITATION.cff). Each release's `release_metadata.json` records the code revision, schema version, input-manifest hashes, output row counts, and artifact hashes needed to identify the exact dataset used downstream.
