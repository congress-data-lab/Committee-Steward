from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "db"
    / "migrations"
    / "0017_add_provenance_identity_uniqueness.sql"
)


def test_source_identity_migration_guards_consolidation_and_null_versions() -> None:
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())

    assert "GROUP BY source_type, source_name, version_tag HAVING count(*) > 1" in sql
    assert "RAISE EXCEPTION" in sql
    assert "consolidate_provenance.py" in sql
    assert (
        "CONSTRAINT source_identity_key UNIQUE NULLS NOT DISTINCT "
        "(source_type, source_name, version_tag)"
        in sql
    )
