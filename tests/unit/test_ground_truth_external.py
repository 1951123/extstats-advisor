from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from extstats_advisor.canonical import canonical_json, digest_bytes
from extstats_advisor.errors import GroundTruthValidationError
from extstats_advisor.ground_truth import (
    AUTHORITATIVE_CARDINALITY_OBSERVATIONS_FORMAT_VERSION,
    AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT,
    AUTHORITATIVE_EXTERNAL_SOURCE,
    ArtifactGroundTruthProvider,
    CardinalityTruth,
    GroundTruthSet,
    GroundTruthSource,
    import_authoritative_ground_truth,
    validate_ground_truth_set,
    write_ground_truth_set,
)
from extstats_advisor.snapshot.bundle import load_snapshot, write_snapshot
from extstats_advisor.snapshot.model import Workload, WorkloadQuery
from extstats_advisor.utility import QErrorLoss, WeightedWorkloadUtility
from tests.unit.test_snapshot_roundtrip import make_snapshot


def _published_snapshot(tmp_path: Path, *, zero_weight_query: bool = False):
    snapshot, _ = make_snapshot()
    if zero_weight_query:
        snapshot = replace(
            snapshot,
            workload=Workload(
                "workload-v1",
                (
                    WorkloadQuery("q1", "SELECT 1", 1.0),
                    WorkloadQuery("q_zero", "SELECT 2", 0.0),
                ),
            ),
        )
    path = tmp_path / "snapshot"
    write_snapshot(snapshot, path)
    return path


def _write_observations(path: Path, truths, *, workload_id: str = "workload-v1") -> Path:
    value = {
        "format_version": AUTHORITATIVE_CARDINALITY_OBSERVATIONS_FORMAT_VERSION,
        "workload_id": workload_id,
        "truths": truths,
    }
    path.write_bytes(canonical_json(value) + b"\n")
    return path


def _import(
    tmp_path: Path,
    *,
    authority: str = "authority-a",
    dataset: str = "dataset-a",
    revision: str = "r1",
    truths=None,
):
    snapshot = _published_snapshot(tmp_path)
    observations = _write_observations(
        tmp_path / "observations.json",
        truths or [{"query_id": "q1", "cardinality": 42}],
    )
    return import_authoritative_ground_truth(
        snapshot,
        observations,
        authority=authority,
        dataset_identity=dataset,
        source_revision=revision,
    )


def test_external_import_binds_snapshot_and_preserves_provider_utility(tmp_path: Path) -> None:
    snapshot_path = _published_snapshot(tmp_path)
    observations = _write_observations(
        tmp_path / "observations.json", [{"query_id": "q1", "cardinality": 42}]
    )
    ground_truth = import_authoritative_ground_truth(
        snapshot_path,
        observations,
        authority="warehouse-counts",
        dataset_identity="orders-2026-01",
        source_revision="immutable-17",
    )
    assert ground_truth.source.kind == AUTHORITATIVE_EXTERNAL_SOURCE
    assert ground_truth.collection_contract == AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT
    assert ground_truth.source.dbms is None
    assert ground_truth.source.source_view_token is None
    assert ground_truth.source.authority == "warehouse-counts"
    assert ground_truth.source.dataset_identity == "orders-2026-01"
    assert ground_truth.source.source_revision == "immutable-17"
    assert ground_truth.source.source_artifact_sha256 == digest_bytes(observations.read_bytes())
    assert ground_truth.source_snapshot_semantic_digest
    assert ground_truth.truths[0].source == AUTHORITATIVE_EXTERNAL_SOURCE

    output = tmp_path / "ground-truth.json"
    digest = write_ground_truth_set(ground_truth, output)
    summary = validate_ground_truth_set(output)
    assert summary["source_kind"] == AUTHORITATIVE_EXTERNAL_SOURCE
    assert summary["collection_contract"] == AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT
    assert summary["authority"] == "warehouse-counts"
    assert summary["dataset_identity"] == "orders-2026-01"
    assert summary["source_revision"] == "immutable-17"
    assert summary["source_artifact_sha256"] == ground_truth.source.source_artifact_sha256
    assert summary["semantic_digest"] == digest

    snapshot = load_snapshot(snapshot_path)
    external_utility = WeightedWorkloadUtility(
        snapshot.workload, ArtifactGroundTruthProvider(ground_truth), QErrorLoss()
    ).evaluate({"q1": 21})
    assert external_utility.objective == 2


