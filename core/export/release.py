"""Canonical release export builders and snapshot queries."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.validation_policy import load_validation_policy
from db.connection import get_connection
from validate.directory_overlap import (
    DirectoryMismatch,
    DirectoryScore,
    score_database as score_directory_database,
)

from .schema import DATASET_SPECS, RELEASE_METADATA_SHEET, SCHEMA_VERSION
from .serialization import compute_semantic_workbook_hash, sha256_file, write_release_artifacts


VALIDATION_POLICY_VERSION = load_validation_policy().version
SELECT_SPECIAL_CODES = frozenset({"HLIG", "HSZS", "SLIN", "SLET", "SPAG"})


def _stable_release_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:24]}"


def make_release_source_id(
    *,
    source_type: str | None,
    source_name: str | None,
    version_tag: str | None,
) -> str:
    return _stable_release_id(
        "release-source",
        {
            "source_type": source_type or "",
            "source_name": source_name or "",
            "version_tag": version_tag or "",
        },
    )


def make_release_source_document_id(
    *,
    release_source_id: str,
    external_id: str | None,
    doc_date: date | None,
    url: str | None,
    content_hash: str | None,
) -> str:
    return _stable_release_id(
        "release-source-document",
        {
            "release_source_id": release_source_id,
            "external_id": external_id or "",
            "doc_date": doc_date.isoformat() if doc_date else "",
            "url": url or "",
            "content_hash": content_hash or "",
        },
    )


def make_release_event_id(
    *,
    congress_no: int,
    chamber: str,
    bioguide_id: str,
    committee_code: str,
    action: str,
    decision_date: date,
    effective_date: date,
    release_source_document_id: str,
    source_locator: str,
) -> str:
    return _stable_release_id(
        "release-event",
        {
            "congress_no": congress_no,
            "chamber": chamber,
            "bioguide_id": bioguide_id,
            "committee_code": committee_code,
            "action": action,
            "decision_date": decision_date.isoformat(),
            "effective_date": effective_date.isoformat(),
            "release_source_document_id": release_source_document_id,
            "source_locator": source_locator,
        },
    )


def is_standing_committee(
    *,
    chamber: str,
    committee_code: str,
    committee_name: str,
    is_joint: bool = False,
) -> bool:
    if is_joint or chamber == "J" or committee_code.startswith("J"):
        return False
    lowered_name = (committee_name or "").lower()
    if "select" in lowered_name or "special" in lowered_name:
        return False
    if committee_code in SELECT_SPECIAL_CODES:
        return False
    return True


def _row_from_cursor(cursor) -> list[dict[str, Any]]:
    headers = [description[0] for description in cursor.description]
    return [dict(zip(headers, row)) for row in cursor.fetchall()]


def _inclusive_last_active(boundary_date: date) -> date:
    return boundary_date - timedelta(days=1)


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stable_now() -> str:
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch:
        return (
            datetime.fromtimestamp(int(source_date_epoch), timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _schema_sha256() -> str:
    return sha256_file(Path("db/schema.sql"))


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _manifest_hashes() -> dict[str, str]:
    manifest_hashes: dict[str, str] = {}
    for root in (Path("manifests"), Path("data/manifests")):
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            manifest_hashes[str(path)] = sha256_file(path)
    return manifest_hashes


def build_assignment_rows(
    raw_rows: list[dict[str, Any]],
    *,
    release_version: str,
    event_id_map: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        termination_effective_date = raw["end_effective_date"] or raw["congress_end_date"]
        last_active_date = _inclusive_last_active(termination_effective_date)
        congress_last_active = _inclusive_last_active(raw["congress_end_date"])
        rows.append(
            {
                "release_version": release_version,
                "schema_version": SCHEMA_VERSION,
                "congress_no": raw["congress_no"],
                "chamber": raw["chamber"],
                "bioguide_id": raw["bioguide_id"],
                "committee_code": raw["committee_code"],
                "committee_name": raw["committee_name"],
                "start_date": raw["start_date"],
                "last_active_date": last_active_date,
                "termination_effective_date": termination_effective_date,
                "ended_early": last_active_date < congress_last_active,
                "start_release_event_id": event_id_map.get(raw["internal_start_event_id"]),
                "end_release_event_id": event_id_map.get(raw["internal_end_event_id"]),
                "internal_start_event_id": raw["internal_start_event_id"],
                "internal_end_event_id": raw["internal_end_event_id"],
            }
        )
    return rows


def build_ranking_rows(
    raw_rows: list[dict[str, Any]], *, release_version: str
) -> list[dict[str, Any]]:
    return [
        {
            "release_version": release_version,
            "schema_version": SCHEMA_VERSION,
            "congress_no": raw["congress_no"],
            "chamber": raw["chamber"],
            "bioguide_id": raw["bioguide_id"],
            "committee_code": raw["committee_code"],
            "committee_name": raw["committee_name"],
            "caucus_party_code": raw["caucus_party_code"],
            "rank_in_party": raw["rank_in_party"],
            "unresolved_slots_before": raw["unresolved_slots_before"],
            "rank_start_date": raw["rank_start_date"],
            "rank_last_active_date": _inclusive_last_active(raw["rank_end_boundary"]),
            "rank_end_boundary": raw["rank_end_boundary"],
            "rank_basis": raw["rank_basis"],
            "rank_observation_id": raw["rank_observation_id"],
            "release_source_document_id": raw["release_source_document_id"],
            "source_locator": raw["source_locator"],
            "raw_member_name": raw["raw_member_name"],
            "rank_after_raw_name": raw["rank_after_raw_name"],
            "observation_kind": raw["observation_kind"],
        }
        for raw in raw_rows
    ]


def build_event_rows(
    raw_rows: list[dict[str, Any]],
    *,
    release_version: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    event_id_map: dict[str, str] = {}
    for raw in raw_rows:
        release_event_id = make_release_event_id(
            congress_no=raw["congress_no"],
            chamber=raw["chamber"],
            bioguide_id=raw["bioguide_id"],
            committee_code=raw["committee_code"],
            action=raw["action"],
            decision_date=raw["decision_date"],
            effective_date=raw["effective_date"],
            release_source_document_id=raw["release_source_document_id"],
            source_locator=raw["source_locator"],
        )
        event_id_map[raw["internal_event_id"]] = release_event_id
        rows.append(
            {
                "release_version": release_version,
                "schema_version": SCHEMA_VERSION,
                "release_event_id": release_event_id,
                "internal_event_id": raw["internal_event_id"],
                "congress_no": raw["congress_no"],
                "chamber": raw["chamber"],
                "bioguide_id": raw["bioguide_id"],
                "committee_code": raw["committee_code"],
                "committee_name": raw["committee_name"],
                "action": raw["action"],
                "decision_date": raw["decision_date"],
                "effective_date": raw["effective_date"],
                "release_source_document_id": raw["release_source_document_id"],
                "internal_source_document_id": raw["internal_source_document_id"],
                "source_locator": raw["source_locator"],
                "text_span": raw["text_span"],
                "extraction_mode": raw["extraction_mode"],
                "note_types": raw["note_types"],
                "interpretation_basis": raw["interpretation_basis"],
            }
        )
    return rows, event_id_map


def build_member_rows(raw_rows: list[dict[str, Any]], *, release_version: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        rows.append(
            {
                "release_version": release_version,
                "schema_version": SCHEMA_VERSION,
                "bioguide_id": raw["bioguide_id"],
                "congress_no": raw["congress_no"],
                "chamber": raw["chamber"],
                "service_start": raw["service_start"],
                "service_last_active_date": _inclusive_last_active(raw["service_end_boundary"]),
                "first_name": raw["first_name"],
                "last_name": raw["last_name"],
                "official_full_name": raw["official_full_name"],
                "nickname": raw["nickname"],
                "state": raw["state"],
                "district": raw["district"],
                "party_code": raw["party_code"],
                "caucus_party_code": raw["caucus_party_code"],
                "exit_reason": raw["exit_reason"],
            }
        )
    return rows


def build_committee_rows(raw_rows: list[dict[str, Any]], *, release_version: str) -> list[dict[str, Any]]:
    return [
        {
            "release_version": release_version,
            "schema_version": SCHEMA_VERSION,
            "committee_code": raw["committee_code"],
            "chamber": raw["chamber"],
            "committee_name": raw["committee_name"],
            "valid_start": raw["valid_start"],
            "valid_last_active_date": raw["valid_last_active_date"],
            "is_joint": raw["is_joint"],
        }
        for raw in raw_rows
    ]


def build_source_rows(raw_rows: list[dict[str, Any]], *, release_version: str) -> list[dict[str, Any]]:
    return [
        {
            "release_version": release_version,
            "schema_version": SCHEMA_VERSION,
            "release_source_id": raw["release_source_id"],
            "internal_source_id": raw["internal_source_id"],
            "release_source_document_id": raw["release_source_document_id"],
            "internal_source_document_id": raw["internal_source_document_id"],
            "source_type": raw["source_type"],
            "source_name": raw["source_name"],
            "version_tag": raw["version_tag"],
            "external_id": raw["external_id"],
            "doc_date": raw["doc_date"],
            "url": raw["url"],
            "content_hash": raw["content_hash"],
            "retrieved_at_utc": None,
            "created_at_utc": None,
        }
        for raw in raw_rows
    ]


def _directory_score_row(
    score: DirectoryScore,
    *,
    release_version: str,
    validation_policy_version: str,
) -> dict[str, Any]:
    return {
        "release_version": release_version,
        "schema_version": SCHEMA_VERSION,
        "validation_policy_version": validation_policy_version,
        "validation_type": "directory_overlap",
        "congress_no": score.congress_no,
        "chamber": score.chamber,
        "snapshot_date": score.snapshot_date,
        "committee_scope": score.committee_scope,
        "event_type": None,
        "gate_status": score.gate_status,
        "reference_count": score.resolved_directory_assignments,
        "observed_count": score.observed_assignments,
        "overlap_count": score.overlap_assignments,
        "reference_only_count": score.directory_only_assignments,
        "observed_only_count": score.observed_only_assignments,
        "directory_member_entries": score.directory_member_entries,
        "resolved_directory_assignments": score.resolved_directory_assignments,
        "unresolved_member_entries": score.unresolved_member_entries,
        "unmapped_committee_entries": score.unmapped_committee_entries,
        "unmapped_committees": score.unmapped_committees,
        "member_resolution_pct": score.member_resolution_pct,
        "reference_coverage_pct": None,
        "directory_coverage_pct": score.directory_coverage_pct,
        "observed_overlap_pct": score.observed_overlap_pct,
        "date_comparable_count": None,
        "exact_date_match_count": None,
        "exact_date_match_pct": None,
        "within_one_day_match_count": None,
        "within_one_day_match_pct": None,
    }


def build_validation_rows(
    *,
    directory_scores: list[DirectoryScore],
    release_version: str,
    validation_policy_version: str,
) -> list[dict[str, Any]]:
    rows = [
        _directory_score_row(
            score,
            release_version=release_version,
            validation_policy_version=validation_policy_version,
        )
        for score in directory_scores
    ]
    return sorted(
        rows,
        key=lambda row: (
            row["validation_type"],
            row["congress_no"],
            row["chamber"],
            row["snapshot_date"] or date.min,
            row["committee_scope"] or "",
            row["event_type"] or "",
        ),
    )


def build_directory_mismatch_rows(
    mismatches: list[DirectoryMismatch], *, release_version: str
) -> list[dict[str, Any]]:
    rows = [
        {
            "release_version": release_version,
            "schema_version": SCHEMA_VERSION,
            **mismatch.as_row(),
            "snapshot_date": mismatch.snapshot_date,
        }
        for mismatch in mismatches
    ]
    return sorted(
        rows,
        key=lambda row: (
            row["congress_no"],
            row["snapshot_date"],
            row["chamber"],
            row["committee_scope"],
            row["side"],
            row["bioguide_id"],
            row["committee_code"],
            row["raw_member_name"],
        ),
    )


def build_data_dictionary_rows(*, congress_from: int, congress_to: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in (
        "assignments",
        "rankings",
        "events",
        "members",
        "committees",
        "sources",
        "validation",
        "directory_mismatches",
        "data_dictionary",
        "release_metadata",
    ):
        spec = DATASET_SPECS[key]
        csv_file = (
            f"{spec.filename_stem}_{congress_from}_{congress_to}.csv"
            if spec.filename_stem
            else None
        )
        for column in spec.columns:
            rows.append(
                {
                    "sheet_name": spec.sheet_name,
                    "column_name": column.name,
                    "csv_file": csv_file,
                    "data_type": column.data_type,
                    "nullable": column.nullable,
                    "description": column.description,
                    "null_rule": "Blank when not applicable or not recorded." if column.nullable else "Never blank.",
                    "date_semantics": column.date_semantics or None,
                    "allowed_values": "; ".join(column.allowed_values) if column.allowed_values else None,
                }
            )
    return rows


def build_release_metadata_rows(metadata: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for field, value in metadata.items():
        if isinstance(value, dict):
            rendered = "; ".join(f"{key}={value[key]}" for key in sorted(value))
        elif isinstance(value, list):
            rendered = "; ".join(str(item) for item in value)
        else:
            rendered = "" if value is None else str(value)
        rows.append({"field": field, "value": rendered})
    return rows


def _filter_standing_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in raw_rows
        if is_standing_committee(
            chamber=row["chamber"],
            committee_code=row["committee_code"],
            committee_name=row["committee_name"],
            is_joint=bool(row.get("is_joint")),
        )
    ]


def _load_assignments(conn, congress_from: int, congress_to: int) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              cm.congress_no,
              c.chamber,
              cm.bioguide_id,
              cm.committee_code,
              COALESCE(cnh.name, cm.committee_code) AS committee_name,
              lower(cm.valid_daterange)::date AS start_date,
              cg.end_date AS congress_end_date,
              cm.start_event_id AS internal_start_event_id,
              cm.end_event_id AS internal_end_event_id,
              se.decision_date AS start_decision_date,
              ee.decision_date AS end_decision_date,
              ee.effective_date AS end_effective_date,
              c.is_joint
            FROM committee_membership cm
            JOIN committee c
              ON c.committee_code = cm.committee_code
            JOIN congress cg
              ON cg.congress_no = cm.congress_no
            LEFT JOIN committee_event se
              ON se.event_id = cm.start_event_id
            LEFT JOIN committee_event ee
              ON ee.event_id = cm.end_event_id
            LEFT JOIN LATERAL (
              SELECT cnh.name
              FROM committee_name_history cnh
              WHERE cnh.committee_code = cm.committee_code
                AND lower(cm.valid_daterange) <@ cnh.valid_daterange
              ORDER BY lower(cnh.valid_daterange) DESC
              LIMIT 1
            ) cnh ON TRUE
            WHERE cm.congress_no BETWEEN %s AND %s
            ORDER BY
              cm.congress_no,
              c.chamber,
              cm.bioguide_id,
              cm.committee_code,
              lower(cm.valid_daterange)
            """,
            (congress_from, congress_to),
        )
        return _filter_standing_rows(_row_from_cursor(cursor))


