"""
Ingest committee APPOINTED events from House Journal files (failsafe).

Run after resolutions and CREC. For whitelisted committees only, finds
appointments in journal files and inserts only when the member is not already
actively appointed to that committee as of the event date. Re-appointments are
allowed only after an intervening REMOVED event.
"""

import os
import re
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
import math
import time
from pathlib import Path

import psycopg2
from db.connection import get_connection
from core.committees.resolver import committee_name_to_id
from core.committees.types import (
    HOUSE_AUTHORIZING_COMMITTEE_IDS,
)
from core.events.journal_parser import parse_journal_file, set_verbose, passes_quality_gates
from core.members.resolver import MemberResolver, MemberResolutionError
from ingest.event_ledger import compute_event_id_canonical
from ingest.event_state import has_active_appointment
from ingest.utils import CONGRESS_DATES
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
JOURNAL_LOG = LOG_DIR / "journal_events.log"


def _append_log_entry(entry: str):
    with JOURNAL_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.utcnow().isoformat()}Z {entry}\n")

PARSER_ID = "ingest/load_journal_events.py"
EXTRACTION_MODE = "journal"


def _sanitize_text(s: str | None) -> str:
    """Remove NUL and other bytes PostgreSQL text doesn't allow."""
    if s is None:
        return ""
    return s.replace("\x00", "").replace("\r", " ")


