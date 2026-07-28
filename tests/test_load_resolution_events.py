from pathlib import Path

import ingest.load_resolution_events as loader


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection
        self.rowcount = 0
        self._result: tuple[int] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.connection.executions.append((sql, params))
        self.rowcount = 0
        normalized_sql = " ".join(sql.split())
        if "SELECT source_id FROM source" in normalized_sql:
            self._result = (1,) if self.connection.source_exists else None
        elif "INSERT INTO source (" in sql:
            self.connection.source_exists = True
            self._result = None if self.connection.source_insert_conflicts else (1,)
        elif "SELECT source_document_id FROM source_document" in normalized_sql:
            self._result = (2,) if self.connection.source_document_exists else None
        elif "INSERT INTO source_document" in sql:
            self.connection.source_document_exists = True
            self._result = (
                None if self.connection.source_document_insert_conflicts else (2,)
            )
        elif "INSERT INTO committee_event" in sql:
            self.rowcount = 1

    def fetchall(self) -> list[tuple[str]]:
        return [("HSFA",)]

    def fetchone(self) -> tuple[int] | None:
        return self._result


class _FakeConnection:
    def __init__(
        self,
        *,
        source_insert_conflicts: bool = False,
        source_document_insert_conflicts: bool = False,
    ) -> None:
        self.executions: list[tuple[str, tuple | None]] = []
        self.committed = False
        self.source_exists = False
        self.source_document_exists = False
        self.source_insert_conflicts = source_insert_conflicts
        self.source_document_insert_conflicts = source_document_insert_conflicts

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        return None


class _FakeMemberResolver:
    def resolve(self, *args: object, **kwargs: object) -> str:
        return "O000173"


def _write_resolution(path: Path, operative_verb: str) -> None:
    path.write_text(
        f"""<?xml version="1.0"?>
<resolution>
  <form><legis-num>H. RES. 76</legis-num><action>
    <action-date date="20230202">February 2, 2023</action-date>
  </action></form>
  <resolution-body><section>
    <text>That the following named Member be {operative_verb} to the following standing committee:</text>
    <committee-appointment-paragraph>
      <header>Committee on Foreign Affairs:</header><text>Ms. Omar.</text>
    </committee-appointment-paragraph>
  </section></resolution-body>
</resolution>
""",
        encoding="utf-8",
    )


def _patch_loader(monkeypatch, connection: _FakeConnection) -> None:
    monkeypatch.setattr(loader, "get_connection", lambda: connection)
    monkeypatch.setattr(loader, "MemberResolver", lambda conn: _FakeMemberResolver())
    monkeypatch.setattr(loader, "committee_name_to_id", lambda *args, **kwargs: "HSFA")


def test_loader_inserts_removal_without_active_appointment_check(
    tmp_path: Path, monkeypatch
) -> None:
    connection = _FakeConnection()
    _patch_loader(monkeypatch, connection)
    monkeypatch.setattr(
        loader,
        "has_active_appointment",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("removals must not use the active-appointment skip")
        ),
    )
    _write_resolution(tmp_path / "BILLS-118hres76eh.xml", "removed from")

    loader.load_resolution_events(congress=118, base_path=tmp_path)

    event_params = next(
        params
        for sql, params in connection.executions
        if "INSERT INTO committee_event" in sql
    )
    assert event_params is not None
    assert event_params[4] == "REMOVED"
    assert connection.committed


def test_loader_preserves_active_appointment_skip(tmp_path: Path, monkeypatch) -> None:
    connection = _FakeConnection()
    _patch_loader(monkeypatch, connection)
    active_checks: list[tuple] = []

    def active_appointment(*args: object) -> bool:
        active_checks.append(args)
        return True

    monkeypatch.setattr(loader, "has_active_appointment", active_appointment)
    _write_resolution(tmp_path / "BILLS-118hres7eh.xml", "elected")

    loader.load_resolution_events(congress=118, base_path=tmp_path)

    assert len(active_checks) == 1
    assert not any(
        "INSERT INTO committee_event" in sql for sql, _ in connection.executions
    )


def test_loader_reuses_resolution_provenance_on_replay(
    tmp_path: Path, monkeypatch
) -> None:
    connection = _FakeConnection()
    _patch_loader(monkeypatch, connection)
    monkeypatch.setattr(loader, "has_active_appointment", lambda *args, **kwargs: False)
    _write_resolution(tmp_path / "BILLS-118hres7eh.xml", "elected")

    loader.load_resolution_events(congress=118, base_path=tmp_path)
    loader.load_resolution_events(congress=118, base_path=tmp_path)

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


def test_house_source_reselects_after_concurrent_insert() -> None:
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


def test_house_document_reselects_after_concurrent_hash_insert(
    tmp_path: Path,
) -> None:
    connection = _FakeConnection(source_document_insert_conflicts=True)
    resolution = tmp_path / "BILLS-118hres1eh.xml"
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