def _load_events(conn, congress_from: int, congress_to: int) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              ce.event_id AS internal_event_id,
              ce.congress_no,
              ce.chamber,
              ce.bioguide_id,
              ce.committee_code,
              COALESCE(cnh.name, ce.committee_code) AS committee_name,
              ce.action,
              ce.decision_date,
              ce.effective_date,
              ce.source_document_id AS internal_source_document_id,
              ce.source_locator,
              ce.text_span,
              ce.extraction_mode,
              string_agg(DISTINCT cen.note_type, '; ' ORDER BY cen.note_type) AS note_types,
              string_agg(DISTINCT cen.interpretation_basis, ' || ' ORDER BY cen.interpretation_basis) AS interpretation_basis,
              c.is_joint
            FROM committee_event ce
            JOIN committee c
              ON c.committee_code = ce.committee_code
            LEFT JOIN committee_event_note cen
              ON cen.event_id = ce.event_id
            LEFT JOIN LATERAL (
              SELECT cnh.name
              FROM committee_name_history cnh
              WHERE cnh.committee_code = ce.committee_code
                AND ce.effective_date <@ cnh.valid_daterange
              ORDER BY lower(cnh.valid_daterange) DESC
              LIMIT 1
            ) cnh ON TRUE
            WHERE ce.congress_no BETWEEN %s AND %s
            GROUP BY
              ce.event_id,
              ce.congress_no,
              ce.chamber,
              ce.bioguide_id,
              ce.committee_code,
              cnh.name,
              ce.action,
              ce.decision_date,
              ce.effective_date,
              ce.source_document_id,
              ce.source_locator,
              ce.text_span,
              ce.extraction_mode,
              c.is_joint
            ORDER BY
              ce.congress_no,
              ce.chamber,
              ce.bioguide_id,
              ce.committee_code,
              ce.decision_date,
              ce.action,
              ce.event_id
            """,
            (congress_from, congress_to),
        )
        return _filter_standing_rows(_row_from_cursor(cursor))


def _load_rankings(conn, congress_from: int, congress_to: int) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              cm.congress_no,
              c.chamber,
              cm.bioguide_id,
              cm.committee_code,
              COALESCE(cnh.name, cm.committee_code) AS committee_name,
              cmr.caucus_party_code,
              cmr.rank_in_party,
              cmr.unresolved_slots_before,
              lower(cmr.valid_daterange)::date AS rank_start_date,
              upper(cmr.valid_daterange)::date AS rank_end_boundary,
              cmr.rank_basis,
              cro.rank_observation_id,
              cro.source_document_id AS internal_source_document_id,
              cro.source_locator,
              cro.raw_member_name,
              cro.rank_after_raw_name,
              cro.observation_kind,
              c.is_joint
            FROM committee_membership_rank cmr
            JOIN committee_membership cm
              ON cm.committee_membership_id = cmr.committee_membership_id
            JOIN committee c
              ON c.committee_code = cm.committee_code
            JOIN committee_rank_observation cro
              ON cro.rank_observation_id = cmr.source_rank_observation_id
            LEFT JOIN LATERAL (
              SELECT cnh.name
              FROM committee_name_history cnh
              WHERE cnh.committee_code = cm.committee_code
                AND lower(cmr.valid_daterange) <@ cnh.valid_daterange
              ORDER BY lower(cnh.valid_daterange) DESC
              LIMIT 1
            ) cnh ON TRUE
            WHERE cm.congress_no BETWEEN %s AND %s
            ORDER BY cm.congress_no, c.chamber, cm.committee_code,
                     cmr.caucus_party_code, lower(cmr.valid_daterange),
                     cmr.rank_in_party, cm.bioguide_id
            """,
            (congress_from, congress_to),
        )
        return _filter_standing_rows(_row_from_cursor(cursor))


