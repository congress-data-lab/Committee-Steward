import re
from pathlib import Path

import pytest

import scripts.ensure_schema as ensure_schema_module


ROOT = Path(__file__).parents[1]


class _ExistingSchemaConnection:
    def __init__(self) -> None:
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True


def test_fresh_schema_includes_null_safe_source_identity_constraint() -> None:
    sql = " ".join((ROOT / "db" / "schema.sql").read_text(encoding="utf-8").split())

    assert re.search(
        r"CONSTRAINT source_identity_key UNIQUE NULLS NOT DISTINCT "
        r"\(\s*source_type, source_name, version_tag\s*\)",
        sql,
    )


def test_fresh_schema_includes_committee_rank_evidence_and_intervals() -> None:
    sql = " ".join((ROOT / "db" / "schema.sql").read_text(encoding="utf-8").split())

    assert "caucus_party_code int" in sql
    assert "CREATE TABLE committee_rank_observation" in sql
    assert "CREATE TABLE committee_membership_rank" in sql
    assert "source_member_ordinal int NOT NULL" in sql
    assert "rank_in_party int NOT NULL" in sql


def test_existing_schema_requires_migration_0017_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexes_without_0017 = ensure_schema_module.REQUIRED_INDEXES - {
        "source_identity_key"
    }
    monkeypatch.setattr(
        ensure_schema_module,
        "inspect_schema",
        lambda conn: (
            set(ensure_schema_module.REQUIRED_TABLES),
            set(ensure_schema_module.REQUIRED_COLUMNS),
            set(indexes_without_0017),
        ),
    )
    connection = _ExistingSchemaConnection()

    with pytest.raises(RuntimeError) as exc_info:
        ensure_schema_module.ensure_schema(connection, ROOT / "db" / "schema.sql")

    message = str(exc_info.value)
    assert "source_identity_key" in message
    assert "through 0018" in message
    assert connection.rolled_back


def test_existing_schema_requires_migration_0018_ranking_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tables_without_ranking = set(ensure_schema_module.REQUIRED_TABLES) - {
        "committee_rank_observation",
        "committee_membership_rank",
    }
    monkeypatch.setattr(
        ensure_schema_module,
        "inspect_schema",
        lambda conn: (
            tables_without_ranking,
            set(ensure_schema_module.REQUIRED_COLUMNS),
            set(ensure_schema_module.REQUIRED_INDEXES),
        ),
    )
    connection = _ExistingSchemaConnection()

    with pytest.raises(RuntimeError) as exc_info:
        ensure_schema_module.ensure_schema(connection, ROOT / "db" / "schema.sql")

    assert "committee_membership_rank" in str(exc_info.value)
    assert "through 0018" in str(exc_info.value)
    assert connection.rolled_back
