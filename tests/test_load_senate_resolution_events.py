from pathlib import Path

import ingest.load_senate_resolution_events as loader


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection
        self._result: tuple[int] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.connection.executions.append((sql, params))
        normalized_sql = " ".join(sql.split())
        if "SELECT source_id FROM source" in normalized_sql:
            self._result = (1,) if self.connection.source_exists else None
        elif "INSERT INTO source (" in sql:
            self.connection.source_exists = True
            self._result = None if self.connection.source_insert_conflicts else (1,)
        elif "SELECT source_document_id FROM source_document" in normalized_sql:
            self._result = (
                (2,) if self.connection.source_document_exists else None
            )
        elif "INSERT INTO source_document" in sql:
            self.connection.source_document_exists = True
            self._result = (
                None if self.connection.source_document_insert_conflicts else (2,)
            )

    def fetchone(self) -> tuple[int] | None:
        return self._result


class _FakeConnection:
    def __init__(
        self,
        *,
        source_exists: bool = False,
        source_document_exists: bool = False,
        source_insert_conflicts: bool = False,
        source_document_insert_conflicts: bool = False,
    ) -> None:
        self.executions: list[tuple[str, tuple | None]] = []
        self.source_exists = source_exists
        self.source_document_exists = source_document_exists
        self.source_insert_conflicts = source_insert_conflicts
        self.source_document_insert_conflicts = source_document_insert_conflicts

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_senate_loader_reuses_resolution_provenance_on_replay(
    tmp_path: Path,
) -> None:
    connection = _FakeConnection()
    resolution = tmp_path / "BILLS-118sres1ats.xml"
    resolution.write_text("<resolution>same bytes</resolution>", encoding="utf-8")

    for _ in range(2):
        source_id = loader._get_or_create_source(connection, 118)
        loader._get_or_create_source_document(
            connection, source_id, resolution, "2023-01-03"
        )

    source_inserts = [
        sql for sql, _ in connection.executions if "INSERT INTO source (" in sql
    ]
    document_inserts = [
        (sql, params)
        for sql, params in connection.executions
        if "INSERT INTO source_document" in sql
    ]
    assert len(source_inserts) == 1
    assert len(document_inserts) == 1
    assert document_inserts[0][1] is not None
    assert len(document_inserts[0][1][3]) == 64


def test_senate_loader_reuses_legacy_document_without_hash(
    tmp_path: Path,
) -> None:
    connection = _FakeConnection(
        source_exists=True,
        source_document_exists=True,
    )
    resolution = tmp_path / "BILLS-118sres1ats.xml"
    resolution.write_text("<resolution>legacy row</resolution>", encoding="utf-8")

    source_id = loader._get_or_create_source(connection, 118)
    document_id = loader._get_or_create_source_document(
        connection, source_id, resolution, "2023-01-03"
    )

    assert source_id == 1
    assert document_id == 2
    assert not any(
        "INSERT INTO source_document" in sql for sql, _ in connection.executions
    )
    document_select = next(
        (sql, params)
        for sql, params in connection.executions
        if "SELECT source_document_id FROM source_document" in " ".join(sql.split())
    )
    assert "content_hash IS NULL" in document_select[0]
    assert document_select[1] is not None
    assert document_select[1][1:3] == (1, resolution.name)


def test_senate_source_reselects_after_concurrent_insert() -> None:
    connection = _FakeConnection(source_insert_conflicts=True)

    source_id = loader._get_or_create_source(connection, 118)

    assert source_id == 1
    source_insert = next(
        sql for sql, _ in connection.executions if "INSERT INTO source (" in sql
    )
    assert "ON CONFLICT ON CONSTRAINT source_identity_key DO NOTHING" in " ".join(
        source_insert.split()
    )
    source_selects = [
        sql
        for sql, _ in connection.executions
        if "SELECT source_id FROM source" in " ".join(sql.split())
    ]
    assert len(source_selects) == 2


def test_senate_document_reselects_after_concurrent_hash_insert(
    tmp_path: Path,
) -> None:
    connection = _FakeConnection(source_document_insert_conflicts=True)
    resolution = tmp_path / "BILLS-118sres1ats.xml"
    resolution.write_text("<resolution>same bytes</resolution>", encoding="utf-8")

    document_id = loader._get_or_create_source_document(
        connection, 1, resolution, "2023-01-03"
    )

    assert document_id == 2
    document_insert = next(
        sql for sql, _ in connection.executions if "INSERT INTO source_document" in sql
    )
    assert (
        "ON CONFLICT (content_hash) WHERE content_hash IS NOT NULL DO NOTHING"
        in " ".join(document_insert.split())
    )
    document_selects = [
        sql
        for sql, _ in connection.executions
        if "SELECT source_document_id FROM source_document" in " ".join(sql.split())
    ]
    assert len(document_selects) == 2
