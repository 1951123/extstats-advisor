from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from extstats_advisor.candidates.universe import (
    derive_candidate_universe,
    load_candidate_universe,
    validate_candidate_universe,
    write_candidate_universe,
)
from extstats_advisor.errors import CandidateGenerationError, CandidateUniverseValidationError
from extstats_advisor.snapshot.bundle import load_snapshot, write_snapshot
from extstats_advisor.snapshot.model import Workload, WorkloadQuery
from tests.unit.test_snapshot_roundtrip import make_snapshot


def _loaded_candidate_fixture(tmp_path: Path):
    snapshot, _ = make_snapshot()
    snapshot = replace(
        snapshot,
        workload=Workload(
            "candidate-workload",
            (
                WorkloadQuery(
                    "q1",
                    'SELECT * FROM "Reporting.Schema"."Order Facts" WHERE "Customer ID" = $1 AND amount > $2 AND "客户" IS NOT NULL',
                ),
            ),
        ),
    )
    snapshot_path = tmp_path / "snapshot"
    write_snapshot(snapshot, snapshot_path)
    return load_snapshot(snapshot_path), snapshot_path


def test_candidate_universe_roundtrip_and_snapshot_binding(tmp_path: Path) -> None:
    snapshot, snapshot_path = _loaded_candidate_fixture(tmp_path)
    universe = derive_candidate_universe(snapshot)
    path = tmp_path / "candidate-universe-v1.json"
    digest = write_candidate_universe(universe, path)
    summary = validate_candidate_universe(path, snapshot)
    loaded = load_candidate_universe(path, snapshot)
    assert digest == summary["semantic_digest"] == loaded.semantic_digest
    assert summary["relevant_group_count"] == 3
    assert summary["candidate_count"] == 6
    assert loaded.candidate_ids_for_query("q1")
    assert loaded.query_ids_for_candidate(loaded.candidates[0].candidate_id) == ("q1",)
    assert loaded.to_dict() == universe.to_dict()
    assert {query_id: loaded.candidate_ids_for_query(query_id) for query_id in ("q1",)} == {
        query_id: tuple(item.candidate_id for item in loaded.incidence if item.query_id == query_id)
        for query_id in ("q1",)
    }
    with pytest.raises(TypeError):
        loaded._query_ids_by_candidate[loaded.candidates[0].candidate_id] = ()
    assert snapshot_path.is_dir()


def test_candidate_artifact_digest_and_source_binding_fail_closed(tmp_path: Path) -> None:
    snapshot, _ = _loaded_candidate_fixture(tmp_path)
    universe = derive_candidate_universe(snapshot)
    path = tmp_path / "candidate.json"
    write_candidate_universe(universe, path)
    value = json.loads(path.read_text())
    value["candidates"][0]["kind"] = "postgresql.ndistinct"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CandidateUniverseValidationError):
        validate_candidate_universe(path)

    old_contract = universe.to_dict()
    old_contract["workload_analysis"]["analysis_contract_version"] = (
        "postgresql-simple-selection-v1"
    )
    old_path = tmp_path / "old-contract.json"
    old_path.write_text(json.dumps(old_contract), encoding="utf-8")
    with pytest.raises(CandidateUniverseValidationError, match="analysis metadata"):
        validate_candidate_universe(old_path)

    other = replace(snapshot, workload=Workload("other", (WorkloadQuery("q1", "SELECT 1"),)))
    other_path = tmp_path / "other-snapshot"
    write_snapshot(other, other_path)
    other_loaded = load_snapshot(other_path)
    path.write_text(json.dumps(universe.to_dict()), encoding="utf-8")
    write_candidate_universe(universe, tmp_path / "candidate-again.json")
    with pytest.raises(CandidateUniverseValidationError):
        validate_candidate_universe(tmp_path / "candidate-again.json", other_loaded)


def test_positive_unsupported_workload_is_not_silently_dropped(tmp_path: Path) -> None:
    snapshot, _ = _loaded_candidate_fixture(tmp_path)
    snapshot = replace(
        snapshot,
        workload=Workload(
            "unsupported",
            (WorkloadQuery("q_bad", 'SELECT * FROM "Order Facts" WHERE "Customer ID" = amount'),),
        ),
    )
    snapshot_path = tmp_path / "unsupported-snapshot"
    write_snapshot(snapshot, snapshot_path)
    with pytest.raises(CandidateGenerationError, match="q_bad"):
        derive_candidate_universe(load_snapshot(snapshot_path))
