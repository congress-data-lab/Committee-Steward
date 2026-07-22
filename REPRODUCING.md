# Reproducing The Release Artifacts

This guide documents the current production-facing replay path for the 113th
through 118th Congresses. It covers reproduction, export, and release-tree
assembly. Two independent full 113th–118th frozen-bundle clean-room databases
produced byte-identical licensing-safe v0.1 artifacts in July 2026. Ranking
support is a subsequent feature and must pass its own clean-room verification
before release.

## Scope

- Published release scope: House and Senate standing committees only
- Published Congress scope: 113 through 118
- Production manifest layout: `manifests/<congress>.csv`
- Workspace compatibility: `data/manifests/manifest_<congress>.csv` still works for older fixtures, but the reproducer prefers `manifests/<congress>.csv`

## Environment

Set the database URL before running reproduction or export:

```bash
export NEON_DATABASE_URL="postgresql://..."
```

`scripts/reproduce.py` and `scripts/export_release.py` also accept `--database-url` when you do not want to rely on the environment.

## Reproduce

The one-command production path for Congresses 113 through 118 is:

```bash
python scripts/reproduce.py --congress-from 113 --congress-to 118
```

That command plans and runs the bounded pipeline end to end and writes the run ledger under `output/reproduction/`.
It defaults to `frozen-bundle`; the published `manifests/source-bundles.json` must exist. Until that index and its archives are published, a fresh clone cannot complete an exact historical replay.

For the containerized PostgreSQL path, see `docs/DOCKER.md`.

### Source modes

`scripts/reproduce.py` supports three source modes:

- `local`:
  - Verifies and runs against files already present in the checked-out workspace
  - Intended for development or migration from an existing data dump
- `frozen-bundle`:
  - Default production mode
  - Hydrates source files from `manifests/source-bundles.json` before the schema and ingest stages
  - Uses a local indexed archive when present; otherwise downloads its HTTPS URL atomically and verifies the declared size and SHA-256 before publishing it to the local bundle cache
  - Use this when you want a replay that does not depend on live GovInfo retrieval
- `govinfo`:
  - Builds or refreshes the per-Congress manifest by calling the GovInfo REST client
  - Requires an `api.data.gov` key through `GOVINFO_API_KEY`
  - Does not yet recreate normalized Congressional Directory JSON, so it is not by itself a complete clean-room replay path

House Journal ingestion is validation-only and disabled by default. Use `--enable-journals` only for a diagnostic comparison; Journal-derived events are not part of the canonical release replay.

If you need archived source bundles, add `--build-source-bundles`; it is off by default in the bounded replay path.

## Export

`scripts/export_release.py` is the canonical release export entrypoint:

```bash
python scripts/export_release.py --congress-from 113 --congress-to 118 --output-dir output/exports/release
```

It writes seven primary canonical CSVs:

- `committee_assignments_113_118.csv`
- `committee_rankings_113_118.csv`
- `committee_events_113_118.csv`
- `committee_members_113_118.csv`
- `committee_committees_113_118.csv`
- `committee_sources_113_118.csv`
- `validation_summary_113_118.csv`

It also writes:

- `directory_mismatches_113_118.csv`
- `committee_membership_113_118.xlsx`
- `release_metadata.json`
- `SHA256SUMS`

The workbook has nine sheets:

- Assignments
- Rankings
- Events
- Members
- Committees
- Sources
- Validation
- Data Dictionary
- Release Metadata

## Assemble

`scripts/assemble_release_tree.py` is code-only by default:

```bash
python scripts/assemble_release_tree.py /tmp/release-tree
```

Use `--complete` only when you have the additional release inputs:

```bash
python scripts/assemble_release_tree.py /tmp/release-tree \
  --complete \
  --license-file LICENSE \
  --source-bundle-index manifests/source-bundles.json \
  --release-artifacts-dir output/exports/release
```

`--complete` requires the license file, the source-bundle index, and the canonical release artifacts. It refuses to run if any of those inputs are missing or unsafe.

The current complete release artifact set is larger than the canonical export bundle. `--complete` expects the seven canonical CSVs, `directory_mismatches_<range>.csv`, `committee_membership_<range>.xlsx`, `release_metadata.json`, and `SHA256SUMS`.

## Verification

The bounded release-fixture lane in CI runs:

```bash
python -m pytest \
  tests/test_govinfo_manifest.py \
  tests/test_source_bundle.py \
            tests/test_reproduce.py \
            tests/test_release_export.py \
            tests/test_release_assembly.py \
            tests/test_release_validation.py \
            tests/test_docker_release.py \
            tests/test_release_tree.py \
  -q
```

`test.yml` also runs the full repository test suite. `release-reproducibility.yml` runs the bounded release-fixture lane on manual dispatch and release tags.

## Full Replay Policy

Full replay is intentionally manual or release-tag only. Use `local` or
`frozen-bundle` mode with already available workspace files or frozen bundles
when you need a replay path that does not depend on live MCP access. CI alone
is not clean-room evidence; use the completed reproduction ledger, release
gates, and artifact checksums.

The 2026-07-20 full-range reconstruction evidence is retained locally under
`release-output/cleanroom-113-118-20260720-{a,b}/reproduction/`. Those runs
predate the licensing-safe public artifact inventory and therefore are not the
final public-release verification evidence.
