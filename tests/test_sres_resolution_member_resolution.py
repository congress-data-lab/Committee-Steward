#!/usr/bin/env python3
"""
Targeted tests for deterministic Senate resolution member normalization/resolution.
Run: .venv/bin/python tests/test_sres_resolution_member_resolution.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.events.sres_parser import (  # noqa: E402
    extract_senate_resolution_state,
    normalize_senate_resolution_name,
    senate_resolution_name_candidates,
    parse_sres_xml,
)
from core.committees.resolver import committee_name_to_id  # noqa: E402
from ingest.load_senate_resolution_events import (  # noqa: E402
    MAJORITY_PARTY_CODES_BY_CONGRESS,
    MINORITY_PARTY_CODES_BY_CONGRESS,
    RESOLVE_WITH_STATE_SQL,
    RESOLVE_WITHOUT_STATE_SQL,
    _day_before,
    _infer_expected_party_codes,
    _infer_party_codes_from_resolved,
    _is_caucus_scoped,
    _is_full_roster_reconstitution,
    _resolve_senate_member_deterministic,
)


class _StubCursor:
    def __init__(self, rows_for_query):
        self._rows_for_query = rows_for_query
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        has_state = "AND s.state = %s" in sql
        # Resolver expects (bioguide_id, party_code, gender)
        def triple(row):
            return (row[0], row[1], row[2]) if len(row) >= 3 else (row[0], row[1], None)
        if has_state:
            _congress, last_name, state = params
            key = (last_name, state)
            raw = self._rows_for_query.get(("with_state", key), [(f"{last_name[:1]}000001", 100)])
            self._rows = [triple(r) for r in raw]
        else:
            _congress, last_name = params
            key = (last_name, None)
            raw = self._rows_for_query.get(("without_state", key), [(f"{last_name[:1]}000001", 100)])
            self._rows = [triple(r) for r in raw]

    def fetchall(self):
        return self._rows


class _StubConn:
    def __init__(self, rows_for_query):
        self._rows_for_query = rows_for_query

    def cursor(self):
        return _StubCursor(self._rows_for_query)


def run_tests() -> None:
    errors: list[str] = []

    # Name cleanup
    if normalize_senate_resolution_name("Mr.\n\t\t\t Johnson") != "Johnson":
        errors.append("normalize newline Johnson failed")
    if normalize_senate_resolution_name("Ms. Stabenow (Chairman)") != "Stabenow":
        errors.append("normalize Stabenow chairman failed")
    if normalize_senate_resolution_name("Mr. Johnson (SD) (Chairman)") != "Johnson":
        errors.append("normalize Johnson SD chairman failed")
    if senate_resolution_name_candidates("Mr. Van Hollen (MD)") != ["Van Hollen", "Hollen"]:
        errors.append("name candidates failed for Van Hollen")
    if senate_resolution_name_candidates("Ms. Cortez Masto (NV)") != ["Cortez Masto", "Masto"]:
        errors.append("name candidates failed for Cortez Masto")
    if extract_senate_resolution_state("Mr. Johnson (SD) (Chairman)") != "SD":
        errors.append("extract state SD failed")
    if committee_name_to_id("Committee on\n\t\t Appropriations", bill_type="sres") != "SSAP":
        errors.append("committee normalization with newlines failed for Appropriations")

    # SQL shape guardrails
    if "s.chamber = 'S'" not in RESOLVE_WITH_STATE_SQL or "s.chamber = 'S'" not in RESOLVE_WITHOUT_STATE_SQL:
        errors.append("resolver SQL must use chamber code 'S'")
    if "JOIN member m ON m.bioguide_id = s.bioguide_id" not in RESOLVE_WITH_STATE_SQL:
        errors.append("resolver SQL must join member_service to member")
    if "party_code" not in RESOLVE_WITH_STATE_SQL or "party_code" not in RESOLVE_WITHOUT_STATE_SQL:
        errors.append("resolver SQL must include party_code for deterministic tie-break")

    rows_for_query = {
        ("without_state", ("Johnson", None)): [("J000111", 100), ("J000222", 200)],
        ("with_state", ("Johnson", "SD")): [("J000111", 100)],
        ("with_state", ("Johnson", "WI")): [("J000222", 200)],
        ("without_state", ("Udall", None)): [("U000001", 100), ("U000002", 100)],
        ("with_state", ("Udall", "CO")): [("U000001", 100)],
        ("with_state", ("Udall", "NM")): [("U000002", 100)],
        ("with_state", ("Van Hollen", "MD")): [("V000128", 100)],
        ("with_state", ("Cortez Masto", "NV")): [("C001113", 100)],
    }
    conn = _StubConn(rows_for_query)

    # Roster-diff helpers (no DB required) - run first
    if not _is_full_roster_reconstitution(
        "Resolved, That the majority party's membership of the Committee on X is constituted"
    ):
        errors.append("_is_full_roster_reconstitution: 'constituted' should match")
    if _is_full_roster_reconstitution("Appoint Senator Smith to the Committee on Y"):
        errors.append("_is_full_roster_reconstitution: add-only text should not match")
    if not _is_caucus_scoped("constitute the majority party's membership"):
        errors.append("_is_caucus_scoped: majority party language should match")
    inferred_codes = _infer_party_codes_from_resolved([
        ("A", {"candidate_parties": [200]}),
        ("B", {"candidate_parties": [200]}),
    ])
    if inferred_codes != {200}:
        errors.append(f"source roster party inference failed: {inferred_codes}")
    if _day_before("2018-01-09") != "2018-01-08":
        errors.append("_day_before: 2018-01-09 should yield 2018-01-08")

    # Resolver determinism
    bid, info = _resolve_senate_member_deterministic(conn, "Johnson", 113, has_member_gender_column=False)
    if bid is not None or info["candidate_count"] != 2:
        errors.append(f"Johnson no-state should be unresolved/2 candidates, got {bid} {info}")

    bid, info = _resolve_senate_member_deterministic(conn, "Johnson (SD)", 113, has_member_gender_column=False)
    if bid != "J000111" or info["candidate_count"] != 1:
        errors.append(f"Johnson SD should resolve uniquely, got {bid} {info}")

    bid, info = _resolve_senate_member_deterministic(conn, "Udall", 113, has_member_gender_column=False)
    if bid is not None or info["candidate_count"] != 2:
        errors.append(f"Udall no-state should be unresolved/2 candidates, got {bid} {info}")

    bid, info = _resolve_senate_member_deterministic(conn, "Udall (CO)", 113, has_member_gender_column=False)
    if bid != "U000001" or info["candidate_count"] != 1:
        errors.append(f"Udall CO should resolve uniquely, got {bid} {info}")

    bid, info = _resolve_senate_member_deterministic(conn, "Udall (NM)", 113, has_member_gender_column=False)
    if bid != "U000002" or info["candidate_count"] != 1:
        errors.append(f"Udall NM should resolve uniquely, got {bid} {info}")
    bid, info = _resolve_senate_member_deterministic(conn, "Van Hollen (MD)", 115, has_member_gender_column=False)
    if bid != "V000128" or info["candidate_count"] != 1:
        errors.append(f"Van Hollen MD should resolve uniquely, got {bid} {info}")
    bid, info = _resolve_senate_member_deterministic(conn, "Cortez Masto (NV)", 115, has_member_gender_column=False)
    if bid != "C001113" or info["candidate_count"] != 1:
        errors.append(f"Cortez Masto NV should resolve uniquely, got {bid} {info}")

    # Party tie-break determinism for caucus-scoped bills
    bid, info = _resolve_senate_member_deterministic(
        conn, "Johnson", 113, has_member_gender_column=False, expected_party_codes=MAJORITY_PARTY_CODES_BY_CONGRESS[113]
    )
    if bid != "J000111" or not info.get("party_tiebreak_used"):
        errors.append(f"Johnson majority tie-break should resolve to SD Democrat, got {bid} {info}")
    bid, info = _resolve_senate_member_deterministic(
        conn, "Johnson", 113, has_member_gender_column=False, expected_party_codes=MINORITY_PARTY_CODES_BY_CONGRESS[113]
    )
    if bid != "J000222" or not info.get("party_tiebreak_used"):
        errors.append(f"Johnson minority tie-break should resolve to WI Republican, got {bid} {info}")

    s17_xml = Path("data/resolutions/113th/bills/sres/BILLS-113sres17ats.xml").read_text()
    s18_xml = Path("data/resolutions/113th/bills/sres/BILLS-113sres18ats.xml").read_text()
    if _infer_expected_party_codes(s17_xml, 113) != MAJORITY_PARTY_CODES_BY_CONGRESS[113]:
        errors.append("failed to infer majority party scope from S.Res. 17")
    if _infer_expected_party_codes(s18_xml, 113) != MINORITY_PARTY_CODES_BY_CONGRESS[113]:
        errors.append("failed to infer minority party scope from S.Res. 18")

    # End-to-end parse expectation for S.Res. 17/18 tokens:
    # after cleanup, only true no-state ambiguities should remain unresolved.
    for bill in ("BILLS-113sres17ats.xml", "BILLS-113sres18ats.xml"):
        path = Path("data/resolutions/113th/bills/sres") / bill
        appts = list(parse_sres_xml(path))
        unresolved_tokens: set[str] = set()
        for appt in appts:
            for raw in appt["members"]:
                # Deterministic stub: only Johnson/Udall without state are ambiguous.
                expected = (
                    MAJORITY_PARTY_CODES_BY_CONGRESS[113]
                    if bill == "BILLS-113sres17ats.xml"
                    else MINORITY_PARTY_CODES_BY_CONGRESS[113]
                )
                bid, _info = _resolve_senate_member_deterministic(
                    conn, raw, 113, has_member_gender_column=False, expected_party_codes=expected
                )
                if bid is None:
                    unresolved_tokens.add(raw)
        for tok in unresolved_tokens:
            last = normalize_senate_resolution_name(tok)
            st = extract_senate_resolution_state(tok)
            if st is not None:
                errors.append(f"{bill}: unresolved token still had state ({tok})")
            if last not in {"Johnson", "Udall"}:
                errors.append(f"{bill}: unexpected unresolved token {tok}")

    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        sys.exit(1)
    print("All S.Res. deterministic resolution tests passed.")


if __name__ == "__main__":
    run_tests()
