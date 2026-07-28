#!/usr/bin/env python3
"""Create the production schema in an empty database or verify an existing one."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_connection


ROOT = Path(__file__).resolve().parent.parent
REQUIRED_TABLES = frozenset(
    {
        "chamber",
        "congress",
        "source",
        "source_document",
        "member",
        "member_service",
        "committee",
        "committee_event",
        "committee_membership",
        "committee_rank_observation",
        "committee_membership_rank",
    }
)
REQUIRED_COLUMNS = frozenset(
    {
        ("member_service", "source_document_id"),
        ("member_service", "caucus_party_code"),
    }
)
REQUIRED_INDEXES = frozenset(
    {"member_service_logical_key", "source_identity_key"}
)


def inspect_schema(conn) -> tuple[set[str], set[tuple[str, str]], set[str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = ANY(%s)
            """,
            (sorted(REQUIRED_TABLES),),
        )
        tables = {row[0] for row in cur.fetchall()}
        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND (table_name, column_name) IN (
                ('member_service', 'source_document_id'),
                ('member_service', 'caucus_party_code')
              )
            """
        )
        columns = {(row[0], row[1]) for row in cur.fetchall()}
        cur.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public' AND indexname = ANY(%s)
            """,
            (sorted(REQUIRED_INDEXES),),
        )
        indexes = {row[0] for row in cur.fetchall()}
    return tables, columns, indexes


def ensure_schema(conn, schema_path: Path) -> str:
    tables, columns, indexes = inspect_schema(conn)
    if not tables:
        with conn.cursor() as cur:
            cur.execute(schema_path.read_text(encoding="utf-8"))
        conn.commit()
        tables, columns, indexes = inspect_schema(conn)
        action = "created"
    else:
        action = "verified"

    missing_tables = REQUIRED_TABLES - tables
    missing_columns = REQUIRED_COLUMNS - columns
    missing_indexes = REQUIRED_INDEXES - indexes
    if missing_tables or missing_columns or missing_indexes:
        conn.rollback()
        details = []
        if missing_tables:
            details.append("tables=" + ",".join(sorted(missing_tables)))
        if missing_columns:
            details.append(
                "columns="
                + ",".join(f"{table}.{column}" for table, column in sorted(missing_columns))
            )
        if missing_indexes:
            details.append("indexes=" + ",".join(sorted(missing_indexes)))
        raise RuntimeError(
            "Existing database is not at the production schema: "
            + "; ".join(details)
            + ". Apply db/migrations through 0018 before reproducing."
        )
    return action


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, default=ROOT / "db" / "schema.sql")
    args = parser.parse_args()

    conn = get_connection()
    try:
        action = ensure_schema(conn, args.schema)
    finally:
        conn.close()
    print(f"Schema {action}: {args.schema}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
