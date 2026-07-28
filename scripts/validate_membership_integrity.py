"""
Validate committee_membership integrity and export flagged rows.

Checks:
1) start/end event action mismatches (APPOINTED/REMOVED)
2) start/end event member/committee mismatches
3) date alignment (appointment_date vs start_date, termination_date vs end_date)
4) one-day intervals
5) text committee mismatch (membership committee absent from start_event text_span)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.committees.resolver import committee_name_to_id
from db.connection import get_connection


SQL = """
SELECT
  cm.committee_membership_id,
  cm.congress_no,
  c.chamber,
  cm.bioguide_id,
  cm.committee_code,
  lower(cm.valid_daterange)::date AS start_date,
  CASE WHEN upper_inf(cm.valid_daterange) THEN NULL ELSE upper(cm.valid_daterange)::date END AS end_date,
  cm.start_event_id,
  cm.end_event_id,
  se.action AS start_action,
  se.bioguide_id AS start_bioguide_id,
  se.committee_code AS start_committee_code,
  se.decision_date AS appointment_date,
  se.text_span AS start_text_span,
  se.extraction_mode AS start_extraction_mode,
  ee.action AS end_action,
  ee.bioguide_id AS end_bioguide_id,
  ee.committee_code AS end_committee_code,
  ee.effective_date AS termination_date
FROM committee_membership cm
JOIN committee c
  ON c.committee_code = cm.committee_code
LEFT JOIN committee_event se
  ON se.event_id = cm.start_event_id
LEFT JOIN committee_event ee
  ON ee.event_id = cm.end_event_id
WHERE (%s::int IS NULL OR cm.congress_no = %s::int)
  AND (%s::text IS NULL OR c.chamber = %s::text)
ORDER BY cm.congress_no, c.chamber, cm.bioguide_id, cm.committee_code, cm.committee_membership_id;
"""


COMMITTEE_RE = re.compile(
    r"(?i)\bCommittee\s+(?:on|of)\s+(?:the\s+)?(.+?)(?:\s+-|\u2014|[.;]|$)"
)


def _parse_committee_codes_from_text_span(
    text_span: str | None, chamber: str
) -> set[str]:
    if not text_span:
        return set()
    bill_type = "hres" if chamber == "H" else "sres"
    codes: set[str] = set()
    for match in COMMITTEE_RE.finditer(text_span):
        committee_name = match.group(1).strip(" ,")
        code = committee_name_to_id(committee_name, bill_type=bill_type)
        if code:
            codes.add(code)
    return codes


def run_validation(congress: int | None, chamber: str | None) -> list[dict]:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(SQL, (congress, congress, chamber, chamber))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    conn.close()

    out: list[dict] = []
    for row in rows:
        rec = dict(zip(cols, row))
        issues: list[str] = []

        if rec["start_event_id"]:
            if rec["start_action"] != "APPOINTED":
                issues.append("start_action_not_appointed")
            if rec["start_bioguide_id"] and rec["start_bioguide_id"] != rec["bioguide_id"]:
                issues.append("start_event_member_mismatch")
            if rec["start_committee_code"] and rec["start_committee_code"] != rec["committee_code"]:
                issues.append("start_event_committee_mismatch")
            if rec["appointment_date"] and rec["start_date"] and rec["appointment_date"] != rec["start_date"]:
                issues.append("appointment_date_start_date_mismatch")

            if rec.get("start_extraction_mode") != "journal":
                parsed_codes = _parse_committee_codes_from_text_span(
                    rec.get("start_text_span"), rec["chamber"]
                )
            else:
                parsed_codes = set()
            if parsed_codes and rec["committee_code"] not in parsed_codes:
                issues.append("start_text_committee_mismatch")
                rec["start_text_parsed_committee_code"] = ";".join(
                    sorted(parsed_codes)
                )
            else:
                rec["start_text_parsed_committee_code"] = None
        else:
            rec["start_text_parsed_committee_code"] = None

        if rec["end_event_id"]:
            if rec["end_action"] != "REMOVED":
                issues.append("end_action_not_removed")
            if rec["end_bioguide_id"] and rec["end_bioguide_id"] != rec["bioguide_id"]:
                issues.append("end_event_member_mismatch")
            if rec["end_committee_code"] and rec["end_committee_code"] != rec["committee_code"]:
                issues.append("end_event_committee_mismatch")
            if rec["termination_date"] and rec["end_date"] and rec["termination_date"] != rec["end_date"]:
                issues.append("termination_date_end_date_mismatch")

        if rec["start_date"] and rec["end_date"] and rec["start_date"] >= rec["end_date"]:
            issues.append("empty_or_negative_interval")

        if issues:
            rec["issue_types"] = ";".join(issues)
            out.append(rec)

    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["issue_types"])
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate committee_membership/event linkage and export issues CSV.")
    ap.add_argument("-c", "--congress", type=int, default=None, help="Optional congress filter")
    ap.add_argument("--chamber", choices=["H", "S"], default=None, help="Optional chamber filter")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output/validation/membership_integrity_issues.csv"),
        help="Output CSV path",
    )
    ap.add_argument(
        "--fail-on-issues",
        action="store_true",
        help="Return exit status 2 when any integrity issue rows are produced.",
    )
    args = ap.parse_args(argv)

    issues = run_validation(args.congress, args.chamber)
    write_csv(args.output, issues)
    print(f"Wrote {len(issues)} flagged rows to {args.output}")
    if args.fail_on_issues and issues:
        print(f"VALIDATION_FAILURE: {len(issues)} membership integrity issues", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
