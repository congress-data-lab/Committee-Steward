#!/usr/bin/env python3
"""Inventory and safely consolidate duplicate provenance rows.

Dry-run is the default. Pass ``--allow-write`` to repoint every discovered
foreign key and delete duplicate ``source_document`` and ``source`` rows in a
single transaction.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Sequence

import psycopg2
from psycopg2 import sql

from db.connection import get_connection


class UnsafeConsolidation(RuntimeError):
    """Raised when duplicate provenance cannot be consolidated losslessly."""


@dataclass(frozen=True)
class SourceRecord:
    source_id: int
    source_type: str
    source_name: str
    version_tag: str | None


@dataclass(frozen=True)
class DocumentRecord:
    source_document_id: int
    canonical_source_id: int | None
    external_id: str | None
    doc_date: date | None
    url: str | None
    raw_json_text: str | None
    content_hash: str | None


@dataclass(frozen=True)
class ForeignKeyReference:
    schema_name: str
    table_name: str
    column_name: str
    target_table: str
    target_column: str
    constraint_name: str
    local_column_count: int
    target_column_count: int

    @property
    def label(self) -> str:
        return f"{self.schema_name}.{self.table_name}.{self.column_name}"


@dataclass(frozen=True)
class UpdateTrigger:
    schema_name: str
    table_name: str
    trigger_name: str
    enabled_state: str
    function_schema: str
    function_name: str

    @property
    def label(self) -> str:
        return f"{self.schema_name}.{self.table_name}.{self.trigger_name}"

    @property
    def is_enabled(self) -> bool:
        return self.enabled_state != "D"


@dataclass(frozen=True)
class ConsolidationSummary:
    source_groups: int
    sources_removed: int
    document_groups: int
    documents_removed: int
    foreign_keys_discovered: int
    reference_rows: dict[str, int]
    wrote: bool


REQUIRED_REFERENCES = {
    ("public", "source_document", "source_id", "source", "source_id"),
    (
        "public",
        "committee_event",
        "source_document_id",
        "source_document",
        "source_document_id",
    ),
    ("public", "member_service", "source_id", "source", "source_id"),
    (
        "public",
        "member_service",
        "source_document_id",
        "source_document",
        "source_document_id",
    ),
}

HANDLED_UPDATE_TRIGGERS = {
    (
        "public",
        "committee_event",
        "trg_committee_event_parsed",
    ): ("public", "trg_set_parsed_at"),
    (
        "public",
        "committee_membership",
        "trg_committee_membership_parsed",
    ): ("public", "trg_set_parsed_at"),
    (
        "public",
        "member_service",
        "trg_member_service_parsed",
    ): ("public", "trg_set_parsed_at"),
}


def build_source_mapping(
    records: Iterable[SourceRecord],
) -> tuple[dict[int, int], int]:
    """Map duplicate source IDs to the oldest exact-identity source row."""
    groups: dict[tuple[str, str, str | None], list[int]] = defaultdict(list)
    for record in records:
        groups[(record.source_type, record.source_name, record.version_tag)].append(
            record.source_id
        )

    mapping: dict[int, int] = {}
    duplicate_groups = 0
    for source_ids in groups.values():
        if len(source_ids) < 2:
            continue
        duplicate_groups += 1
        keep_id = min(source_ids)
        mapping.update(
            (source_id, keep_id)
            for source_id in source_ids
            if source_id != keep_id
        )
    return mapping, duplicate_groups


def _document_identity(record: DocumentRecord) -> tuple[object, ...] | None:
    if record.canonical_source_id is None:
        return None
    if record.content_hash is not None:
        return ("hash", record.canonical_source_id, record.content_hash)
    if record.external_id is None:
        return None
    return (
        "legacy",
        record.canonical_source_id,
        record.external_id,
        record.doc_date,
    )


def _document_metadata(record: DocumentRecord) -> tuple[object, ...]:
    return (
        record.external_id,
        record.doc_date,
        record.url,
        record.raw_json_text,
    )


def build_document_mapping(
    records: Iterable[DocumentRecord],
) -> tuple[dict[int, int], int]:
    """Map only unambiguous duplicate documents to their oldest row.

    Content hashes provide the preferred identity. Legacy rows without a hash
    use canonical source, external ID, and document date. Rows without either
    identity are intentionally left untouched.
    """
    groups: dict[tuple[object, ...], list[DocumentRecord]] = defaultdict(list)
    for record in records:
        identity = _document_identity(record)
        if identity is not None:
            groups[identity].append(record)

    mapping: dict[int, int] = {}
    duplicate_groups = 0
    for identity, group in groups.items():
        if len(group) < 2:
            continue
        metadata = {_document_metadata(record) for record in group}
        if len(metadata) != 1:
            ids = sorted(record.source_document_id for record in group)
            raise UnsafeConsolidation(
                f"document identity {identity!r} has conflicting metadata "
                f"across source_document_ids {ids}"
            )
        duplicate_groups += 1
        keep_id = min(record.source_document_id for record in group)
        mapping.update(
            (record.source_document_id, keep_id)
            for record in group
            if record.source_document_id != keep_id
        )
    return mapping, duplicate_groups


def discover_foreign_keys(cursor) -> list[ForeignKeyReference]:
    """Return every FK column targeting source or source_document."""
    cursor.execute(
        """
        SELECT
            local_ns.nspname,
            local_table.relname,
            local_col.attname,
            target_table.relname,
            target_col.attname,
            constraint_row.conname,
            cardinality(constraint_row.conkey),
            cardinality(constraint_row.confkey)
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS local_table
          ON local_table.oid = constraint_row.conrelid
        JOIN pg_namespace AS local_ns
          ON local_ns.oid = local_table.relnamespace
        JOIN pg_class AS target_table
          ON target_table.oid = constraint_row.confrelid
        JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
          AS local_key(attnum, position) ON TRUE
        JOIN LATERAL unnest(constraint_row.confkey) WITH ORDINALITY
          AS target_key(attnum, position)
          ON target_key.position = local_key.position
        JOIN pg_attribute AS local_col
          ON local_col.attrelid = local_table.oid
         AND local_col.attnum = local_key.attnum
        JOIN pg_attribute AS target_col
          ON target_col.attrelid = target_table.oid
         AND target_col.attnum = target_key.attnum
        WHERE constraint_row.contype = 'f'
          AND target_table.relname IN ('source', 'source_document')
          AND pg_table_is_visible(target_table.oid)
        ORDER BY local_ns.nspname, local_table.relname,
                 constraint_row.conname, local_key.position
        """
    )
    return [ForeignKeyReference(*row) for row in cursor.fetchall()]


def validate_foreign_keys(references: Sequence[ForeignKeyReference]) -> None:
    """Refuse schemas whose provenance references cannot be repointed safely."""
    for reference in references:
        if reference.local_column_count != 1 or reference.target_column_count != 1:
            raise UnsafeConsolidation(
                f"composite provenance FK {reference.constraint_name!r} "
                "is not supported"
            )
        expected_target = {
            "source": "source_id",
            "source_document": "source_document_id",
        }.get(reference.target_table)
        if reference.target_column != expected_target:
            raise UnsafeConsolidation(
                f"unexpected target {reference.target_table}.{reference.target_column} "
                f"for FK {reference.constraint_name!r}"
            )

    found = {
        (
            reference.schema_name,
            reference.table_name,
            reference.column_name,
            reference.target_table,
            reference.target_column,
        )
        for reference in references
    }
    missing = sorted(REQUIRED_REFERENCES - found)
    if missing:
        labels = ", ".join(f"{row[0]}.{row[1]}.{row[2]}" for row in missing)
        raise UnsafeConsolidation(
            f"required provenance foreign keys were not discovered: {labels}"
        )


def discover_update_triggers(cursor) -> list[UpdateTrigger]:
    """Find user UPDATE triggers on every provenance-referencing table."""
    cursor.execute(
        """
        SELECT DISTINCT
            table_ns.nspname,
            table_row.relname,
            trigger_row.tgname,
            trigger_row.tgenabled,
            function_ns.nspname,
            function_row.proname
        FROM pg_trigger AS trigger_row
        JOIN pg_class AS table_row
          ON table_row.oid = trigger_row.tgrelid
        JOIN pg_namespace AS table_ns
          ON table_ns.oid = table_row.relnamespace
        JOIN pg_proc AS function_row
          ON function_row.oid = trigger_row.tgfoid
        JOIN pg_namespace AS function_ns
          ON function_ns.oid = function_row.pronamespace
        WHERE NOT trigger_row.tgisinternal
          AND (trigger_row.tgtype & 16) <> 0
          AND EXISTS (
              SELECT 1
              FROM pg_constraint AS fk
              JOIN pg_class AS target_table
                ON target_table.oid = fk.confrelid
              WHERE fk.contype = 'f'
                AND fk.conrelid = table_row.oid
                AND target_table.relname IN ('source', 'source_document')
                AND pg_table_is_visible(target_table.oid)
          )
        ORDER BY table_ns.nspname, table_row.relname, trigger_row.tgname
        """
    )
    return [UpdateTrigger(*row) for row in cursor.fetchall()]


def _handled_trigger_function(
    trigger: UpdateTrigger,
) -> tuple[str, str] | None:
    return HANDLED_UPDATE_TRIGGERS.get(
        (trigger.schema_name, trigger.table_name, trigger.trigger_name)
    )


def validate_update_triggers(
    triggers: Sequence[UpdateTrigger],
) -> list[UpdateTrigger]:
    """Return handled enabled triggers and refuse any unknown enabled trigger."""
    handled: list[UpdateTrigger] = []
    for trigger in triggers:
        if not trigger.is_enabled:
            continue
        expected_function = _handled_trigger_function(trigger)
        actual_function = (trigger.function_schema, trigger.function_name)
        if expected_function != actual_function:
            raise UnsafeConsolidation(
                f"enabled UPDATE trigger {trigger.label!r} is not explicitly "
                f"handled (function {trigger.function_schema}."
                f"{trigger.function_name})"
            )
        handled.append(trigger)
    return handled


def _emit_trigger_audit(
    triggers: Sequence[UpdateTrigger], emit: Callable[[str], None]
) -> None:
    enabled_count = sum(trigger.is_enabled for trigger in triggers)
    emit(
        f"update trigger audit: {len(triggers)} discovered; "
        f"{enabled_count} enabled"
    )
    for trigger in triggers:
        if not trigger.is_enabled:
            status = "disabled"
        elif _handled_trigger_function(trigger) == (
            trigger.function_schema,
            trigger.function_name,
        ):
            status = "handled; parsed_at preserved during FK repoint"
        else:
            status = "UNSAFE"
        emit(
            f"trigger audit {trigger.label}: {status} "
            f"({trigger.function_schema}.{trigger.function_name})"
        )


def _load_sources(cursor) -> list[SourceRecord]:
    cursor.execute(
        """
        SELECT source_id, source_type, source_name, version_tag
        FROM source
        ORDER BY source_id
        """
    )
    return [SourceRecord(*row) for row in cursor.fetchall()]


def _create_mapping_table(
    cursor, table_name: str, mapping: dict[int, int]
) -> None:
    cursor.execute(
        sql.SQL(
            "CREATE TEMP TABLE {} ("
            "drop_id bigint PRIMARY KEY, keep_id bigint NOT NULL"
            ") ON COMMIT DROP"
        ).format(sql.Identifier(table_name))
    )
    if mapping:
        cursor.executemany(
            sql.SQL("INSERT INTO {} (drop_id, keep_id) VALUES (%s, %s)").format(
                sql.Identifier(table_name)
            ),
            sorted(mapping.items()),
        )


def _load_documents(cursor) -> list[DocumentRecord]:
    cursor.execute(
        """
        SELECT
            document.source_document_id,
            COALESCE(source_map.keep_id, document.source_id),
            document.external_id,
            document.doc_date,
            document.url,
            document.raw_json::text,
            document.content_hash
        FROM source_document AS document
        LEFT JOIN _source_dedup_map AS source_map
          ON source_map.drop_id = document.source_id
        ORDER BY document.source_document_id
        """
    )
    return [DocumentRecord(*row) for row in cursor.fetchall()]


def _mapping_table(target_table: str) -> str:
    if target_table == "source":
        return "_source_dedup_map"
    if target_table == "source_document":
        return "_document_dedup_map"
    raise UnsafeConsolidation(f"unsupported provenance target {target_table!r}")


def _count_references(cursor, reference: ForeignKeyReference) -> int:
    query = sql.SQL(
        "SELECT count(*) FROM {}.{} AS reference_row "
        "JOIN {} AS mapping ON mapping.drop_id = reference_row.{}"
    ).format(
        sql.Identifier(reference.schema_name),
        sql.Identifier(reference.table_name),
        sql.Identifier(_mapping_table(reference.target_table)),
        sql.Identifier(reference.column_name),
    )
    cursor.execute(query)
    return int(cursor.fetchone()[0])


def _repoint_reference(cursor, reference: ForeignKeyReference) -> int:
    query = sql.SQL(
        "UPDATE {}.{} AS reference_row "
        "SET {} = mapping.keep_id FROM {} AS mapping "
        "WHERE reference_row.{} = mapping.drop_id"
    ).format(
        sql.Identifier(reference.schema_name),
        sql.Identifier(reference.table_name),
        sql.Identifier(reference.column_name),
        sql.Identifier(_mapping_table(reference.target_table)),
        sql.Identifier(reference.column_name),
    )
    cursor.execute(query)
    return cursor.rowcount


def _assert_no_dropped_references(cursor, reference: ForeignKeyReference) -> None:
    remaining = _count_references(cursor, reference)
    if remaining:
        raise UnsafeConsolidation(
            f"{remaining} rows in {reference.label} still reference duplicate IDs"
        )


def _lock_provenance_tables(
    cursor, references: Sequence[ForeignKeyReference]
) -> None:
    tables = {
        ("public", "source"),
        ("public", "source_document"),
        *((reference.schema_name, reference.table_name) for reference in references),
    }
    identifiers = [
        sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        for schema, table in sorted(tables)
    ]
    cursor.execute(
        sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
            sql.SQL(", ").join(identifiers)
        )
    )


def _set_triggers_enabled(
    cursor,
    triggers: Sequence[UpdateTrigger],
    *,
    enabled: bool,
) -> None:
    for trigger in triggers:
        if not enabled:
            action = sql.SQL("DISABLE")
        elif trigger.enabled_state == "O":
            action = sql.SQL("ENABLE")
        elif trigger.enabled_state == "R":
            action = sql.SQL("ENABLE REPLICA")
        elif trigger.enabled_state == "A":
            action = sql.SQL("ENABLE ALWAYS")
        else:
            raise UnsafeConsolidation(
                f"cannot restore trigger {trigger.label!r} from state "
                f"{trigger.enabled_state!r}"
            )
        cursor.execute(
            sql.SQL("ALTER TABLE {}.{} {} TRIGGER {}").format(
                sql.Identifier(trigger.schema_name),
                sql.Identifier(trigger.table_name),
                action,
                sql.Identifier(trigger.trigger_name),
            )
        )


def consolidate_provenance(
    connection,
    *,
    allow_write: bool = False,
    emit: Callable[[str], None] = print,
) -> ConsolidationSummary:
    """Inventory duplicates, then optionally consolidate them atomically.

    PostgreSQL applies ``ALTER TABLE ... DISABLE TRIGGER`` transactionally.
    Therefore the outer rollback restores original trigger states if a repoint
    fails after handled timestamp triggers have been suspended.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            references = discover_foreign_keys(cursor)
            validate_foreign_keys(references)
            triggers = discover_update_triggers(cursor)
            emit(f"mode: {'WRITE' if allow_write else 'DRY RUN'}")
            emit(f"foreign keys discovered: {len(references)}")
            _emit_trigger_audit(triggers, emit)
            handled_triggers = validate_update_triggers(triggers)
            if allow_write:
                _lock_provenance_tables(cursor, references)

            source_mapping, source_groups = build_source_mapping(
                _load_sources(cursor)
            )
            _create_mapping_table(cursor, "_source_dedup_map", source_mapping)

            document_mapping, document_groups = build_document_mapping(
                _load_documents(cursor)
            )
            _create_mapping_table(
                cursor, "_document_dedup_map", document_mapping
            )

            reference_rows = {
                reference.label: _count_references(cursor, reference)
                for reference in references
            }
            affected_tables = {
                (reference.schema_name, reference.table_name)
                for reference in references
                if reference_rows[reference.label]
            }
            triggers_to_suspend = [
                trigger
                for trigger in handled_triggers
                if (trigger.schema_name, trigger.table_name) in affected_tables
            ]

            emit(
                f"duplicate source groups: {source_groups}; "
                f"duplicate source rows: {len(source_mapping)}"
            )
            emit(
                f"duplicate document groups: {document_groups}; "
                f"duplicate document rows: {len(document_mapping)}"
            )
            for label, count in reference_rows.items():
                emit(f"references to repoint {label}: {count}")

            if allow_write:
                _set_triggers_enabled(
                    cursor, triggers_to_suspend, enabled=False
                )
                actual_reference_rows = {
                    reference.label: _repoint_reference(cursor, reference)
                    for reference in references
                }
                _set_triggers_enabled(
                    cursor, triggers_to_suspend, enabled=True
                )
                for reference in references:
                    _assert_no_dropped_references(cursor, reference)

                cursor.execute(
                    """
                    DELETE FROM source_document AS document
                    USING _document_dedup_map AS mapping
                    WHERE document.source_document_id = mapping.drop_id
                    """
                )
                deleted_documents = cursor.rowcount
                cursor.execute(
                    """
                    DELETE FROM source AS source_row
                    USING _source_dedup_map AS mapping
                    WHERE source_row.source_id = mapping.drop_id
                    """
                )
                deleted_sources = cursor.rowcount
                if deleted_documents != len(document_mapping):
                    raise UnsafeConsolidation(
                        "document delete count changed during consolidation"
                    )
                if deleted_sources != len(source_mapping):
                    raise UnsafeConsolidation(
                        "source delete count changed during consolidation"
                    )
                for label, count in actual_reference_rows.items():
                    emit(f"references repointed {label}: {count}")
                emit(f"duplicate document rows deleted: {deleted_documents}")
                emit(f"duplicate source rows deleted: {deleted_sources}")
                reference_rows = actual_reference_rows
                connection.commit()
                emit("transaction: committed")
            else:
                connection.rollback()
                emit("transaction: rolled back (dry run)")

        return ConsolidationSummary(
            source_groups=source_groups,
            sources_removed=len(source_mapping),
            document_groups=document_groups,
            documents_removed=len(document_mapping),
            foreign_keys_discovered=len(references),
            reference_rows=reference_rows,
            wrote=allow_write,
        )
    except Exception:
        connection.rollback()
        raise


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Commit FK repoints and duplicate-row deletes (default: dry run).",
    )
    parser.add_argument(
        "--database-url",
        help="Optional PostgreSQL URL; defaults to NEON_DATABASE_URL.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    connection = (
        psycopg2.connect(args.database_url) if args.database_url else get_connection()
    )
    try:
        consolidate_provenance(connection, allow_write=args.allow_write)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