def _get_journal_source_id(conn, congress: int) -> int | None:
    """Get or create journal source row for this congress. Caller holds conn."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id FROM source WHERE source_type = 'journal' AND version_tag = %s",
            (f"congress_{congress}",),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                """
                INSERT INTO source (source_type, source_name, version_tag)
                VALUES ('journal', 'House Journal', %s)
                RETURNING source_id
                """,
                (f"congress_{congress}",),
            )
            insert_row = cur.fetchone()
            return insert_row[0] if insert_row else None
        return row[0]


def _ensure_connection(conn, congress: int, *, force_reconnect: bool = False):
    """
    Run a keepalive (SELECT 1) unless force_reconnect. On connection failure or
    force_reconnect, close conn, open a new one, re-fetch valid_committee_codes
    and source_id, create a new MemberResolver.
    Returns (conn, member_resolver, valid_committee_codes, source_id).
    On success (no reconnect): returns (conn, None, None, None) — caller keeps existing state.
    """
    if not force_reconnect:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            pass
        else:
            return (conn, None, None, None)
    try:
        conn.close()
    except Exception:
        pass
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT committee_code FROM committee")
        valid_committee_codes = {row[0] for row in cur.fetchall()}
    source_id = _get_journal_source_id(conn, congress)
    member_resolver = MemberResolver(conn)
    return (conn, member_resolver, valid_committee_codes, source_id)

# Base path = parent of GPO-HJOURNAL-YYYY dirs (e.g. data/journals)
_DEFAULT_JOURNAL_PARENT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL_BASE = Path(
    os.environ.get("JOURNAL_BASE", _DEFAULT_JOURNAL_PARENT / "data" / "journals")
)


def _fmt_elapsed(seconds: float) -> str:
    total = int(max(0, seconds))
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _set_worker_memory_limit_mb(limit_mb: int | None) -> None:
    """Best-effort per-process memory cap for parser workers (Linux/macOS only)."""
    if not limit_mb or limit_mb <= 0:
        return
    try:
        import resource  # Unix only

        limit_bytes = int(limit_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except Exception:
        # If unsupported, continue without hard cap.
        return


def _parse_journal_file_worker(
    path_str: str,
    worker_memory_mb: int | None = None,
    enforce_worker_memory_limit: bool = False,
) -> tuple[str, list[dict]]:
    """Process-pool worker: parse one journal PDF and return appointments."""
    if enforce_worker_memory_limit:
        _set_worker_memory_limit_mb(worker_memory_mb)
    p = Path(path_str)
    appointments = parse_journal_file(p)
    return (path_str, appointments)


def _years_for_congress(congress: int) -> list[int]:
    """Years that overlap the given congress (e.g. 113 -> [2013, 2014])."""
    if congress not in CONGRESS_DATES:
        return []
    start, end = CONGRESS_DATES[congress]
    return list(range(start.year, end.year))


def _is_main_journal_pdf(path: Path, years: list[int]) -> bool:
    """
    Keep only PDFs that are the main "JOURNAL OF THE HOUSE OF REPRESENTATIVES"
    volume. GPO names these GPO-HJOURNAL-{year}-2-{part}.pdf. Exclude volume 1
    (cover/TOC), volume 3+ (index, appendix, History of Bills, Questions of Order,
    Rules, Proceedings Subsequent to Sine Die, etc.).
    """
    stem = path.stem
    if not stem.startswith("GPO-HJOURNAL-"):
        return False
    # Strictly keep only the two main journal parts: ...-2-1 and ...-2-2
    # (exclude cover/TOC/index/appendix volumes like ...-1-*, ...-3-*).
    if not re.fullmatch(r"GPO-HJOURNAL-\d{4}-2-[12]", stem):
        return False
    try:
        year_part = stem.split("-")[2]  # YYYY from GPO-HJOURNAL-YYYY-2-*
        year = int(year_part)
        return year in years
    except (IndexError, ValueError):
        return False


def _get_gpo_journal_files(base: Path, congress: int) -> list[Path]:
    """
    Discover GPO House Journal files for this congress.

    Layout: base / GPO-HJOURNAL-{year} / pdf / *.pdf.
    Only includes main journal volume (GPO-HJOURNAL-{year}-2-*.pdf), not
    cover (2013-1.pdf), index, appendix, History of Bills, Rules, etc.
    Returns sorted list of file paths to process.
    """
    years = _years_for_congress(congress)
    paths: list[Path] = []
    for year in years:
        year_dir = base / f"GPO-HJOURNAL-{year}"
        if not year_dir.exists():
            continue
        pdf_dir = year_dir / "pdf"
        if pdf_dir.exists():
            candidates = list(pdf_dir.glob("*.pdf"))
        else:
            candidates = list(year_dir.glob("*.pdf"))
        paths.extend(p for p in candidates if _is_main_journal_pdf(p, years))
    return sorted(paths)


def load_journal_events(
    congress: int,
    base_path: Path | None = None,
    verbose: bool = False,
    dry_run: bool = False,
    chamber: str = "H",
    parse_workers: int = 1,
    parse_memory_gb: float = 0.0,
    worker_memory_gb: float = 8.0,
    enforce_worker_memory_limit: bool = False,
):
    """
    Ingest committee APPOINTED events from House journal files for the given congress.

    chamber: "H" for House (GPO-HJOURNAL). Senate journal ingestion is disabled.
    - Parses journal files for committee appointment language (whitelisted committees only).
    - Resolves member names to bioguide_id and committee text to committee_code.
    - Inserts only when no existing committee_event exists for (congress, chamber,
      bioguide_id, committee_code, action='APPOINTED'). Conflicting dates are
      disregarded: we only add journal rows for appointments we don't already have.
    """
    base = base_path or DEFAULT_JOURNAL_BASE
    if not base.exists():
        print(f"Journal base path does not exist: {base}")
        return

    if chamber != "H":
        print("Senate journal ingestion is disabled. Use House only (--chamber H).")
        return

    conn = get_connection()
    member_resolver = MemberResolver(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT committee_code FROM committee")
        valid_committee_codes = {row[0] for row in cur.fetchall()}

    whitelist = HOUSE_AUTHORIZING_COMMITTEE_IDS

    source_id = _get_journal_source_id(conn, congress)
    if source_id is None:
        print("Failed to get or create journal source; aborting.")
        conn.close()
        return

    inserted_count = 0
    files_processed = 0
    skipped_already_have = 0
    skipped_unresolved_member = 0
    skipped_untracked_committee = 0
    pending_insertions: list[dict] = []

    set_verbose(verbose)
    journal_files = _get_gpo_journal_files(base, congress)
    if not journal_files:
        print(f"No GPO-HJOURNAL files found for congress {congress} under {base}")
        conn.close()
        return

    run_chamber = chamber
    run_started = time.monotonic()
    parsed_by_file: dict[str, list[dict]] = {}
    parse_workers = max(1, int(parse_workers))
    worker_memory_mb = max(0, int(worker_memory_gb * 1024))
    if parse_memory_gb and parse_memory_gb > 0:
        budget_workers = max(1, int(math.floor(parse_memory_gb / max(worker_memory_gb, 0.25))))
        parse_workers = min(parse_workers, budget_workers)
    if parse_workers > 1 and len(journal_files) > 1:
        if verbose:
            budget_msg = (
                f", parse_memory_gb={parse_memory_gb}, worker_memory_gb={worker_memory_gb}"
                if parse_memory_gb and parse_memory_gb > 0
                else ""
            )
            print(f"  [Journal] Parallel parse enabled (workers={parse_workers}{budget_msg})")
        with ProcessPoolExecutor(max_workers=parse_workers) as ex:
            futs = {
                ex.submit(
                    _parse_journal_file_worker,
                    str(fpath),
                    worker_memory_mb if worker_memory_mb > 0 else None,
                    enforce_worker_memory_limit,
                ): str(fpath)
                for fpath in journal_files
                if fpath.is_file()
            }
            total_parse = len(futs)
            completed_parse = 0
            pending = set(futs.keys())
            last_heartbeat = time.monotonic()
            heartbeat_interval_s = 20.0
            while pending:
                done, pending = wait(pending, timeout=5.0, return_when=FIRST_COMPLETED)
                if not done:
                    if verbose and (time.monotonic() - last_heartbeat) >= heartbeat_interval_s:
                        elapsed = _fmt_elapsed(time.monotonic() - run_started)
                        print(
                            f"    [ParseProgress @ {elapsed}] completed={completed_parse}/{total_parse}, "
                            f"pending={len(pending)}",
                            flush=True,
                        )
                        last_heartbeat = time.monotonic()
                    continue
                for fut in done:
                    path_str = futs[fut]
                    try:
                        _, appointments = fut.result()
                    except Exception as e:
                        if verbose:
                            print(
                                f"    [ParseFallback] {Path(path_str).name}: worker failed ({e}); parsing inline",
                                flush=True,
                            )
                        # Leave file unset so main loop parses inline.
                        continue
                    parsed_by_file[path_str] = appointments
                    completed_parse += 1
                    if verbose:
                        elapsed = _fmt_elapsed(time.monotonic() - run_started)
                        print(
                            f"    [Parsed {completed_parse}/{total_parse} @ {elapsed}] "
                            f"{Path(path_str).name}: {len(appointments)} appointment(s)",
                            flush=True,
                        )

    for fpath in journal_files:
        if not fpath.is_file():
            continue

        if verbose:
            elapsed = _fmt_elapsed(time.monotonic() - run_started)
            print(f"  [Journal @ {elapsed}] Processing {fpath.name}")

        # Parse first; then refresh DB connection for this file's DB phase.
        appointments = parsed_by_file.get(str(fpath))
        if appointments is not None and parse_workers > 1 and len(appointments) == 0:
            # Guardrail: if a worker returns empty, verify once inline to avoid silent worker-edge failures.
            verify_started = time.monotonic()
            inline_appointments = parse_journal_file(fpath)
            if inline_appointments:
                appointments = inline_appointments
                if verbose:
                    verify_elapsed = _fmt_elapsed(time.monotonic() - verify_started)
                    print(
                        f"    [ParseVerify @ {verify_elapsed}] {fpath.name}: "
                        f"worker=0, inline={len(inline_appointments)} appointment(s)"
                    )
        if appointments is None:
            parse_started = time.monotonic()
            appointments = parse_journal_file(fpath)
            if verbose:
                parse_elapsed = _fmt_elapsed(time.monotonic() - parse_started)
                print(
                    f"    [Parsed inline @ {parse_elapsed}] {fpath.name}: "
                    f"{len(appointments)} appointment(s)"
                )
        if not appointments:
            print(
                f"  {fpath.name}: candidates_total=0 (no candidates from parser) inserted=0"
            )
            if verbose:
                print("    -> 0 appointments (parser returned none)")
            continue

        try:
            raw_text = _sanitize_text(
                fpath.read_bytes().decode("utf-8", errors="replace")[:50000]
            )
        except (UnicodeDecodeError, OSError, ValueError):
            raw_text = ""
        doc_date = appointments[0].get("decision_date")

        did_file = False
        for retry in range(4):  # allow up to 3 reconnects per file
            try:
                # Refresh connection per-file so long PDF parse does not leave stale idle session.
                conn, new_resolver, new_codes, new_sid = _ensure_connection(
                    conn, congress, force_reconnect=True
                )
                if new_resolver is not None:
                    member_resolver = new_resolver
                    valid_committee_codes = new_codes
                    source_id = new_sid
                source_document_id = None
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT source_document_id FROM source_document WHERE source_id = %s AND external_id = %s",
                            (source_id, fpath.name),
                        )
                        doc_row = cur.fetchone()
                        if not doc_row:
                            cur.execute(
                                """
                                INSERT INTO source_document (source_id, external_id, doc_date)
                                VALUES (%s, %s, %s)
                                RETURNING source_document_id
                                """,
                                (source_id, fpath.name, doc_date),
                            )
                            doc_row = cur.fetchone()
                        if doc_row:
                            source_document_id = doc_row[0]
                        else:
                            break

                file_event_count = 0
                file_skipped_committee = 0
                file_skipped_member = 0
                file_skipped_existing = 0
                # Quality-gate counters (per PDF)
                file_candidates_total = 0
                file_rejected_committee_too_long = 0
                file_rejected_committee_no_phrase = 0
                file_rejected_committee_has_bill_tokens = 0
                file_rejected_member_stopword = 0
                file_rejected_member_regex_fail = 0
                file_resolved_ok = 0
                # Refresh connection before the resolve-heavy loop (avoids SSL drop after source_document)
                conn, _resolver, _codes, _sid = _ensure_connection(conn, congress)
                if _resolver is not None:
                    member_resolver = _resolver
                    valid_committee_codes = _codes
                    source_id = _sid
                for i, appt in enumerate(appointments):
                    # Periodic keepalive so long resolve loops don't hit an idle-closed connection
                    if i > 0 and i % 25 == 0:
                        conn, _resolver, _codes, _sid = _ensure_connection(conn, congress)
                        if _resolver is not None:
                            member_resolver = _resolver
                            valid_committee_codes = _codes
                            source_id = _sid
                    chamber = appt.get("chamber") or "H"
                    congress_no = appt.get("congress") or congress
                    member_raw = _sanitize_text(appt.get("member_raw") or "").strip()
                    committee_raw = _sanitize_text(appt.get("committee_raw") or "").strip()
                    decision_date = appt.get("decision_date")
                    source_loc = _sanitize_text(appt.get("source_loc") or fpath.name)
                    text_span = _sanitize_text(appt.get("text_span") or f"{committee_raw}: {member_raw}")

                    if not member_raw or not committee_raw or not decision_date or chamber not in ("H", "S"):
                        continue

                    file_candidates_total += 1
                    passed, reject_reason = passes_quality_gates(committee_raw, member_raw)
                    if not passed:
                        if reject_reason == "committee_too_long":
                            file_rejected_committee_too_long += 1
                        elif reject_reason == "committee_no_required_phrase":
                            file_rejected_committee_no_phrase += 1
                        elif reject_reason == "committee_has_bill_tokens":
                            file_rejected_committee_has_bill_tokens += 1
                        elif reject_reason == "member_stopword":
                            file_rejected_member_stopword += 1
                        else:
                            file_rejected_member_regex_fail += 1
                        continue

                    bill_type = "hres" if chamber == "H" else "sres"
                    comm_code = committee_name_to_id(committee_raw, bill_type=bill_type)
                    if not comm_code or comm_code not in valid_committee_codes or comm_code not in whitelist:
                        skipped_untracked_committee += 1
                        file_skipped_committee += 1
                        if verbose:
                            print(f"    [Skip] Unsupported committee '{committee_raw}'")
                        continue

                    event_date_str = decision_date.isoformat() if hasattr(decision_date, "isoformat") else str(decision_date)
                    try:
                        bioguide_id = member_resolver.resolve(
                            member_raw, congress_no, chamber, event_date=event_date_str
                        )
                    except MemberResolutionError:
                        skipped_unresolved_member += 1
                        file_skipped_member += 1
                        if verbose:
                            print(f"    [Skip] Member unresolved '{member_raw}'")
                        continue

                    file_resolved_ok += 1  # passed gates + resolved member + committee

                    with conn.cursor() as cur:
                        event_date = event_date_str
                        if has_active_appointment(
                            conn, congress_no, chamber, bioguide_id, comm_code, event_date
                        ):
                            skipped_already_have += 1
                            file_skipped_existing += 1
                            continue
                        event_id = compute_event_id_canonical(
                            congress_no, chamber, bioguide_id, comm_code, "APPOINTED", event_date
                        )
                        if dry_run:
                            pending_insertions.append(
                                {
                                    "event_id": event_id,
                                    "congress": congress_no,
                                    "chamber": chamber,
                                    "bioguide_id": bioguide_id,
                                    "committee_code": comm_code,
                                    "action": "APPOINTED",
                                    "decision_date": decision_date,
                                    "effective_date": decision_date,
                                    "source_document_id": source_document_id,
                                    "source_locator": source_loc,
                                    "text_span": text_span,
                                }
                            )
                            inserted_count += 1
                            file_event_count += 1
                            _append_log_entry(
                                f"insert_event dry_run=1 file={fpath.name} congress={congress_no} chamber={chamber} "
                                f"bioguide_id={bioguide_id} committee_code={comm_code} decision_date={event_date} "
                                f"source_loc={source_loc!r} member_raw={member_raw!r}"
                            )
                        else:
                            cur.execute(
                                """
                                INSERT INTO committee_event (
                                    event_id, congress_no, chamber, bioguide_id, committee_code,
                                    action, decision_date, effective_date,
                                    source_document_id, source_locator, text_span, extraction_mode
                                ) VALUES (%s, %s, %s, %s, %s, 'APPOINTED', %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (congress_no, chamber, bioguide_id, committee_code, action, decision_date) DO NOTHING
                                """,
                                (
                                    event_id, congress_no, chamber, bioguide_id, comm_code,
                                    decision_date, decision_date, source_document_id, source_loc, text_span, EXTRACTION_MODE,
                                ),
                            )
                            if cur.rowcount:
                                inserted_count += 1
                                file_event_count += 1
                                _append_log_entry(
                                    f"insert_event dry_run=0 file={fpath.name} congress={congress_no} chamber={chamber} "
                                    f"bioguide_id={bioguide_id} committee_code={comm_code} decision_date={event_date} "
                                    f"source_loc={source_loc!r} member_raw={member_raw!r}"
                                )

                did_file = True
                files_processed += 1
                _append_log_entry(
                    f"congress={congress} file={fpath.name} inserted={file_event_count} "
                    f"skip_committee={file_skipped_committee} skip_member={file_skipped_member} "
                    f"skip_existing={file_skipped_existing}"
                )
                # One-line quality-gate summary per PDF
                print(
                    f"  {fpath.name}: candidates_total={file_candidates_total} "
                    f"rejected_committee_too_long={file_rejected_committee_too_long} "
                    f"rejected_committee_no_phrase={file_rejected_committee_no_phrase} "
                    f"rejected_committee_has_bill_tokens={file_rejected_committee_has_bill_tokens} "
                    f"rejected_member_stopword={file_rejected_member_stopword} "
                    f"rejected_member_regex_fail={file_rejected_member_regex_fail} "
                    f"resolved_ok={file_resolved_ok} inserted={file_event_count}"
                )
                if verbose:
                    print(
                        f"    [File] {fpath.name}: inserted={file_event_count} "
                        f"skip_committee={file_skipped_committee} skip_member={file_skipped_member} "
                        f"skip_existing={file_skipped_existing}"
                    )
                    elapsed = _fmt_elapsed(time.monotonic() - run_started)
                    print(
                        f"    [Progress @ {elapsed}] files_done={files_processed}/{len(journal_files)} "
                        f"inserted_total={inserted_count}"
                    )
                # Commit per file so reconnects for subsequent files cannot drop prior writes.
                if not dry_run:
                    conn.commit()
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                if retry < 3:
                    if verbose:
                        print(f"  [Journal] Connection error on {fpath.name} (attempt {retry + 1}/4), reconnecting and retrying: {e}")
                    conn, new_resolver, new_codes, new_sid = _ensure_connection(conn, congress, force_reconnect=True)
                    member_resolver = new_resolver
                    valid_committee_codes = new_codes
                    source_id = new_sid
                    continue
                raise

        if not did_file:
            continue

    # Dry run: list all pending insertions with member names (lookup before closing conn)
    if dry_run and pending_insertions:
        conn, _, _, _ = _ensure_connection(conn, congress)
        bioguide_ids = list({e["bioguide_id"] for e in pending_insertions})
        name_by_id: dict[str, str] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bioguide_id, first_name, last_name FROM member WHERE bioguide_id = ANY(%s)",
                (bioguide_ids,),
            )
            for bid, first, last in cur.fetchall():
                name_by_id[bid] = f"{first or ''} {last or ''}".strip() or bid
        print("\nDry run: no database changes. Pending insertions (would be inserted):")
        for event in pending_insertions:
            name = name_by_id.get(event["bioguide_id"], event["bioguide_id"])
            print(
                f"    - {event['decision_date']} {event['chamber']} {event['committee_code']} -> {name} ({event['bioguide_id']}) {event['source_locator']}"
            )
        print(f"  Total: {len(pending_insertions)} events.")

    conn.close()
    print("JOURNAL INGESTION COMPLETE (failsafe)")
    print(f"  Congress: {congress}  Chamber: {run_chamber}")
    print(f"  Journal files found: {len(journal_files)}")
    print(f"  Files processed (had ≥1 appointment): {files_processed}")
    print(f"  Events inserted (missing from resolutions/CREC): {inserted_count}")
    if len(journal_files) > 0 and files_processed == 0:
        print("  (No appointments extracted. Run: python -m scripts.diagnose_journal_parser -c <congress> --show-text to inspect PDF text vs. parser patterns.)")
    print(f"  Skipped (already have appointment): {skipped_already_have}")
    print(f"  Skipped (unresolved member): {skipped_unresolved_member}")
    print(f"  Skipped (untracked committee): {skipped_untracked_committee}")


def main():
    import argparse
    p = argparse.ArgumentParser(description="Load journal committee appointments (failsafe after CREC).")
    p.add_argument("-c", "--congress", type=int, default=113)
    p.add_argument("--chamber", choices=("H",), default="H", help="House only (GPO-HJOURNAL)")
    p.add_argument("--base", type=Path, default=None, help="Path to journal base (parent of GPO-HJOURNAL-* dirs)")
    p.add_argument("--verbose", "-v", action="store_true", help="Log progress while processing PDFs")
    p.add_argument("--dry-run", action="store_true", help="Do not insert; show what would be inserted (default)")
    p.add_argument("--allow-write", action="store_true", help="Allow DB writes (only after validation passes)")
    p.add_argument(
        "--parse-workers",
        type=int,
        default=int(os.environ.get("JOURNAL_PARSE_WORKERS", "1")),
        help="Number of worker processes for PDF parsing (default: 1)",
    )
    p.add_argument(
        "--parse-memory-gb",
        type=float,
        default=float(os.environ.get("JOURNAL_PARSE_MEMORY_GB", "0")),
        help="Total memory budget for parser workers in GB (0 disables budget cap).",
    )
    p.add_argument(
        "--worker-memory-gb",
        type=float,
        default=float(os.environ.get("JOURNAL_WORKER_MEMORY_GB", "8")),
        help="Per-worker memory cap in GB (best effort on Unix).",
    )
    p.add_argument(
        "--enforce-worker-memory-limit",
        action="store_true",
        help="Apply hard RLIMIT_AS per worker process (off by default).",
    )
    args = p.parse_args()
    allow_write = args.allow_write or os.environ.get("JOURNAL_ALLOW_WRITE", "").strip() == "1"
    dry_run = not allow_write
    if args.dry_run:
        dry_run = True
    load_journal_events(
        congress=args.congress,
        base_path=args.base,
        verbose=args.verbose,
        dry_run=dry_run,
        chamber=args.chamber,
        parse_workers=args.parse_workers,
        parse_memory_gb=args.parse_memory_gb,
        worker_memory_gb=args.worker_memory_gb,
        enforce_worker_memory_limit=args.enforce_worker_memory_limit,
    )


if __name__ == "__main__":
    main()
