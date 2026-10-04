from __future__ import annotations

import json

import pyarrow as pa
import pytest

from extstats_advisor.errors import GroundTruthValidationError
from extstats_advisor.ground_truth import (
    GROUND_TRUTH_FORMAT_VERSION,
    PRODUCTION_EXACT_SOURCE,
    CardinalityTruth,
    GroundTruthSet,
    GroundTruthSource,
    load_ground_truth_set,
    validate_ground_truth_set,
    write_ground_truth_set,
)
from extstats_advisor.snapshot.model import (
    AdvisorSnapshot,
    ColumnSchema,
    DBMSIdentity,
    PopulationMetadata,
    RelationName,
    RelationSchema,
    SnapshotConsistency,
    Workload,
    WorkloadQuery,
)


def _source() -> GroundTruthSource:
    return GroundTruthSource(
        PRODUCTION_EXACT_SOURCE,
        "postgresql",
        "16.14",
        160014,
        "1:2:",
    )


def _truth_set(created_at: str | None = None) -> GroundTruthSet:
    return GroundTruthSet(
        "a" * 64,
        "utility-workload",
        _source(),
        (
            CardinalityTruth("q2", 0, PRODUCTION_EXACT_SOURCE),
            CardinalityTruth("q1", 12, PRODUCTION_EXACT_SOURCE),
        ),
        created_at=created_at,
    )


def _snapshot() -> AdvisorSnapshot:
    relation = RelationSchema(
        "rel_1",
        RelationName("items", schema="public", catalog="postgres"),
        (ColumnSchema("id", 1, "int32", False, "integer"),),
    )
    return AdvisorSnapshot(
        (relation,),
        (PopulationMetadata("rel_1", 2, "estimate", "fixture"),),
        Workload(
            "utility-workload",
            (
                WorkloadQuery("q1", 'SELECT * FROM "public"."items" WHERE "id" = 1'),
                WorkloadQuery("q2", 'SELECT * FROM "public"."items" WHERE "id" = 2', 0),
            ),
        ),
        {"rel_1": pa.table({"id": pa.array([1, 2], type=pa.int32())})},
        DBMSIdentity("postgresql", "16.14"),
        SnapshotConsistency(),
        {"source_view_token": "1:2:"},
        {},
        semantic_digest="a" * 64,
    )


def test_ground_truth_round_trip_and_runtime_metadata_do_not_change_digest(tmp_path) -> None:
    first = _truth_set("2026-01-01T00:00:00Z")
    second = GroundTruthSet(
        first.source_snapshot_semantic_digest,
        first.workload_id,
        first.source,
        first.truths,
        created_at="2027-01-01T00:00:00Z",
        runtime_metadata={"elapsed_seconds": 9.5},
    )
    first_path = tmp_path / "ground-truth-v1.json"
    second_path = tmp_path / "ground-truth-v1-second.json"
    first_digest = write_ground_truth_set(first, first_path)
    second_digest = write_ground_truth_set(second, second_path)
    assert first_digest == second_digest
    loaded = load_ground_truth_set(first_path)
    assert loaded.computed_semantic_digest == first_digest
    assert validate_ground_truth_set(first_path)["format_version"] == GROUND_TRUTH_FORMAT_VERSION


def test_ground_truth_validates_against_snapshot_and_requires_positive_coverage(tmp_path) -> None:
    path = tmp_path / "truth.json"
    truth = GroundTruthSet(
        "a" * 64,
        "utility-workload",
        _source(),
        (CardinalityTruth("q1", 12, PRODUCTION_EXACT_SOURCE),),
    )
    write_ground_truth_set(truth, path)
    assert validate_ground_truth_set(path, _snapshot())["query_count"] == 1

    missing = GroundTruthSet(
        "a" * 64,
        "utility-workload",
        _source(),
        (CardinalityTruth("q2", 0, PRODUCTION_EXACT_SOURCE),),
    )
    missing_path = tmp_path / "missing.json"
    write_ground_truth_set(missing, missing_path)
    with pytest.raises(GroundTruthValidationError, match="missing positive-weight"):
        validate_ground_truth_set(missing_path, _snapshot())


def test_ground_truth_rejects_digest_corruption_duplicate_and_negative(tmp_path) -> None:
    path = tmp_path / "truth.json"
    write_ground_truth_set(_truth_set(), path)
    value = json.loads(path.read_text())
    value["truths"][0]["cardinality"] = 99
    path.write_text(json.dumps(value))
    with pytest.raises(GroundTruthValidationError, match="digest"):
        validate_ground_truth_set(path)

    with pytest.raises(GroundTruthValidationError, match="unique"):
        GroundTruthSet(
            "a" * 64,
            "utility-workload",
            _source(),
            (
                CardinalityTruth("q1", 1, PRODUCTION_EXACT_SOURCE),
                CardinalityTruth("q1", 2, PRODUCTION_EXACT_SOURCE),
            ),
        )
    with pytest.raises(GroundTruthValidationError, match="non-negative"):
        CardinalityTruth("q", -1, PRODUCTION_EXACT_SOURCE)
