from __future__ import annotations

from types import SimpleNamespace

import pytest

from extstats_advisor.dbms.postgres.sandbox import (
    _effective_database,
    _verify_database_binding,
)
from extstats_advisor.errors import PlannerSandboxError, PlannerSandboxValidationError
from extstats_advisor.snapshot.model import RelationName


class _Result:
    def __init__(self, value: str) -> None:
        self.value = value

    def fetchone(self) -> tuple[str]:
        return (self.value,)


class _Connection:
    def __init__(self, database: str) -> None:
        self.database = database

    def execute(self, query: str):
        assert query == "SELECT current_database()"
        return _Result(self.database)


def _snapshot(database: str = "extstats_stock") -> SimpleNamespace:
    return SimpleNamespace(
        schemas=(SimpleNamespace(relation_name=RelationName("forest10", "public", database)),)
    )


def test_explicit_physical_database_replaces_only_live_catalog_binding() -> None:
    snapshot = _snapshot()
    assert _effective_database(snapshot, None, PlannerSandboxError) == "extstats_stock"
    assert _effective_database(snapshot, "exp_foundations_001", PlannerSandboxError) == (
        "exp_foundations_001"
    )
    assert (
        _verify_database_binding(
            _Connection("exp_foundations_001"),
            snapshot,
            "exp_foundations_001",
            PlannerSandboxValidationError,
        )
        == "exp_foundations_001"
    )


def test_mismatched_or_unsafe_physical_database_binding_fails_closed() -> None:
    snapshot = _snapshot()
    with pytest.raises(PlannerSandboxValidationError, match="effective database binding"):
        _verify_database_binding(
            _Connection("other_database"),
            snapshot,
            "exp_foundations_001",
            PlannerSandboxValidationError,
        )
    with pytest.raises(PlannerSandboxError, match="non-empty"):
        _effective_database(snapshot, "", PlannerSandboxError)
    with pytest.raises(PlannerSandboxError, match="invalid character"):
        _effective_database(snapshot, "exp\x00foundations", PlannerSandboxError)
