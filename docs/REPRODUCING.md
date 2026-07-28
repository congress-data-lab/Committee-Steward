# Reproducing the Release Artifacts

This guide is the authoritative replay, export, and release-assembly procedure for the 113th–118th release. Two independent frozen-bundle clean-room databases produced byte-identical schema-1.1.0 artifacts, including dated committee rankings, in July 2026.

## Requirements

The native path requires Python 3.11 or later, `uv`, and a clean PostgreSQL database. Set the database URL or pass `--database-url` directly:

```bash
uv sync
export NEON_DATABASE_URL="postgresql://user:password@host:5432/database"
# Use one fixed release timestamp for byte-identical metadata.
export SOURCE_DATE_EPOCH=1784246400
```

Despite the historical environment-variable name, standard PostgreSQL works; the database need not be hosted by Neon. For an isolated PostgreSQL 16 container instead, use [Docker reproduction](DOCKER.md).

## Source modes

`scripts/reproduce.py` supports three modes:

- `frozen-bundle` is the default release-reproduction mode. It hydrates the exact files named in `manifests/source-bundles.json`, verifies declared sizes and SHA-256 hashes, and uses a local indexed archive or its declared HTTPS URL. It requires no GovInfo API key.
- `local` verifies and runs against source files already present at manifest paths. It is intended for development and migration from an existing working corpus.
- `govinfo` performs deterministic GovInfo REST discovery and bounded downloads. It requires `GOVINFO_API_KEY`. Because it does not independently recreate normalized Congressional Directory snapshots, it is a maintenance path rather than a complete historical clean-room replay by itself.

GovInfo acquisition is snapshot-pinned after the first manifest is written: later
reproduction runs reuse the existing manifest and its local files. Use
`--refresh-govinfo` only when deliberately accepting a new live API snapshot;
that refreshed manifest must then be reviewed and archived before release.

House Journal ingestion is diagnostic and disabled by default. `--enable-journals` does not make Journal-derived events part of the canonical release. `--build-source-bundles` creates archives for a future release and is also off by default.

## Replay

Run the bounded pipeline end to end:

```bash
uv run python scripts/reproduce.py \
  --congress-from 113 \
  --congress-to 118 \
  --source-mode frozen-bundle
```

The command writes a machine-readable run ledger below `output/reproduction/`. Exact replay requires the published `manifests/source-bundles.json` and the archives it indexes; the reproducer stops rather than silently substituting different inputs when they are unavailable.

Resume a verified interrupted run with the reproducer's `--resume` option. Resume checks the recorded source-manifest and hydration state before continuing.

## Export

`scripts/export_release.py` is the only canonical release exporter:

```bash
uv run python scripts/export_release.py \
  --congress-from 113 \
  --congress-to 118 \
  --output-dir output/exports/release
```

It writes eight canonical CSVs:

- `committee_assignments_113_118.csv`
- `committee_rankings_113_118.csv`
- `committee_events_113_118.csv`
- `committee_members_113_118.csv`
- `committee_committees_113_118.csv`
- `committee_sources_113_118.csv`
- `validation_summary_113_118.csv`
- `directory_mismatches_113_118.csv`

It also writes `committee_membership_113_118.xlsx`, `release_metadata.json`, and `SHA256SUMS`. The workbook contains nine sheets: Assignments, Rankings, Events, Members, Committees, Sources, Validation, Data Dictionary, and Release Metadata. Directory mismatches are CSV-only.

## Assemble the public tree

Create a source-only release tree:

```bash
uv run python scripts/assemble_release_tree.py /tmp/committee-steward-release
```

Create a complete tree when the license, bundle index, and exported artifacts are available:

```bash
uv run python scripts/assemble_release_tree.py /tmp/committee-steward-release \
  --complete \
  --license-file LICENSE \
  --source-bundle-index manifests/source-bundles.json \
  --release-artifacts-dir output/exports/release
```

`--complete` refuses missing or unsafe inputs. It copies the eight canonical CSVs, workbook, release metadata, checksums, license, and source-bundle index into an allowlisted tree and runs the release safety check before publishing the destination atomically.

## Verification

Run the full test suite:

```bash
uv run python -m pytest tests -q
```

The bounded release-fixture lane used by CI is:

```bash
uv run python -m pytest \
  tests/test_govinfo_manifest.py \
  tests/test_source_bundle.py \
  tests/test_reproduce.py \
  tests/test_release_export.py \
  tests/test_release_assembly.py \
  tests/test_release_validation.py \
  tests/test_docker_release.py \
  tests/test_release_tree.py \
  tests/test_release_docs.py \
  -q
```

Full historical replay remains manual or release-tag initiated because it requires the complete frozen corpus and a clean database. CI fixtures verify orchestration and contracts; the completed reproduction ledger, release gates, and artifact checksums provide clean-room evidence.
