from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa

from extstats_advisor.dbms.postgres import native_stats
from extstats_advisor.dbms.postgres.patch import PatchCapabilities


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.rolled_back = False
        self.closed = False

    def execute(self, statement: object, params: object = None) -> _FakeResult:
        self.calls.append(statement)
        return _FakeResult()

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class _FakePsycopg:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.sql = SimpleNamespace(SQL=_FakeComposable, Identifier=_FakeComposable)

    def connect(self, *args: object, **kwargs: object) -> _FakeConnection:
        return self.connection


class _FakeComposable:
    def __init__(self, statement: object) -> None:
        self.statement = statement

    def format(self, *args: object) -> _FakeComposable:
        return self


class _FakeResult:
    def fetchone(self) -> tuple[float]:
        return (1000.0,)


def test_native_materialization_starts_explicit_read_write_transaction(monkeypatch) -> None:
    connection = _FakeConnection()
    snapshot_digest = "a" * 64
    universe_digest = "b" * 64
    capabilities = PatchCapabilities(
        "postgresql-pgextadv-16.14-v1",
        "6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6",
        "16.14",
        160014,
        (),
        (),
    )
    monkeypatch.setattr(native_stats, "_psycopg", lambda: _FakePsycopg(connection))
    monkeypatch.setattr(
        native_stats,
        "_check_inputs",
        lambda snapshot, universe: (object(), pa.table({"value": [1]}), 1000.0),
    )
    monkeypatch.setattr(native_stats, "probe_patched_postgres", lambda connection: capabilities)
    monkeypatch.setattr(native_stats, "create_scratch_relation", lambda *args: None)
    monkeypatch.setattr(native_stats, "copy_arrow_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(native_stats, "_create_statistics", lambda *args: {})
    monkeypatch.setattr(native_stats, "_relation_oid", lambda *args: 42)
    monkeypatch.setattr(native_stats, "ordinary_stats_fingerprint", lambda *args: "c" * 64)
    monkeypatch.setattr(native_stats, "_candidate_results", lambda *args: ((), {}))

    materialization = native_stats.materialize_native_stats(
        "postgresql://patched",
        SimpleNamespace(semantic_digest=snapshot_digest),
        SimpleNamespace(semantic_digest=universe_digest),
    )

    assert connection.calls[0] == "BEGIN READ WRITE"
    assert "BEGIN ISOLATION LEVEL READ WRITE" not in connection.calls
    assert connection.rolled_back is True
    assert connection.closed is True
    assert materialization.observed_reltuples == 1000.0