def _load_members(
    conn,
    congress_from: int,
    congress_to: int,
    bioguide_ids: list[str],
) -> list[dict[str, Any]]:
    if not bioguide_ids:
        return []
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              ms.bioguide_id,
              ms.congress_no,
              ms.chamber,
              lower(ms.valid_daterange)::date AS service_start,
              CASE
                WHEN upper_inf(ms.valid_daterange) THEN cg.end_date
                ELSE upper(ms.valid_daterange)::date
              END AS service_end_boundary,
              m.first_name,
              m.last_name,
              m.official_full_name,
              m.nickname,
              ms.state,
              ms.district,
              ms.party_code,
              ms.caucus_party_code,
              ms.exit_reason
            FROM member_service ms
            JOIN congress cg
              ON cg.congress_no = ms.congress_no
            JOIN member m
              ON m.bioguide_id = ms.bioguide_id
            WHERE ms.congress_no BETWEEN %s AND %s
              AND ms.bioguide_id = ANY(%s)
            ORDER BY
              ms.congress_no,
              ms.chamber,
              ms.bioguide_id,
              lower(ms.valid_daterange)
            """,
            (congress_from, congress_to, bioguide_ids),
        )
        return _row_from_cursor(cursor)


def _load_committees(conn, committee_codes: list[str]) -> list[dict[str, Any]]:
    if not committee_codes:
        return []
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              c.committee_code,
              c.chamber,
              cnh.name AS committee_name,
              lower(cnh.valid_daterange)::date AS valid_start,
              CASE
                WHEN upper_inf(cnh.valid_daterange) THEN NULL
                ELSE (upper(cnh.valid_daterange) - INTERVAL '1 day')::date
              END AS valid_last_active_date,
              c.is_joint
            FROM committee c
            JOIN committee_name_history cnh
              ON cnh.committee_code = c.committee_code
            WHERE c.committee_code = ANY(%s)
            ORDER BY c.committee_code, lower(cnh.valid_daterange)
            """,
            (committee_codes,),
        )
        return _filter_standing_rows(_row_from_cursor(cursor))