def test_external_runtime_metadata_does_not_change_identity_but_provenance_does(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _import(first_dir)
    second = _import(second_dir)
    assert first.computed_semantic_digest == second.computed_semantic_digest
    assert (
        replace(
            first,
            created_at="2030-01-01T00:00:00Z",
            runtime_metadata={"elapsed_seconds": 3.5},
        ).computed_semantic_digest
        == first.computed_semantic_digest
    )

    authority_dir = tmp_path / "authority"
    dataset_dir = tmp_path / "dataset"
    revision_dir = tmp_path / "revision"
    cardinality_dir = tmp_path / "cardinality"
    for directory in (authority_dir, dataset_dir, revision_dir, cardinality_dir):
        directory.mkdir()
    assert (
        _import(authority_dir, authority="authority-b").computed_semantic_digest
        != first.computed_semantic_digest
    )
    assert (
        _import(dataset_dir, dataset="dataset-b").computed_semantic_digest
        != first.computed_semantic_digest
    )
    assert (
        _import(revision_dir, revision="r2").computed_semantic_digest
        != first.computed_semantic_digest
    )
    assert (
        _import(
            cardinality_dir, truths=[{"query_id": "q1", "cardinality": 43}]
        ).computed_semantic_digest
        != first.computed_semantic_digest
    )
    different_bytes_dir = tmp_path / "different-bytes"
    different_bytes_dir.mkdir()
    different_snapshot = _published_snapshot(different_bytes_dir)
    different_observations = different_bytes_dir / "observations.json"
    different_observations.write_text(
        json.dumps(
            {
                "format_version": AUTHORITATIVE_CARDINALITY_OBSERVATIONS_FORMAT_VERSION,
                "workload_id": "workload-v1",
                "truths": [{"query_id": "q1", "cardinality": 42}],
            },
            indent=2,
        )
    )
    assert (
        import_authoritative_ground_truth(
            different_snapshot,
            different_observations,
            authority="authority-a",
            dataset_identity="dataset-a",
            source_revision="r1",
        ).computed_semantic_digest
        != first.computed_semantic_digest
    )


@pytest.mark.parametrize(
    ("truths", "match"),
    [
        ([{"query_id": "unknown", "cardinality": 1}], "unknown workload"),
        ([{"query_id": "q1", "cardinality": -1}], "non-negative"),
        ([{"query_id": "q1", "cardinality": True}], "non-negative"),
        ([{"query_id": "q1", "cardinality": 1.0}], "non-negative"),
        ([{"query_id": "q1", "cardinality": 1}, {"query_id": "q1", "cardinality": 1}], "unique"),
    ],
)
def test_external_import_rejects_malformed_observations(tmp_path: Path, truths, match: str) -> None:
    snapshot = _published_snapshot(tmp_path)
    observations = _write_observations(tmp_path / "bad.json", truths)
    with pytest.raises(GroundTruthValidationError, match=match):
        import_authoritative_ground_truth(
            snapshot,
            observations,
            authority="a",
            dataset_identity="d",
            source_revision="r",
        )


def test_external_import_rejects_unknown_fields_workload_and_missing_positive_query(
    tmp_path: Path,
) -> None:
    snapshot = _published_snapshot(tmp_path)
    path = tmp_path / "bad.json"
    value = {
        "format_version": AUTHORITATIVE_CARDINALITY_OBSERVATIONS_FORMAT_VERSION,
        "workload_id": "wrong",
        "truths": [{"query_id": "q1", "cardinality": 1}],
        "unexpected": False,
    }
    path.write_bytes(json.dumps(value).encode())
    with pytest.raises(GroundTruthValidationError, match="unknown fields"):
        import_authoritative_ground_truth(
            snapshot, path, authority="a", dataset_identity="d", source_revision="r"
        )

    _write_observations(path, [{"query_id": "q1", "cardinality": 1}], workload_id="wrong")
    with pytest.raises(GroundTruthValidationError, match="workload ID"):
        import_authoritative_ground_truth(
            snapshot, path, authority="a", dataset_identity="d", source_revision="r"
        )

    zero_snapshot = _published_snapshot(tmp_path / "zero", zero_weight_query=True)
    _write_observations(path, [{"query_id": "q1", "cardinality": 1}])
    imported = import_authoritative_ground_truth(
        zero_snapshot, path, authority="a", dataset_identity="d", source_revision="r"
    )
    assert [truth.query_id for truth in imported.truths] == ["q1"]


def test_source_view_binding_is_only_required_for_production_source(tmp_path: Path) -> None:
    snapshot_path = _published_snapshot(tmp_path)
    observations = _write_observations(
        tmp_path / "observations.json", [{"query_id": "q1", "cardinality": 1}]
    )
    imported = import_authoritative_ground_truth(
        snapshot_path, observations, authority="a", dataset_identity="d", source_revision="r"
    )
    assert imported.source.source_view_token is None
    with pytest.raises(GroundTruthValidationError, match="source-view"):
        from extstats_advisor.ground_truth import CardinalityTruth, GroundTruthSet

        bad = GroundTruthSet(
            imported.source_snapshot_semantic_digest,
            imported.workload_id,
            GroundTruthSource("production-exact-execution", "example-db", "1", 1, "wrong-token"),
            (CardinalityTruth("q1", 1, "production-exact-execution"),),
        )
        output = tmp_path / "bad-production.json"
        write_ground_truth_set(bad, output)
        from extstats_advisor.snapshot.bundle import load_snapshot

        validate_ground_truth_set(output, load_snapshot(snapshot_path))


def test_external_and_production_truth_use_identical_utility_math(tmp_path: Path) -> None:
    snapshot, _ = make_snapshot()
    snapshot = replace(snapshot, semantic_provenance={"source_view_token": "source-token"})
    snapshot_path = tmp_path / "snapshot"
    write_snapshot(snapshot, snapshot_path)
    loaded = load_snapshot(snapshot_path)
    observations = _write_observations(
        tmp_path / "observations.json", [{"query_id": "q1", "cardinality": 42}]
    )
    external = import_authoritative_ground_truth(
        snapshot_path, observations, authority="a", dataset_identity="d", source_revision="r"
    )
    production = GroundTruthSet(
        loaded.semantic_digest or "",
        loaded.workload.workload_id,
        GroundTruthSource(
            "production-exact-execution",
            loaded.dbms.name,
            loaded.dbms.version or "unknown",
            1,
            "source-token",
        ),
        (CardinalityTruth("q1", 42, "production-exact-execution"),),
    )
    production_utility = WeightedWorkloadUtility(
        loaded.workload, ArtifactGroundTruthProvider(production), QErrorLoss()
    ).evaluate({"q1": 21})
    external_utility = WeightedWorkloadUtility(
        loaded.workload, ArtifactGroundTruthProvider(external), QErrorLoss()
    ).evaluate({"q1": 21})
    assert production_utility.objective == external_utility.objective == 2
    assert production.computed_semantic_digest != external.computed_semantic_digest


def test_external_import_rejects_unsealed_snapshot_and_malformed_source_sha(tmp_path: Path) -> None:
    snapshot, _ = make_snapshot()
    observations = _write_observations(
        tmp_path / "observations.json", [{"query_id": "q1", "cardinality": 1}]
    )
    with pytest.raises(GroundTruthValidationError, match="sealed"):
        import_authoritative_ground_truth(
            snapshot, observations, authority="a", dataset_identity="d", source_revision="r"
        )
    with pytest.raises(GroundTruthValidationError, match="SHA256"):
        GroundTruthSource(
            AUTHORITATIVE_EXTERNAL_SOURCE,
            authority="a",
            dataset_identity="d",
            source_revision="r",
            source_artifact_sha256="not-a-sha",
        )


def test_ground_truth_cli_import_validate_and_inspect(tmp_path: Path, capsys) -> None:
    from extstats_advisor.cli import main

    snapshot = _published_snapshot(tmp_path)
    observations = _write_observations(
        tmp_path / "observations.json", [{"query_id": "q1", "cardinality": 42}]
    )
    output = tmp_path / "external-ground-truth.json"
    assert (
        main(
            [
                "ground-truth",
                "import",
                "authoritative",
                str(snapshot),
                str(observations),
                "--authority",
                "a",
                "--dataset-identity",
                "d",
                "--source-revision",
                "r",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert AUTHORITATIVE_EXTERNAL_SOURCE in capsys.readouterr().out
    assert main(["ground-truth", "validate", str(output), "--snapshot", str(snapshot)]) == 0
    inspected = capsys.readouterr().out
    assert "authoritative-external-exact" in inspected
    assert "source_artifact_sha256" in inspected
