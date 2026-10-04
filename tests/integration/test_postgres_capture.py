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
from extstats_advisor.ground_truth import (
    load_ground_truth_set,
    validate_ground_truth_set,
    write_ground_truth_set,
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


def test_live_acquisition_observes_read_only_repeatable_read(
    postgres_capture_dsn: str,
) -> None:
    class ObservingAcquirer(PostgresSnapshotAcquirer):
        observed: tuple[str, str] | None = None

        def _verify_transaction(self, connection) -> None:
            super()._verify_transaction(connection)
            self.observed = (
                str(connection.execute("SHOW transaction_read_only").fetchone()[0]),
                str(connection.execute("SHOW transaction_isolation").fetchone()[0]),
            )

    acquirer = ObservingAcquirer(postgres_capture_dsn)
    acquirer.capture(
        AcquisitionRequest('"Reporting.Schema"."Order Facts"', SamplePolicy(3, seed=9)),
        _workload(),
    )
    assert acquirer.observed == ("on", "repeatable read")


def test_opt_in_ground_truth_shares_source_view_and_is_exact(
    postgres_capture_dsn: str, tmp_path: Path
) -> None:
    acquirer = PostgresSnapshotAcquirer(postgres_capture_dsn)
    snapshot, ground_truth = acquirer.capture_with_ground_truth(
        AcquisitionRequest(
            '"Reporting.Schema"."Order Facts"',
            SamplePolicy(17, seed=42),
        ),
        _workload(),
    )
    snapshot_path = tmp_path / "snapshot"
    truth_path = tmp_path / "ground-truth-v1.json"
    snapshot_digest = write_snapshot(snapshot, snapshot_path)
    truth_digest = write_ground_truth_set(ground_truth, truth_path)
    loaded_snapshot = load_snapshot(snapshot_path)
    assert snapshot_digest == ground_truth.source_snapshot_semantic_digest
    assert (
        snapshot.semantic_provenance["source_view_token"] == ground_truth.source.source_view_token
    )
    assert validate_ground_truth_set(truth_path, loaded_snapshot)["semantic_digest"] == truth_digest
    assert load_ground_truth_set(truth_path, loaded_snapshot).truths[0].cardinality == 100


def test_normal_capture_does_not_execute_workload_truth_queries(
    postgres_capture_dsn: str, tmp_path: Path
) -> None:
    snapshot = PostgresSnapshotAcquirer(postgres_capture_dsn).capture(
        AcquisitionRequest(
            '"Reporting.Schema"."Order Facts"',
            SamplePolicy(3, seed=7),
        ),
        Workload(
            "no-truth-workload",
            (
                WorkloadQuery(
                    "q_missing",
                    'SELECT * FROM "Reporting.Schema"."Missing For Truth Test"',
                ),
            ),
        ),
    )
    write_snapshot(snapshot, tmp_path / "normal-capture")


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
