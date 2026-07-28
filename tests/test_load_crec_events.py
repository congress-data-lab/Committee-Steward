from datetime import date

import ingest.load_crec_events as loader


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.connection.executions.append((sql, params))

    def fetchone(self):
        return self.connection.fetchone_result

    def fetchall(self):
        return self.connection.fetchall_result


class _Connection:
    def __init__(self, fetchone_result=None, fetchall_result=None) -> None:
        self.fetchone_result = fetchone_result
        self.fetchall_result = fetchall_result or []
        self.executions: list[tuple[str, tuple | None]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def _removed_event(effective_date: str = "2024-01-21") -> tuple:
    return (
        "event-id",
        118,
        "H",
        "J000292",
        "HSIF",
        "REMOVED",
        "2024-01-09",
        effective_date,
        1,
        "source#content[0]",
        "resignation text",
        "record_pattern",
    )


def test_event_effective_date_refresh_detects_reparsed_change() -> None:
    connection = _Connection((date(2024, 1, 9),))

    assert loader._event_effective_date_needs_refresh(
        connection, "event-id", "2024-01-21"
    )


def test_existing_source_removals_are_available_for_effective_date_replay() -> None:
    connection = _Connection(fetchall_result=[("HSIF",), ("UNTRACKED",)])

    committees = loader._get_existing_source_removal_committees(
        connection,
        10,
        "CREC-page.json#content[0]",
        "J000292",
        118,
        "H",
        {"HSIF"},
    )

    assert committees == ["HSIF"]


def test_flush_allows_same_source_effective_date_repair(monkeypatch) -> None:
    connection = _Connection()
    calls: list[tuple[str, list[tuple]]] = []
    monkeypatch.setattr(loader, "has_active_removal", lambda *args: True)
    monkeypatch.setattr(
        loader, "_event_effective_date_needs_refresh", lambda *args: True
    )

    def execute_values(cursor, sql, values, **kwargs):
        calls.append((sql, values))
        cursor.rowcount = len(values)

    monkeypatch.setattr(loader.psycopg2.extras, "execute_values", execute_values)

    assert loader._flush_event_batch(connection, [_removed_event()]) == (0, 1)
    assert len(calls) == 1
    assert "ON CONFLICT DO NOTHING" in calls[0][0]
    assert calls[0][1][0][7] == "2024-01-21"


def test_flush_suppresses_duplicate_logical_event_ids(monkeypatch) -> None:
    connection = _Connection()
    calls: list[tuple[str, list[tuple]]] = []
    monkeypatch.setattr(loader, "has_active_removal", lambda *args: False)
    monkeypatch.setattr(
        loader, "_event_effective_date_needs_refresh", lambda *args: False
    )

    def execute_values(cursor, sql, values, **kwargs):
        calls.append((sql, values))
        cursor.rowcount = 1

    monkeypatch.setattr(loader.psycopg2.extras, "execute_values", execute_values)
    duplicate = ("different-source-event-id", *_removed_event()[1:])

    assert loader._flush_event_batch(
        connection, [_removed_event(), duplicate]
    ) == (0, 1)
    assert len(calls) == 1
    assert "ON CONFLICT DO NOTHING" in calls[0][0]


def test_flush_still_suppresses_redundant_removal(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(loader, "has_active_removal", lambda *args: True)
    monkeypatch.setattr(
        loader, "_event_effective_date_needs_refresh", lambda *args: False
    )
    monkeypatch.setattr(
        loader.psycopg2.extras,
        "execute_values",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("redundant removal should not reach the database")
        ),
    )

    assert loader._flush_event_batch(connection, [_removed_event()]) == (0, 0)
