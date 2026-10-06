"""Generic singleton utility profiling and frozen precedence models."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from extstats_advisor.errors import SingletonProfileValidationError, SingletonProfilingError
from extstats_advisor.native_stats.model import ABSENT_NATIVE, PRESENT
from extstats_advisor.utility.model import UtilityResult

SINGLETON_PROFILE_FORMAT_VERSION = "singleton-profile-v1"
SINGLETON_PRECEDENCE_POLICY = "singleton-utility-precedence-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SingletonProfileValidationError(f"{label} must be a non-empty string")
    return value


def _finite(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SingletonProfileValidationError(f"{label} must be finite")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise SingletonProfileValidationError(f"{label} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class BaselineProfile:
    objective: float

    def __post_init__(self) -> None:
        _finite(self.objective, "baseline objective", nonnegative=True)

    def to_dict(self) -> dict[str, Any]:
        return {"objective": self.objective}


@dataclass(frozen=True, slots=True)
class CandidateSingletonProfile:
    candidate_id: str
    native_state: str
    static_precedence_rank: int
    singleton_objective: float
    improvement: float
    frozen_precedence_rank: int | None

    def __post_init__(self) -> None:
        _token(self.candidate_id, "singleton candidate_id")
        if self.native_state not in {PRESENT, ABSENT_NATIVE}:
            raise SingletonProfileValidationError("unknown singleton native state")
        if (
            isinstance(self.static_precedence_rank, bool)
            or not isinstance(self.static_precedence_rank, int)
            or self.static_precedence_rank < 1
        ):
            raise SingletonProfileValidationError("static precedence rank must be positive")
        _finite(self.singleton_objective, "singleton objective", nonnegative=True)
        _finite(self.improvement, "singleton improvement")
        if self.frozen_precedence_rank is not None and (
            isinstance(self.frozen_precedence_rank, bool)
            or not isinstance(self.frozen_precedence_rank, int)
            or self.frozen_precedence_rank < 1
        ):
            raise SingletonProfileValidationError("frozen precedence rank must be positive")
        if self.native_state == PRESENT and self.frozen_precedence_rank is None:
            raise SingletonProfileValidationError("PRESENT singleton must have a frozen rank")
        if self.native_state == ABSENT_NATIVE and self.frozen_precedence_rank is not None:
            raise SingletonProfileValidationError(
                "ABSENT_NATIVE singleton cannot have a frozen rank"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "native_state": self.native_state,
            "static_precedence_rank": self.static_precedence_rank,
            "singleton_objective": self.singleton_objective,
            "improvement": self.improvement,
            "frozen_precedence_rank": self.frozen_precedence_rank,
        }


@dataclass(frozen=True, slots=True)
class SingletonProfile:
    source_snapshot_semantic_digest: str
    candidate_universe_semantic_digest: str
    native_stats_repository_semantic_digest: str
    ground_truth_semantic_digest: str
    sandbox_contract: str
    backend_contract: str
    server_version: str
    server_version_num: int
    ordinary_stats_fingerprint: str
    utility_contract: str
    loss_contract: str
    precedence_policy: str
    baseline: BaselineProfile
    candidate_profiles: tuple[CandidateSingletonProfile, ...]
    frozen_ordered_candidate_ids: tuple[str, ...]
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    created_at: str | None = field(default=None, repr=False, compare=False)
    semantic_digest: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("source snapshot semantic digest", self.source_snapshot_semantic_digest),
            ("candidate universe semantic digest", self.candidate_universe_semantic_digest),
            ("native repository semantic digest", self.native_stats_repository_semantic_digest),
            ("ground truth semantic digest", self.ground_truth_semantic_digest),
            ("ordinary statistics fingerprint", self.ordinary_stats_fingerprint),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise SingletonProfileValidationError(f"{label} must be a SHA-256 token")
        for label, value in (
            ("sandbox contract", self.sandbox_contract),
            ("backend contract", self.backend_contract),
            ("server version", self.server_version),
            ("utility contract", self.utility_contract),
            ("loss contract", self.loss_contract),
        ):
            _token(value, label)
        if (
            isinstance(self.server_version_num, bool)
            or not isinstance(self.server_version_num, int)
            or self.server_version_num < 1
        ):
            raise SingletonProfileValidationError("server_version_num must be positive")
        if self.precedence_policy != SINGLETON_PRECEDENCE_POLICY:
            raise SingletonProfileValidationError("unsupported singleton precedence policy")
        if not isinstance(self.runtime_metadata, Mapping):
            raise SingletonProfileValidationError("singleton runtime_metadata must be an object")
        object.__setattr__(self, "runtime_metadata", MappingProxyType(dict(self.runtime_metadata)))
        if self.created_at is not None:
            _token(self.created_at, "singleton created_at")
        if self.semantic_digest is not None and not _SHA256.fullmatch(self.semantic_digest):
            raise SingletonProfileValidationError("singleton semantic digest is invalid")

        ids = [profile.candidate_id for profile in self.candidate_profiles]
        if len(ids) != len(set(ids)):
            raise SingletonProfileValidationError("singleton candidate IDs must be unique")
        present = [
            profile for profile in self.candidate_profiles if profile.native_state == PRESENT
        ]
        absent = [
            profile for profile in self.candidate_profiles if profile.native_state == ABSENT_NATIVE
        ]
        if any(
            profile.singleton_objective != self.baseline.objective
            or profile.improvement != 0
            or profile.frozen_precedence_rank is not None
            for profile in absent
        ):
            raise SingletonProfileValidationError(
                "ABSENT_NATIVE singleton profile must be baseline-equivalent and unranked"
            )
        for profile in self.candidate_profiles:
            expected = self.baseline.objective - profile.singleton_objective
            if profile.improvement != expected:
                raise SingletonProfileValidationError(
                    f"singleton improvement is inconsistent for {profile.candidate_id}"
                )
        policy_order = tuple(
            profile.candidate_id
            for profile in sorted(
                present,
                key=lambda item: (
                    -item.improvement,
                    item.static_precedence_rank,
                    item.candidate_id,
                ),
            )
        )
        if self.frozen_ordered_candidate_ids != policy_order:
            raise SingletonProfileValidationError(
                "frozen candidate order violates singleton precedence policy"
            )
        ranks = [profile.frozen_precedence_rank for profile in present]
        if sorted(ranks) != list(range(1, len(present) + 1)):
            raise SingletonProfileValidationError("frozen precedence ranks are not contiguous")
        expected_order = tuple(
            profile.candidate_id
            for profile in sorted(present, key=lambda item: item.frozen_precedence_rank or 0)
        )
        if self.frozen_ordered_candidate_ids != expected_order:
            raise SingletonProfileValidationError("frozen candidate order is inconsistent")
        if len(set(self.frozen_ordered_candidate_ids)) != len(self.frozen_ordered_candidate_ids):
            raise SingletonProfileValidationError("frozen candidate order contains duplicates")

    @property
    def present_count(self) -> int:
        return sum(profile.native_state == PRESENT for profile in self.candidate_profiles)

    @property
    def absent_count(self) -> int:
        return sum(profile.native_state == ABSENT_NATIVE for profile in self.candidate_profiles)

    def semantic_manifest(self) -> dict[str, Any]:
        return {
            "format_version": SINGLETON_PROFILE_FORMAT_VERSION,
            "source_snapshot_semantic_digest": self.source_snapshot_semantic_digest,
            "candidate_universe_semantic_digest": self.candidate_universe_semantic_digest,
            "native_stats_repository_semantic_digest": self.native_stats_repository_semantic_digest,
            "ground_truth_semantic_digest": self.ground_truth_semantic_digest,
            "planner": {
                "sandbox_contract": self.sandbox_contract,
                "backend_contract": self.backend_contract,
                "server_version": self.server_version,
                "server_version_num": self.server_version_num,
                "ordinary_stats_fingerprint": self.ordinary_stats_fingerprint,
            },
            "utility": {
                "utility_contract": self.utility_contract,
                "loss_contract": self.loss_contract,
            },
            "precedence_policy": self.precedence_policy,
            "baseline": self.baseline.to_dict(),
            "candidate_profiles": [
                profile.to_dict()
                for profile in sorted(self.candidate_profiles, key=lambda item: item.candidate_id)
            ],
            "frozen_ordered_candidate_ids": list(self.frozen_ordered_candidate_ids),
        }

    @property
    def computed_semantic_digest(self) -> str:
        from extstats_advisor.canonical import digest_json

        return digest_json(self.semantic_manifest())

    def to_manifest(self) -> dict[str, Any]:
        value = self.semantic_manifest()
        value["semantic_digest"] = self.computed_semantic_digest
        if self.created_at is not None:
            value["created_at"] = self.created_at
        if self.runtime_metadata:
            value["runtime_metadata"] = dict(self.runtime_metadata)
        return value


def _positive_query_ids(snapshot: Any, candidate_universe: Any) -> tuple[str, ...]:
    profiles = {profile.query_id: profile for profile in candidate_universe.query_profiles}
    result = []
    for query in snapshot.workload.queries:
        if query.weight <= 0:
            continue
        profile = profiles.get(query.query_id)
        if profile is None or profile.analysis_status != "supported":
            raise SingletonProfilingError(
                f"positive-weight query {query.query_id!r} is outside singleton profiling scope"
            )
        result.append(query.query_id)
    if not result:
        raise SingletonProfilingError("workload has no positive-weight supported queries")
    return tuple(result)


def _validate_sources(snapshot: Any, candidate_universe: Any, repository: Any) -> None:
    snapshot_digest = getattr(snapshot, "semantic_digest", None)
    if not isinstance(snapshot_digest, str) or not _SHA256.fullmatch(snapshot_digest):
        raise SingletonProfilingError("singleton profiling requires a sealed snapshot")
    if candidate_universe.source_snapshot_semantic_digest != snapshot_digest:
        raise SingletonProfilingError("candidate universe snapshot digest mismatch")
    if repository.source_snapshot_semantic_digest != snapshot_digest:
        raise SingletonProfilingError("native repository snapshot digest mismatch")
    if repository.candidate_universe_semantic_digest != candidate_universe.semantic_digest:
        raise SingletonProfilingError("native repository candidate universe digest mismatch")
    universe_ids = {
        (candidate.candidate_id, candidate.relation_id, candidate.kind, candidate.column_ordinals)
        for candidate in candidate_universe.candidates
    }
    repository_ids = {
        (candidate.candidate_id, candidate.relation_id, candidate.kind, candidate.column_ordinals)
        for candidate in repository.candidate_models
    }
    if universe_ids != repository_ids:
        raise SingletonProfilingError("native repository candidate set does not match universe")


def _utility_contracts(utility_provider: Any) -> tuple[str, str]:
    utility_contract = getattr(utility_provider, "utility_contract", None)
    if not isinstance(utility_contract, str) or not utility_contract:
        utility_contract = getattr(utility_provider, "contract_version", None)
    loss_contract = getattr(utility_provider, "loss_contract", None)
    if not isinstance(utility_contract, str) or not utility_contract:
        raise SingletonProfilingError("utility provider contract is required")
    if not isinstance(loss_contract, str) or not loss_contract:
        raise SingletonProfilingError("utility provider loss contract is required")
    return utility_contract, loss_contract


def _estimate_map(estimates: Sequence[Any], requested_query_ids: Sequence[str]) -> dict[str, float]:
    estimate_ids = tuple(estimate.query_id for estimate in estimates)
    if estimate_ids != tuple(requested_query_ids):
        raise SingletonProfilingError("planner did not return the requested query estimates")
    return {estimate.query_id: estimate.estimated_rows for estimate in estimates}


def _percentile_95(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return float(ordered[index])


def _incidence_runtime(
    *,
    present_candidate_ids: Sequence[str],
    incidence_counts: Mapping[str, int],
    baseline_query_count: int,
    singleton_query_count: int,
    strategy: str,
    elapsed_seconds: float,
) -> dict[str, Any]:
    counts = {
        candidate_id: incidence_counts[candidate_id] for candidate_id in present_candidate_ids
    }
    values = tuple(counts.values())
    reference_count = baseline_query_count * (1 + len(present_candidate_ids))
    saved_count = reference_count - (baseline_query_count + singleton_query_count)
    summary = {
        "min": min(values) if values else 0,
        "mean": sum(values) / len(values) if values else 0.0,
        "median": (
            float(sorted(values)[len(values) // 2])
            if values and len(values) % 2
            else (
                (sorted(values)[len(values) // 2 - 1] + sorted(values)[len(values) // 2]) / 2
                if values
                else 0.0
            )
        ),
        "p95": _percentile_95(values),
        "max": max(values) if values else 0,
    }
    return {
        "evaluation_strategy": strategy,
        "baseline_configuration_count": 1,
        "singleton_configuration_count": len(present_candidate_ids),
        "baseline_planner_query_estimate_count": baseline_query_count,
        "singleton_planner_query_estimate_count": singleton_query_count,
        "planner_query_estimate_count": baseline_query_count + singleton_query_count,
        "full_workload_reference_planner_query_estimate_count": reference_count,
        "saved_planner_query_estimate_count": saved_count,
        "planner_query_reduction_fraction": (
            saved_count / reference_count if reference_count else 0.0
        ),
        "candidate_incidence_counts": counts,
        "candidate_incidence_summary": summary,
        "zero_incidence_present_candidate_count": sum(value == 0 for value in values),
        "profiling_wall_clock_seconds": elapsed_seconds,
    }


def _assemble_profile(
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    utility_contract: str,
    loss_contract: str,
    baseline_result: UtilityResult,
    candidate_results: Sequence[tuple[Any, UtilityResult]],
    *,
    ground_truth_semantic_digest: str,
    sandbox_contract: str,
    runtime: Mapping[str, Any],
) -> SingletonProfile:
    profiles: list[CandidateSingletonProfile] = []
    for candidate, result in candidate_results:
        native_state = next(
            item.state
            for item in native_repository.candidate_models
            if item.candidate_id == candidate.candidate_id
        )
        frozen_rank = 1 if native_state == PRESENT else None
        profiles.append(
            CandidateSingletonProfile(
                candidate.candidate_id,
                native_state,
                candidate.static_precedence_rank,
                result.objective,
                baseline_result.objective - result.objective,
                frozen_rank,
            )
        )
    present_profiles = [profile for profile in profiles if profile.native_state == PRESENT]
    ordered = sorted(
        present_profiles,
        key=lambda profile: (
            -profile.improvement,
            profile.static_precedence_rank,
            profile.candidate_id,
        ),
    )
    frozen_order = tuple(profile.candidate_id for profile in ordered)
    ranks = {candidate_id: rank for rank, candidate_id in enumerate(frozen_order, start=1)}
    ranked_profiles = tuple(
        CandidateSingletonProfile(
            profile.candidate_id,
            profile.native_state,
            profile.static_precedence_rank,
            profile.singleton_objective,
            profile.improvement,
            ranks.get(profile.candidate_id),
        )
        for profile in profiles
    )
    return SingletonProfile(
        snapshot.semantic_digest,
        candidate_universe.semantic_digest,
        native_repository.semantic_digest,
        ground_truth_semantic_digest,
        sandbox_contract,
        str(native_repository.backend_contract),
        native_repository.server_version,
        native_repository.server_version_num,
        native_repository.ordinary_stats_fingerprint,
        utility_contract,
        loss_contract,
        SINGLETON_PRECEDENCE_POLICY,
        BaselineProfile(baseline_result.objective),
        ranked_profiles,
        frozen_order,
        runtime,
    )


def _validate_ground_truth_digest(ground_truth_semantic_digest: str) -> None:
    if not _SHA256.fullmatch(ground_truth_semantic_digest):
        raise SingletonProfilingError("ground truth semantic digest is invalid")


def _validate_utility_result(result: UtilityResult, loss_contract: str) -> None:
    if result.loss_contract != loss_contract:
        raise SingletonProfilingError("utility result loss contract changed during profiling")


def validate_nonincident_estimates_unchanged(
    baseline_estimates: Mapping[str, float],
    singleton_estimates: Mapping[str, float],
    affected_query_ids: Sequence[str],
) -> None:
    """Fail closed when a full-workload audit changes a nonincident estimate."""

    if set(singleton_estimates) != set(baseline_estimates):
        raise SingletonProfilingError("singleton audit estimate coverage changed")
    affected = set(affected_query_ids)
    for query_id, baseline in baseline_estimates.items():
        if query_id not in affected and singleton_estimates[query_id] != baseline:
            raise SingletonProfilingError(
                f"nonincident planner estimate changed for query {query_id!r}"
            )


def _profile_common_setup(
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    utility_provider: Any,
    *,
    ground_truth_semantic_digest: str,
) -> tuple[tuple[str, ...], str, str, dict[str, Any]]:
    _validate_sources(snapshot, candidate_universe, native_repository)
    if not callable(getattr(candidate_universe, "query_ids_for_candidate", None)):
        raise SingletonProfilingError("candidate universe does not expose incidence indexes")
    query_ids = _positive_query_ids(snapshot, candidate_universe)
    utility_contract, loss_contract = _utility_contracts(utility_provider)
    _validate_ground_truth_digest(ground_truth_semantic_digest)
    repository_by_id = {
        candidate.candidate_id: candidate for candidate in native_repository.candidate_models
    }
    return query_ids, utility_contract, loss_contract, repository_by_id


def profile_singletons(
    planner_session: Any,
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    utility_provider: Any,
    configuration_factory: Callable[[Iterable[str]], Any],
    *,
    ground_truth_semantic_digest: str,
    sandbox_contract: str,
    runtime_metadata: Mapping[str, Any] | None = None,
    estimate_audit: MutableMapping[str, Any] | None = None,
) -> SingletonProfile:
    """Evaluate singleton utilities using exact incidence-incremental calls."""

    started = time.perf_counter()
    query_ids, utility_contract, loss_contract, repository_by_id = _profile_common_setup(
        snapshot,
        candidate_universe,
        native_repository,
        utility_provider,
        ground_truth_semantic_digest=ground_truth_semantic_digest,
    )

    def evaluate(query_subset: Sequence[str]) -> dict[str, float]:
        estimates = planner_session.estimate_queries(query_subset)
        return _estimate_map(estimates, query_subset)

    planner_session.activate(configuration_factory(()))
    baseline_estimates = evaluate(query_ids)
    if estimate_audit is not None:
        estimate_audit["baseline"] = dict(baseline_estimates)
        estimate_audit["singletons"] = {}
    baseline_result = utility_provider.evaluate(baseline_estimates)
    _validate_utility_result(baseline_result, loss_contract)
    candidate_results: list[tuple[Any, UtilityResult]] = []
    singleton_query_estimate_count = 0
    incidence_counts: dict[str, int] = {}
    for candidate in candidate_universe.candidates:
        native = repository_by_id[candidate.candidate_id]
        if native.state == ABSENT_NATIVE:
            candidate_results.append((candidate, baseline_result))
            continue
        incidence = tuple(candidate_universe.query_ids_for_candidate(candidate.candidate_id))
        unknown = set(incidence).difference(query_ids)
        if unknown:
            raise SingletonProfilingError(
                f"candidate incidence references queries outside profiling scope: {sorted(unknown)}"
            )
        incidence_set = set(incidence)
        affected = tuple(query_id for query_id in query_ids if query_id in incidence_set)
        incidence_counts[candidate.candidate_id] = len(affected)
        if not affected:
            if estimate_audit is not None:
                estimate_audit["singletons"][candidate.candidate_id] = dict(baseline_estimates)
            candidate_results.append((candidate, baseline_result))
            continue
        planner_session.activate(configuration_factory((candidate.candidate_id,)))
        singleton_estimates = evaluate(affected)
        singleton_query_estimate_count += len(affected)
        merged_estimates = dict(baseline_estimates)
        merged_estimates.update(singleton_estimates)
        if tuple(merged_estimates) != query_ids:
            raise SingletonProfilingError("incremental estimates did not cover the full workload")
        if estimate_audit is not None:
            estimate_audit["singletons"][candidate.candidate_id] = dict(merged_estimates)
        result = utility_provider.evaluate(merged_estimates)
        _validate_utility_result(result, loss_contract)
        candidate_results.append((candidate, result))

    present_profiles = [
        candidate
        for candidate, _ in candidate_results
        if repository_by_id[candidate.candidate_id].state == PRESENT
    ]
    runtime = _incidence_runtime(
        present_candidate_ids=[candidate.candidate_id for candidate in present_profiles],
        incidence_counts=incidence_counts,
        baseline_query_count=len(query_ids),
        singleton_query_count=singleton_query_estimate_count,
        strategy="incidence-incremental-v1",
        elapsed_seconds=time.perf_counter() - started,
    )
    if runtime_metadata:
        runtime.update(runtime_metadata)
    return _assemble_profile(
        snapshot,
        candidate_universe,
        native_repository,
        utility_contract,
        loss_contract,
        baseline_result,
        candidate_results,
        ground_truth_semantic_digest=ground_truth_semantic_digest,
        sandbox_contract=sandbox_contract,
        runtime=runtime,
    )


def profile_singletons_full_workload_reference(
    planner_session: Any,
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    utility_provider: Any,
    configuration_factory: Callable[[Iterable[str]], Any],
    *,
    ground_truth_semantic_digest: str,
    sandbox_contract: str,
    runtime_metadata: Mapping[str, Any] | None = None,
    estimate_audit: MutableMapping[str, Any] | None = None,
) -> SingletonProfile:
    """Reference evaluator retaining the pre-incremental full-workload behavior."""

    started = time.perf_counter()
    query_ids, utility_contract, loss_contract, repository_by_id = _profile_common_setup(
        snapshot,
        candidate_universe,
        native_repository,
        utility_provider,
        ground_truth_semantic_digest=ground_truth_semantic_digest,
    )

    def evaluate(candidate_ids: Sequence[str]) -> UtilityResult:
        planner_session.activate(configuration_factory(candidate_ids))
        estimates = planner_session.estimate_queries(query_ids)
        estimate_map = _estimate_map(estimates, query_ids)
        result = utility_provider.evaluate(estimate_map)
        _validate_utility_result(result, loss_contract)
        return result

    baseline_result = evaluate(())
    if estimate_audit is not None:
        estimate_audit["baseline"] = {
            item.query_id: item.estimate for item in baseline_result.per_query
        }
        estimate_audit["singletons"] = {}
    candidate_results: list[tuple[Any, UtilityResult]] = []
    incidence_counts: dict[str, int] = {}
    for candidate in candidate_universe.candidates:
        native = repository_by_id[candidate.candidate_id]
        incidence_counts[candidate.candidate_id] = len(
            set(candidate_universe.query_ids_for_candidate(candidate.candidate_id)).intersection(
                query_ids
            )
        )
        result = (
            baseline_result
            if native.state == ABSENT_NATIVE
            else evaluate((candidate.candidate_id,))
        )
        if estimate_audit is not None and native.state == PRESENT:
            estimate_audit["singletons"][candidate.candidate_id] = {
                item.query_id: item.estimate for item in result.per_query
            }
        candidate_results.append(
            (
                candidate,
                result,
            )
        )
    present_profiles = [
        candidate
        for candidate, _ in candidate_results
        if repository_by_id[candidate.candidate_id].state == PRESENT
    ]
    runtime = _incidence_runtime(
        present_candidate_ids=[candidate.candidate_id for candidate in present_profiles],
        incidence_counts=incidence_counts,
        baseline_query_count=len(query_ids),
        singleton_query_count=len(query_ids) * len(present_profiles),
        strategy="full-workload-reference-v1",
        elapsed_seconds=time.perf_counter() - started,
    )
    if runtime_metadata:
        runtime.update(runtime_metadata)
    return _assemble_profile(
        snapshot,
        candidate_universe,
        native_repository,
        utility_contract,
        loss_contract,
        baseline_result,
        candidate_results,
        ground_truth_semantic_digest=ground_truth_semantic_digest,
        sandbox_contract=sandbox_contract,
        runtime=runtime,
    )
