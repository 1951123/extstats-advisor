from __future__ import annotations

import json
from pathlib import Path

import pytest

from extstats_advisor.dbms.base import AcquisitionRequest, SamplePolicy
from extstats_advisor.dbms.postgres import (
    PostgresSnapshotAcquirer,
    UnsupportedPostgresTypeError,
    UnsupportedRelationError,
)
from extstats_advisor.snapshot.bundle import load_snapshot, validate_snapshot, write_snapshot
from extstats_advisor.snapshot.model import Workload, WorkloadQuery

pytestmark = pytest.mark.integration


def _workload() -> Workload:
    return Workload(
        "integration-workload",
        (WorkloadQuery("q1", 'SELECT "Customer ID" FROM "Reporting.Schema"."Order Facts"'),),
    )


def _capture(dsn: str, output: Path, seed: int):
    snapshot = PostgresSnapshotAcquirer(dsn).capture(
        AcquisitionRequest(
            '"Reporting.Schema"."Order Facts"',
            SamplePolicy(17, seed=seed),
        ),
        _workload(),
    )
    write_snapshot(snapshot, output)
    return snapshot


def test_stock_postgres_capture_is_typed_repeatable_and_read_only(
    postgres_capture_dsn: str, tmp_path: Path
) -> None:
    first = _capture(postgres_capture_dsn, tmp_path / "first", 42)
    second = _capture(postgres_capture_dsn, tmp_path / "second", 42)
    first_id = first.schemas[0].relation_id
    assert first.schemas[0].relation_name.to_dict() == {
        "catalog": "postgres",
        "schema": "Reporting.Schema",
        "name": "Order Facts",
    }
    assert first.samples[first_id].num_rows == second.samples[first_id].num_rows == 17
    assert first.samples[first_id].equals(second.samples[first_id])
    assert first.samples[first_id].column("Observed At").type.tz == "UTC"
    assert "城市" in [field.name for field in first.samples[first_id].schema]
    assert first.populations[0].row_count_source == "postgresql.pg_class.reltuples"
    assert first.populations[0].row_count_quality == "estimate"
    assert first.semantic_provenance["sampling"]["method"] == "postgresql-system-adaptive-v1"
    assert first.semantic_provenance["sampling"]["seed"] == 42
    assert validate_snapshot(tmp_path / "first")["sample_row_counts"][first_id] == 17
    loaded = load_snapshot(tmp_path / "first")
    manifest = json.loads((tmp_path / "first" / "manifest.json").read_text())
    assert "dsn" not in json.dumps(manifest).lower()
    assert loaded.consistency.to_dict()["mode"] == "consistent-source-view"


def test_unsupported_type_and_rls_fail_closed(postgres_capture_dsn: str) -> None:
    acquirer = PostgresSnapshotAcquirer(postgres_capture_dsn)
    with pytest.raises(UnsupportedPostgresTypeError, match="Unsupported JSON"):
        acquirer.capture(
            AcquisitionRequest('"Reporting.Schema"."Unsupported JSON"', SamplePolicy(1, seed=1)),
            _workload(),
        )
    with pytest.raises(UnsupportedRelationError, match="row-level security"):
        acquirer.capture(
            AcquisitionRequest('"Reporting.Schema"."RLS Table"', SamplePolicy(1, seed=1)),
            _workload(),
        )
