from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from extstats_advisor.candidates.model import Candidate
from extstats_advisor.errors import SingletonProfileValidationError
from extstats_advisor.native_stats.model import ABSENT_NATIVE, PRESENT, NativeStatsCandidate
from extstats_advisor.optimization.artifact import (
    load_singleton_profile,
    validate_singleton_profile,
    write_singleton_profile,
)
from extstats_advisor.optimization.singleton import (
    SINGLETON_PRECEDENCE_POLICY,
    BaselineProfile,
    CandidateSingletonProfile,
    SingletonProfile,
    profile_singletons,
)
from extstats_advisor.snapshot.model import Workload, WorkloadQuery
from extstats_advisor.utility.model import PerQueryUtility, UtilityResult


def _candidates() -> tuple[Candidate, ...]:
    return (
        Candidate.make("group_a", "rel", "postgresql.mcv", (1, 2), ("a", "b"), 2),
        Candidate.make("group_b", "rel", "postgresql.mcv", (1, 3), ("a", "c"), 1),
        Candidate.make("group_c", "rel", "postgresql.dependencies", (2, 3), ("b", "c"), 3),
    )


def _sources() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    candidates = _candidates()
    snapshot = SimpleNamespace(
        semantic_digest="a" * 64,
        workload=Workload(
            "singleton-workload",
            (
                WorkloadQuery("q1", "SELECT 1", 2),
                WorkloadQuery("q2", "SELECT 2", 1),
                WorkloadQuery("q_zero", "SELECT 3", 0),
            ),
        ),
    )
    universe = SimpleNamespace(
        source_snapshot_semantic_digest=snapshot.semantic_digest,
        semantic_digest="b" * 64,
        candidates=candidates,
        query_profiles=tuple(
            SimpleNamespace(query_id=query_id, analysis_status="supported")
            for query_id in ("q1", "q2", "q_zero")
        ),
    )
    native = []
    for index, candidate in enumerate(candidates):
        if index == 2:
            native.append(
                NativeStatsCandidate(
                    candidate.candidate_id,
                    candidate.relation_id,
                    candidate.kind,
                    candidate.column_ordinals,
                    candidate.column_names,
                    ABSENT_NATIVE,
                    None,
                    0,
                    None,
                    None,
                )
            )
        else:
            native.append(
                NativeStatsCandidate(
                    candidate.candidate_id,
                    candidate.relation_id,
                    candidate.kind,
                    candidate.column_ordinals,
                    candidate.column_names,
                    PRESENT,
                    "fixture",
                    1,
                    "a" * 64,
                    f"payloads/{candidate.candidate_id}.bin",
                )
            )
    repository = SimpleNamespace(
        source_snapshot_semantic_digest=snapshot.semantic_digest,
        candidate_universe_semantic_digest=universe.semantic_digest,
        semantic_digest="c" * 64,
        backend_contract="backend-v1",
        server_version="16.14",
        server_version_num=160014,
        ordinary_stats_fingerprint="d" * 64,
        candidate_models=tuple(native),
    )
    return snapshot, universe, repository


class _FakePlanner:
    def __init__(self) -> None:
        self.configurations: list[tuple[str, ...]] = []
        self.estimate_calls: list[tuple[str, ...]] = []

    def activate(self, configuration) -> None:
        self.configurations.append(configuration.ordered_candidate_ids)

    def estimate_queries(self, query_ids):
        ids = tuple(query_ids)
        self.estimate_calls.append(ids)
        active = self.configurations[-1]
        base = {(): 10, (self._candidates[0],): 8, (self._candidates[1],): 8}[active]
        return tuple(
            SimpleNamespace(query_id=query_id, estimated_rows=base + index)
            for index, query_id in enumerate(ids)
        )


class _FakeUtility:
    utility_contract = "weighted-workload-mean-v1"
    loss_contract = "qerror-cardinality-floor-1-v1"

    def __init__(self) -> None:
        self.calls: list[dict[str, int]] = []

    def evaluate(self, estimates):
        self.calls.append(dict(estimates))
        objective = float(sum(estimates.values()))
        records = tuple(
            PerQueryUtility(query_id, 1.0, float(estimate), 1, float(estimate))
            for query_id, estimate in estimates.items()
        )
        return UtilityResult(objective, self.loss_contract, len(records), len(records), records)


