# Docker Reproduction

Docker standardizes Python 3.11 and PostgreSQL 16. It does not make MCP part of the production runtime. Exact historical reproduction uses checksum-pinned frozen source bundles; GovInfo REST is the supported discovery and download path for a new or refreshed Congress.

## Configure

Create a local `.env` from `.env.example` and set at least:

```dotenv
POSTGRES_PASSWORD=
SOURCE_MODE=frozen-bundle
SOURCE_BUNDLE_INDEX=manifests/source-bundles.json
CONGRESS_FROM=113
CONGRESS_TO=118
```

Set `GOVINFO_API_KEY` to an `api.data.gov` key only when using `SOURCE_MODE=govinfo`. Frozen-bundle replay does not require an API key. `NEON_DATABASE_URL` is not used by Docker Compose because Compose creates an isolated PostgreSQL service.

Create the host output directory and run the one-shot pipeline:

```bash
mkdir -p release-output
docker compose up --build --abort-on-container-exit --exit-code-from pipeline pipeline
```

PostgreSQL has no host port mapping. The pipeline reaches it only through the private Compose network. Generated ledgers, validation evidence, CSVs, and the workbook appear under `release-output/reproduction/`. Compose retains hydrated manifests in the same project-scoped source cache as the raw source files, so `--resume` cannot pair persisted source data with an older manifest from a newly created pipeline container.

## Source Modes

- `frozen-bundle` is the release-reproduction mode. It hydrates exact files named in `manifests/source-bundles.json`, verifies hashes, and requires no preexisting data dump. If an indexed local archive is absent, hydration downloads its HTTPS URL to the local bundle cache and atomically publishes it only after size and SHA-256 verification.
- `govinfo` performs deterministic GovInfo REST discovery and bounded downloads. It requires `GOVINFO_API_KEY`. This is the maintenance path, but a new Congress is not releasable until normalized Directory snapshots and a frozen bundle have also been produced and reviewed.
- `local` verifies files already present at manifest paths. It exists for development and migration from an older data dump.

To reset the database and hydrated-source volumes, stop the stack and explicitly remove its volumes:

```bash
docker compose down --volumes
```

That command deletes the Compose database and source caches. It does not delete `release-output/`.
