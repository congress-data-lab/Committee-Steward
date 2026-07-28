from datetime import date
from pathlib import Path

from ingest.load_directory_snapshots import publication_date_from_path
from validate.directory_overlap import (
    SnapshotAssignment,
    parse_directory_member_label,
    resolve_directory_committee,
    score_snapshot,
)


def test_publication_date_uses_adjacent_official_cdir_artifact(tmp_path: Path):
    snapshot = tmp_path / "committee_memberships_2018.json"
    snapshot.write_text("[]", encoding="utf-8")
    (tmp_path / "CDIR-2018-07-27-HOUSECOMMITTEES.htm").write_text("", encoding="utf-8")
    (tmp_path / "CDIR-2018-10-29-HOUSECOMMITTEES.htm").write_text("", encoding="utf-8")

    assert publication_date_from_path(snapshot, 115) == date(2018, 7, 27)


def test_publication_date_month_suffix_selects_matching_artifact(tmp_path: Path):
    snapshot = tmp_path / "committee_memberships_2018_oct.json"
    snapshot.write_text("[]", encoding="utf-8")
    (tmp_path / "CDIR-2018-07-27-SENATECOMMITTEES.htm").write_text("", encoding="utf-8")
    (tmp_path / "CDIR-2018-10-29-SENATECOMMITTEES.htm").write_text("", encoding="utf-8")

    assert publication_date_from_path(snapshot, 115) == date(2018, 10, 29)


def test_publication_date_falls_back_to_embedded_manifest_metadata(tmp_path: Path):
    snapshots = tmp_path / "data/congressional_directories/113th"
    snapshots.mkdir(parents=True)
    snapshot = snapshots / "committee_memberships_2014.json"
    snapshot.write_text("[]", encoding="utf-8")
    manifests = tmp_path / "data/manifests"
    manifests.mkdir(parents=True)
    (manifests / "manifest_113.csv").write_text(
        "expected_type,local_path,date_issued\n"
        "directory_snapshot_normalized,data/congressional_directories/113th/committee_memberships_2014.json,\n"
        "directory_snapshot,data/primary/113/cdir/CDIR-2014-02-18.zip,2014-02-18\n",
        encoding="utf-8",
    )

    assert publication_date_from_path(snapshot, 113) == date(2014, 2, 18)


def test_manifest_metadata_pairs_multiple_normalized_snapshots_by_order(
    tmp_path: Path,
):
    snapshots = tmp_path / "data/congressional_directories/115th"
    snapshots.mkdir(parents=True)
    first = snapshots / "committee_memberships_2018.json"
    second = snapshots / "committee_memberships_2018_oct.json"
    first.write_text("[]", encoding="utf-8")
    second.write_text("[]", encoding="utf-8")
    manifests = tmp_path / "data/manifests"
    manifests.mkdir(parents=True)
    (manifests / "manifest_115.csv").write_text(
        "expected_type,local_path,date_issued\n"
        "directory_snapshot_normalized,data/congressional_directories/115th/committee_memberships_2018.json,\n"
        "directory_snapshot_normalized,data/congressional_directories/115th/committee_memberships_2018_oct.json,\n"
        "directory_snapshot,data/primary/115/cdir/CDIR-2018-07-27.zip,2018-07-27\n"
        "directory_snapshot,data/primary/115/cdir/CDIR-2018-10-29.zip,2018-10-29\n",
        encoding="utf-8",
    )

    assert publication_date_from_path(first, 115) == date(2018, 7, 27)
    assert publication_date_from_path(second, 115) == date(2018, 10, 29)


def test_parse_directory_member_label_supports_legacy_and_current_forms():
    assert parse_directory_member_label("Collin C. Peterson, of Minnesota.") == (
        "Collin C. Peterson",
        "Minnesota",
    )
    assert parse_directory_member_label("Debbie Stabenow (MI) CHAIRWOMAN") == (
        "Debbie Stabenow",
        "MI",
    )
    assert parse_directory_member_label("David Scott (GA-13)") == (
        "David Scott",
        "GA",
    )
    assert parse_directory_member_label("Vacant") is None
    assert parse_directory_member_label("[VACANT]") is None
    assert parse_directory_member_label("Garret Graves T4 (LA-06)") == (
        "Garret Graves",
        "LA",
    )
    assert parse_directory_member_label("Kirsten E. Gillibrand NY)") == (
        "Kirsten E. Gillibrand",
        "NY",
    )
    assert parse_directory_member_label("(ph) (202) 224-4751") is None


def test_directory_committee_fallback_supports_post_115_name_history():
    assert resolve_directory_committee("Oversight and Reform", 116, "H", {}) == "HSGO"
    assert (
        resolve_directory_committee(
            "United States and the Chinese Communist Party", 118, "H", {}
        )
        == "HSZS"
    )


def test_score_snapshot_counts_both_mismatch_directions_and_unresolved_entries():
    shared = SnapshotAssignment("A000001", "HSAG")
    directory_only = SnapshotAssignment("B000002", "HSAG")
    observed_only = SnapshotAssignment("C000003", "HSAG")
    score = score_snapshot(
        {shared, directory_only},
        {shared, observed_only},
        congress_no=115,
        snapshot_date=date(2018, 7, 27),
        chamber="H",
        committee_scope="standing",
        directory_member_entries=3,
        unresolved_member_entries=1,
        unmapped_committee_entries=0,
        unmapped_committees=0,
        minimum_directory_coverage=0.95,
        minimum_member_resolution=0.99,
    )

    assert score.overlap_assignments == 1
    assert score.directory_only_assignments == 1
    assert score.observed_only_assignments == 1
    assert score.member_resolution_pct == 66.667
    assert score.directory_coverage_pct == 33.333
    assert score.gate_status == "FAIL"
