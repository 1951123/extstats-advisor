"""Generic singleton utility profiling and frozen precedence models."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
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
) -> SingletonProfile:
    """Evaluate baseline and every PRESENT singleton through one session."""

    _validate_sources(snapshot, candidate_universe, native_repository)
    snapshot_digest = snapshot.semantic_digest
    query_ids = _positive_query_ids(snapshot, candidate_universe)
    utility_contract = getattr(utility_provider, "utility_contract", None)
    if not isinstance(utility_contract, str) or not utility_contract:
        utility_contract = getattr(utility_provider, "contract_version", None)
    loss_contract = getattr(utility_provider, "loss_contract", None)
    if not isinstance(utility_contract, str) or not utility_contract:
        raise SingletonProfilingError("utility provider contract is required")
    if not isinstance(loss_contract, str) or not loss_contract:
        raise SingletonProfilingError("utility provider loss contract is required")
    if not _SHA256.fullmatch(ground_truth_semantic_digest):
        raise SingletonProfilingError("ground truth semantic digest is invalid")

    def evaluate(candidate_ids: Sequence[str]) -> UtilityResult:
        planner_session.activate(configuration_factory(candidate_ids))
        estimates = planner_session.estimate_queries(query_ids)
        estimate_map = {estimate.query_id: estimate.estimated_rows for estimate in estimates}
        if tuple(estimate_map) != query_ids:
            raise SingletonProfilingError("planner did not return the requested query estimates")
        result = utility_provider.evaluate(estimate_map)
        if result.loss_contract != loss_contract:
            raise SingletonProfilingError("utility result loss contract changed during profiling")
        return result

    baseline_result = evaluate(())
    repository_by_id = {
        candidate.candidate_id: candidate for candidate in native_repository.candidate_models
    }
    profiles: list[CandidateSingletonProfile] = []
    planner_query_estimate_count = baseline_result.query_count
    for candidate in candidate_universe.candidates:
        native = repository_by_id[candidate.candidate_id]
        if native.state == ABSENT_NATIVE:
            profiles.append(
                CandidateSingletonProfile(
                    candidate.candidate_id,
                    native.state,
                    candidate.static_precedence_rank,
                    baseline_result.objective,
                    0.0,
                    None,
                )
            )
            continue
        result = evaluate((candidate.candidate_id,))
        planner_query_estimate_count += result.query_count
        profiles.append(
            CandidateSingletonProfile(
                candidate.candidate_id,
                native.state,
                candidate.static_precedence_rank,
                result.objective,
                baseline_result.objective - result.objective,
                1,
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
    backend_contract = str(native_repository.backend_contract)
    profile_runtime = {
        "baseline_configuration_count": 1,
        "singleton_configuration_count": len(present_profiles),
        "planner_query_estimate_count": planner_query_estimate_count,
    }
    if runtime_metadata:
        profile_runtime.update(runtime_metadata)
    return SingletonProfile(
        snapshot_digest,
        candidate_universe.semantic_digest,
        native_repository.semantic_digest,
        ground_truth_semantic_digest,
        sandbox_contract,
        backend_contract,
        native_repository.server_version,
        native_repository.server_version_num,
        native_repository.ordinary_stats_fingerprint,
        utility_contract,
        loss_contract,
        SINGLETON_PRECEDENCE_POLICY,
        BaselineProfile(baseline_result.objective),
        ranked_profiles,
        frozen_order,
        profile_runtime,
    )