def _load_sources(conn, source_document_ids: list[int]) -> list[dict[str, Any]]:
    if not source_document_ids:
        return []
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              sd.source_document_id AS internal_source_document_id,
              sd.source_id AS internal_source_id,
              s.source_type,
              s.source_name,
              s.version_tag,
              sd.external_id,
              sd.doc_date,
              sd.url,
              sd.content_hash,
              s.retrieved_at AS retrieved_at_utc,
              sd.created_at AS created_at_utc
            FROM source_document sd
            LEFT JOIN source s
              ON s.source_id = sd.source_id
            WHERE sd.source_document_id = ANY(%s)
            ORDER BY sd.source_document_id
            """,
            (source_document_ids,),
        )
        raw_rows = _row_from_cursor(cursor)
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        release_source_id = make_release_source_id(
            source_type=raw["source_type"],
            source_name=raw["source_name"],
            version_tag=raw["version_tag"],
        )
        raw["release_source_id"] = release_source_id
        raw["release_source_document_id"] = make_release_source_document_id(
            release_source_id=release_source_id,
            external_id=raw["external_id"],
            doc_date=raw["doc_date"],
            url=raw["url"],
            content_hash=raw["content_hash"],
        )
        rows.append(raw)
    return rows


def _build_release_payload(
    conn,
    *,
    congress_from: int,
    congress_to: int,
    release_version: str,
    git_commit: str | None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    assignment_raw = _load_assignments(conn, congress_from, congress_to)
    event_raw = _load_events(conn, congress_from, congress_to)
    ranking_raw = _load_rankings(conn, congress_from, congress_to)

    bioguide_ids = sorted(
        {row["bioguide_id"] for row in assignment_raw}
        | {row["bioguide_id"] for row in ranking_raw}
    )
    committee_codes = sorted(
        {row["committee_code"] for row in assignment_raw}
        | {row["committee_code"] for row in event_raw}
        | {row["committee_code"] for row in ranking_raw}
    )
    source_document_ids = sorted(
        {row["internal_source_document_id"] for row in event_raw}
        | {row["internal_source_document_id"] for row in ranking_raw}
    )

    member_raw = _load_members(conn, congress_from, congress_to, bioguide_ids)
    committee_raw = _load_committees(conn, committee_codes)
    source_raw = _load_sources(conn, source_document_ids)
    source_document_id_map = {
        row["internal_source_document_id"]: row["release_source_document_id"]
        for row in source_raw
    }
    for row in event_raw:
        row["release_source_document_id"] = source_document_id_map[row["internal_source_document_id"]]
    for row in ranking_raw:
        row["release_source_document_id"] = source_document_id_map[
            row["internal_source_document_id"]
        ]

    events, event_id_map = build_event_rows(event_raw, release_version=release_version)
    assignments = build_assignment_rows(
        assignment_raw,
        release_version=release_version,
        event_id_map=event_id_map,
    )
    rankings = build_ranking_rows(ranking_raw, release_version=release_version)
    members = build_member_rows(member_raw, release_version=release_version)
    committees = build_committee_rows(committee_raw, release_version=release_version)
    sources = build_source_rows(source_raw, release_version=release_version)

    policy = load_validation_policy()
    congresses = set(range(congress_from, congress_to + 1))
    directory_congresses = sorted(congresses & set(policy.directory_congresses))
    directory_scores, directory_mismatches = score_directory_database(
        conn,
        directory_congresses,
        minimum_directory_coverage=policy.minimum_directory_coverage,
        minimum_member_resolution=policy.minimum_member_resolution,
        committee_scopes=set(policy.directory_committee_scopes),
    )
    validation = build_validation_rows(
        directory_scores=directory_scores,
        release_version=release_version,
        validation_policy_version=policy.version,
    )
    directory_mismatch_rows = build_directory_mismatch_rows(
        directory_mismatches, release_version=release_version
    )

    file_rows: dict[str, list[dict[str, Any]]] = {
        "assignments": assignments,
        "rankings": rankings,
        "events": events,
        "members": members,
        "committees": committees,
        "sources": sources,
        "validation": validation,
        "directory_mismatches": directory_mismatch_rows,
    }

    dictionary_rows = build_data_dictionary_rows(congress_from=congress_from, congress_to=congress_to)
    metadata_base = {
        "release_version": release_version,
        "schema_version": SCHEMA_VERSION,
        "validation_policy_version": policy.version,
        "scope": "standing",
        "congress_from": congress_from,
        "congress_to": congress_to,
        "git_commit": git_commit,
        "generated_at_utc": _stable_now(),
        "database_schema_sha256": _schema_sha256(),
        "manifest_hashes": _manifest_hashes(),
        "row_counts": {key: len(value) for key, value in file_rows.items()},
        "csv_filenames": {
            key: f"{DATASET_SPECS[key].filename_stem}_{congress_from}_{congress_to}.csv"
            for key in (
                "assignments",
                "rankings",
                "events",
                "members",
                "committees",
                "sources",
                "validation",
                "directory_mismatches",
            )
        },
        "workbook_filename": f"committee_membership_{congress_from}_{congress_to}.xlsx",
        "workbook_semantic_sha256": "",
    }
    metadata_rows = build_release_metadata_rows(metadata_base)

    sheet_rows = {
        DATASET_SPECS["assignments"].sheet_name: assignments,
        DATASET_SPECS["rankings"].sheet_name: rankings,
        DATASET_SPECS["events"].sheet_name: events,
        DATASET_SPECS["members"].sheet_name: members,
        DATASET_SPECS["committees"].sheet_name: committees,
        DATASET_SPECS["sources"].sheet_name: sources,
        DATASET_SPECS["validation"].sheet_name: validation,
        DATASET_SPECS["data_dictionary"].sheet_name: dictionary_rows,
        RELEASE_METADATA_SHEET: metadata_rows,
    }
    metadata_base["workbook_semantic_sha256"] = compute_semantic_workbook_hash(sheet_rows)
    metadata_rows = build_release_metadata_rows(metadata_base)
    file_rows["data_dictionary"] = dictionary_rows
    file_rows["release_metadata"] = metadata_rows
    return file_rows, metadata_base


def export_release(
    *,
    congress_from: int,
    congress_to: int,
    output_dir: Path,
    release_version: str,
    database_url: str | None = None,
    git_commit: str | None = None,
) -> dict[str, Any]:
    if congress_from > congress_to:
        raise ValueError("congress_from must be less than or equal to congress_to")
    if database_url:
        os.environ["NEON_DATABASE_URL"] = database_url
    conn = get_connection()
    try:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True, deferrable=True, autocommit=False)
        git_commit = git_commit or _git_commit()
        with conn:
            file_rows, metadata = _build_release_payload(
                conn,
                congress_from=congress_from,
                congress_to=congress_to,
                release_version=release_version,
                git_commit=git_commit,
            )
        written_files = write_release_artifacts(
            output_dir,
            file_rows,
            congress_from=congress_from,
            congress_to=congress_to,
            release_metadata=metadata,
        )
        metadata["artifact_files"] = {
            filename: info
            for filename, info in written_files.items()
            if filename not in {"release_metadata.json", "SHA256SUMS"}
        }
        metadata_path = output_dir / "release_metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written_files["release_metadata.json"]["sha256"] = sha256_file(metadata_path)
        sha256sums_path = output_dir / "SHA256SUMS"
        lines = [
            f"{info['sha256']}  {filename}"
            for filename, info in sorted(written_files.items())
            if filename != "SHA256SUMS"
        ]
        sha256sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written_files["SHA256SUMS"]["sha256"] = sha256_file(sha256sums_path)
        return metadata
    finally:
        conn.close()