def test_singleton_profiling_evaluates_baseline_and_present_candidates_once() -> None:
    snapshot, universe, repository = _sources()
    planner = _FakePlanner()
    planner._candidates = [candidate.candidate_id for candidate in universe.candidates[:2]]
    utility = _FakeUtility()
    profile = profile_singletons(
        planner,
        snapshot,
        universe,
        repository,
        utility,
        lambda ids: SimpleNamespace(ordered_candidate_ids=tuple(ids)),
        ground_truth_semantic_digest="e" * 64,
        sandbox_contract="sandbox-v1",
    )

    assert planner.configurations == [
        (),
        (universe.candidates[0].candidate_id,),
        (universe.candidates[1].candidate_id,),
    ]
    assert planner.estimate_calls == [("q1", "q2")] * 3
    assert len(utility.calls) == 3
    assert profile.baseline.objective == 21
    assert profile.present_count == 2
    assert profile.absent_count == 1
    assert profile.frozen_ordered_candidate_ids == (
        universe.candidates[1].candidate_id,
        universe.candidates[0].candidate_id,
    )
    absent = profile.candidate_profiles[-1]
    assert absent.native_state == ABSENT_NATIVE
    assert absent.singleton_objective == profile.baseline.objective
    assert absent.improvement == 0
    assert absent.frozen_precedence_rank is None
    assert profile.runtime_metadata["baseline_configuration_count"] == 1
    assert profile.runtime_metadata["singleton_configuration_count"] == 2
    assert profile.runtime_metadata["planner_query_estimate_count"] == 6


def _manual_profile(**kwargs) -> SingletonProfile:
    values = {
        "source_snapshot_semantic_digest": "a" * 64,
        "candidate_universe_semantic_digest": "b" * 64,
        "native_stats_repository_semantic_digest": "c" * 64,
        "ground_truth_semantic_digest": "d" * 64,
        "sandbox_contract": "sandbox-v1",
        "backend_contract": "backend-v1",
        "server_version": "16.14",
        "server_version_num": 160014,
        "ordinary_stats_fingerprint": "e" * 64,
        "utility_contract": "weighted-workload-mean-v1",
        "loss_contract": "qerror-cardinality-floor-1-v1",
        "precedence_policy": SINGLETON_PRECEDENCE_POLICY,
        "baseline": BaselineProfile(10.0),
        "candidate_profiles": (
            CandidateSingletonProfile("cand_a", PRESENT, 2, 7.0, 3.0, 2),
            CandidateSingletonProfile("cand_b", PRESENT, 1, 7.0, 3.0, 1),
            CandidateSingletonProfile("cand_absent", ABSENT_NATIVE, 3, 10.0, 0.0, None),
        ),
        "frozen_ordered_candidate_ids": ("cand_b", "cand_a"),
    }
    values.update(kwargs)
    return SingletonProfile(**values)


def test_singleton_profile_roundtrip_digest_and_runtime_metadata(tmp_path) -> None:
    first = _manual_profile(runtime_metadata={"elapsed_seconds": 1.0})
    second = _manual_profile(runtime_metadata={"elapsed_seconds": 2.0}, created_at="later")
    first_path = tmp_path / "singleton-profile-v1.json"
    second_path = tmp_path / "singleton-profile-v1-second.json"
    first_digest = write_singleton_profile(first, first_path)
    second_digest = write_singleton_profile(second, second_path)
    assert first_digest == second_digest
    assert load_singleton_profile(first_path).computed_semantic_digest == first_digest
    assert validate_singleton_profile(first_path)["frozen_ordered_candidate_ids"] == [
        "cand_b",
        "cand_a",
    ]

    value = json.loads(first_path.read_text())
    value["candidate_profiles"][0]["improvement"] = 99
    first_path.write_text(json.dumps(value))
    with pytest.raises(SingletonProfileValidationError):
        load_singleton_profile(first_path)


def test_singleton_profile_rejects_invalid_absent_and_present_rank() -> None:
    with pytest.raises(SingletonProfileValidationError):
        _manual_profile(
            candidate_profiles=(CandidateSingletonProfile("cand_a", PRESENT, 2, 7.0, 3.0, None),),
            frozen_ordered_candidate_ids=(),
        )
    with pytest.raises(SingletonProfileValidationError, match="precedence policy"):
        _manual_profile(
            candidate_profiles=(
                CandidateSingletonProfile("cand_a", PRESENT, 2, 7.0, 3.0, 1),
                CandidateSingletonProfile("cand_b", PRESENT, 1, 7.0, 3.0, 2),
                CandidateSingletonProfile("cand_absent", ABSENT_NATIVE, 3, 10.0, 0.0, None),
            ),
            frozen_ordered_candidate_ids=("cand_a", "cand_b"),
        )
