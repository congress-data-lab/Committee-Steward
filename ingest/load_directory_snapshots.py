"""
Load committee_memberships_*.json from data/congressional_directories into
congressional_directory_entry as external validation snapshots.

One row per committee per file. Re-running the same file replaces that snapshot
(congress_no + publication_date) so loads are idempotent.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

# Project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.connection import get_connection

PARSER_ID = "load_directory_snapshots"
CHAMBER_MAP = {"House": "H", "Senate": "S"}


def congress_from_folder(folder_name: str) -> int | None:
    """e.g. '113th' -> 113, '114th' -> 114."""
    m = re.match(r"(\d+)th", folder_name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def publication_date_from_filename(filename: str, congress_no: int | None = None) -> date | None:
    """Derive the best available publication date encoded in a filename."""
    exact_m = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", filename)
    if exact_m:
        return date(*(int(part) for part in exact_m.groups()))
    # First 4-digit year
    year_m = re.search(r"(20\d{2})", filename)
    if year_m:
        year = int(year_m.group(1))
        # Optional month hint (e.g. 2018_oct -> October)
        if "oct" in filename.lower() or "_10" in filename or "10_" in filename:
            return date(year, 10, 1)
        return date(year, 1, 1)
    # No year in filename (e.g. 118th_committee_memberships_output.json): use congress-based default
    if congress_no is not None:
        # 118th -> 2024, 119th -> 2026, etc.
        year = 2024 if congress_no >= 118 else (1986 + 2 * congress_no)
        return date(year, 1, 1)
    return None


def _publication_date_from_manifest(path: Path, congress_no: int) -> date | None:
    manifest_names = (
        Path("data/manifests") / f"manifest_{congress_no}.csv",
        Path("manifests") / f"{congress_no}.csv",
    )
    manifest_path = next(
        (
            ancestor / relative
            for ancestor in path.parents
            for relative in manifest_names
            if (ancestor / relative).is_file()
        ),
        None,
    )
    if manifest_path is None:
        return None

    with manifest_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    normalized = sorted(
        (
            row
            for row in rows
            if row.get("expected_type") == "directory_snapshot_normalized"
        ),
        key=lambda row: row.get("local_path", ""),
    )
    matching_index = next(
        (
            index
            for index, row in enumerate(normalized)
            if Path(row.get("local_path", "")).name == path.name
        ),
        None,
    )
    if matching_index is None:
        return None
    explicit_date = normalized[matching_index].get("date_issued", "")
    if explicit_date:
        return date.fromisoformat(explicit_date)

    source_dates = sorted(
        date.fromisoformat(row["date_issued"])
        for row in rows
        if row.get("expected_type") == "directory_snapshot"
        and row.get("date_issued")
    )
    if matching_index < len(source_dates):
        return source_dates[matching_index]
    return None


def publication_date_from_path(path: Path, congress_no: int | None = None) -> date | None:
    """Use an adjacent official CDIR artifact to recover the exact snapshot date."""
    fallback = publication_date_from_filename(path.name, congress_no)
    year = fallback.year if fallback else None
    month = fallback.month if fallback and fallback.month != 1 else None
    candidates: list[date] = []
    for sibling in path.parent.iterdir():
        match = re.search(r"CDIR-(20\d{2})-(\d{2})-(\d{2})", sibling.name, re.IGNORECASE)
        if not match:
            continue
        candidate = date(*(int(part) for part in match.groups()))
        if year is None or candidate.year == year:
            candidates.append(candidate)
    if month is not None:
        month_matches = [candidate for candidate in candidates if candidate.month == month]
        if month_matches:
            return min(month_matches)
    if candidates:
        return min(candidates)
    if congress_no is not None:
        manifest_date = _publication_date_from_manifest(path, congress_no)
        if manifest_date is not None:
            return manifest_date
    return fallback


def load_file(path: Path, congress_no: int, publication_date: date, parsed_source: str) -> list[dict]:
    """Read JSON and build rows for congressional_directory_entry. Supports both name/type and committee/chamber formats."""
    with open(path, encoding="utf-8") as f:
        committees = json.load(f)
    if not isinstance(committees, list):
        committees = [committees]
    rows = []
    for committee in committees:
        # 113th–117th: "name", "type" (House/Senate); 118th: "committee", "chamber"
        name = committee.get("name") or committee.get("committee") or ""
        raw_type = committee.get("type") or committee.get("chamber") or "House"
        if isinstance(raw_type, str) and raw_type.upper() in ("H", "S"):
            raw_type = "House" if raw_type.upper() == "H" else "Senate"
        chamber = CHAMBER_MAP.get(raw_type, "H")
        committee_text = name.strip()
        if not committee_text:
            continue
        normalized_committees = {
            "name": name,
            "type": raw_type,
            "democrats": committee.get("democrats") or [],
            "republicans": committee.get("republicans") or [],
        }
        raw_text = json.dumps(committee, ensure_ascii=False)
        rows.append({
            "congress_no": congress_no,
            "chamber": chamber,
            "bioguide_id": None,
            "committee_text": committee_text,
            "normalized_committees": json.dumps(normalized_committees, ensure_ascii=False),
            "publication_date": publication_date,
            "page_reference": path.name,
            "raw_text": raw_text,
            "parsed_source": parsed_source,
            "parser_id": PARSER_ID,
        })
    return rows


def upsert_snapshot(conn, congress_no: int, publication_date: date, rows: list[dict]) -> int:
    """Replace all entries for this (congress_no, publication_date) then insert rows."""
    parsed_sources = sorted({row["parsed_source"] for row in rows})
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM congressional_directory_entry
            WHERE congress_no = %s
              AND (publication_date = %s OR parsed_source = ANY(%s))
            """,
            (congress_no, publication_date, parsed_sources),
        )
        deleted = cur.rowcount
    if not rows:
        return deleted
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO congressional_directory_entry (
                congress_no, chamber, bioguide_id, committee_text, normalized_committees,
                publication_date, page_reference, raw_text, parsed_source, parser_id
            ) VALUES (
                %(congress_no)s, %(chamber)s, %(bioguide_id)s, %(committee_text)s,
                %(normalized_committees)s::jsonb, %(publication_date)s, %(page_reference)s,
                %(raw_text)s, %(parsed_source)s, %(parser_id)s
            )
            """,
            rows,
        )
        inserted = len(rows)
    conn.commit()
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Load directory committee_memberships JSON into congressional_directory_entry")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Paths to committee_memberships_*.json files or directories to scan",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("data/congressional_directories"),
        help="Base directory to scan for committee_memberships_*.json if no paths given",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would be loaded, do not write to DB")
    args = parser.parse_args()

    if args.paths:
        files = []
        for p in args.paths:
            p = Path(p)
            if p.is_file() and p.suffix.lower() == ".json" and ("committee_memberships" in p.name or "118th" in p.name):
                files.append(p)
            elif p.is_dir():
                files.extend(p.glob("**/committee_memberships*.json"))
                files.extend(p.glob("**/118th*.json"))
        files = sorted(set(files))
    else:
        files = sorted(args.dir.glob("**/committee_memberships*.json"))
        files += sorted(args.dir.glob("**/118th*.json"))
        files = sorted(set(files))

    if not files:
        print("No committee_memberships*.json or 118th*.json files found.", file=sys.stderr)
        sys.exit(1)

    base = Path(".").resolve()
    total_inserted = 0
    for path in files:
        path = path.resolve()
        # e.g. .../113th/committee_memberships_2014.json -> 113th
        parent_name = path.parent.name
        congress_no = congress_from_folder(parent_name)
        if congress_no is None:
            print(f"Skip (cannot parse congress from folder): {path}", file=sys.stderr)
            continue
        pub_date = publication_date_from_path(path, congress_no)
        if pub_date is None:
            print(f"Skip (cannot parse publication date from filename): {path}", file=sys.stderr)
            continue
        try:
            parsed_source = str(path.relative_to(base))
        except ValueError:
            parsed_source = str(path)
        rows = load_file(path, congress_no, pub_date, parsed_source)
        if args.dry_run:
            print(f"Would load {path}: congress={congress_no} publication_date={pub_date} rows={len(rows)}")
            total_inserted += len(rows)
            continue
        conn = get_connection()
        try:
            n = upsert_snapshot(conn, congress_no, pub_date, rows)
            print(f"{path}: {n} rows")
            total_inserted += n
        finally:
            conn.close()

    if args.dry_run:
        print(f"Dry run: would insert {total_inserted} rows total")
    else:
        print(f"Done. Inserted {total_inserted} rows.")


if __name__ == "__main__":
    main()
