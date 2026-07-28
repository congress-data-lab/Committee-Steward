# Docker Reproduction

Docker standardizes Python 3.11 and PostgreSQL 16. It does not make MCP or live web search part of the production runtime. Exact historical reproduction uses checksum-pinned frozen source bundles.

The supported source modes and their limitations are defined once in [Reproducing the release artifacts](REPRODUCING.md#source-modes).

## Configure and run

Copy the environment template and set a local database password:

```bash
cp .env.example .env
```

```dotenv
POSTGRES_PASSWORD=choose-a-local-password
SOURCE_MODE=frozen-bundle
SOURCE_BUNDLE_INDEX=manifests/source-bundles.json
CONGRESS_FROM=113
CONGRESS_TO=118
```

Set `GOVINFO_API_KEY` only when using the maintainer-oriented `govinfo` mode. Frozen-bundle replay requires no API key. `NEON_DATABASE_URL` is not used by Compose because it creates an isolated PostgreSQL service.

Run the one-shot pipeline:

```bash
mkdir -p release-output
docker compose up --build --abort-on-container-exit --exit-code-from pipeline pipeline
```

PostgreSQL has no host port mapping. The pipeline reaches it only through the private Compose network. Generated ledgers, validation evidence, CSVs, and the workbook appear below `release-output/reproduction/`. Compose retains hydrated manifests alongside its project-scoped source cache so `--resume` cannot pair persisted source data with an unrelated manifest from a newly created pipeline container.

## Reset

To delete the Compose database and hydrated-source cache volumes:

```bash
docker compose down --volumes
```

This does not delete the host's `release-output/` directory.
