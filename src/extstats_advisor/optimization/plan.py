"""Pure budget-induced screening plans built from SingletonProfile v1."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from extstats_advisor.errors import OptimizationPlanningError, OptimizationPlanValidationError
from extstats_advisor.native_stats.model import ABSENT_NATIVE
from extstats_advisor.optimization.budget import (
    OptimizationBudget,
)
from extstats_advisor.optimization.singleton import (
    SINGLETON_PRECEDENCE_POLICY,
    SingletonProfile,
)

OPTIMIZATION_PLAN_FORMAT_VERSION = "optimization-plan-v1"
OPTIMIZATION_PLAN_V2_FORMAT_VERSION = "optimization-plan-v2"
SCREENING_POLICY = "singleton-prefix-screening-v1"
_DEFAULT_MAX_STATISTICS_COUNT = object()
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise OptimizationPlanValidationError(f"{label} must be a SHA-256 token")
    return value


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OptimizationPlanValidationError(f"{label} must be a non-empty string")
    return value


def _finite(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OptimizationPlanValidationError(f"{label} must be finite")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise OptimizationPlanValidationError(f"{label} must be finite")
    return number


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OptimizationPlanValidationError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class ScreenedCandidate:
    """A PRESENT candidate and its position in the budgeted search space."""

    candidate_id: str
    frozen_precedence_rank: int
    search_space_position: int
    singleton_objective: float
    singleton_improvement: float

    def __post_init__(self) -> None:
        _token(self.candidate_id, "screened candidate_id")
        for value, label in (
            (self.frozen_precedence_rank, "frozen precedence rank"),
            (self.search_space_position, "search-space position"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise OptimizationPlanValidationError(f"{label} must be a positive integer")
        _finite(self.singleton_objective, "screened singleton objective", nonnegative=True)
        _finite(self.singleton_improvement, "screened singleton improvement")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "frozen_precedence_rank": self.frozen_precedence_rank,
            "search_space_position": self.search_space_position,
            "singleton_objective": self.singleton_objective,
            "singleton_improvement": self.singleton_improvement,
        }


@dataclass(frozen=True, slots=True)
class OptimizationPlan:
    """Immutable, deterministic restriction of a SingletonProfile search space."""

    source_snapshot_semantic_digest: str
    candidate_universe_semantic_digest: str
    native_stats_repository_semantic_digest: str
    ground_truth_semantic_digest: str
    singleton_profile_semantic_digest: str
    utility_contract: str
    loss_contract: str
    precedence_policy: str
    screening_policy: str
    budget: OptimizationBudget
    actionable_candidate_count: int
    screened_candidate_ids: tuple[str, ...]
    screened_candidates: tuple[ScreenedCandidate, ...]
    excluded_actionable_candidate_ids: tuple[str, ...]
    absent_native_candidate_ids: tuple[str, ...]
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    created_at: str | None = field(default=None, repr=False, compare=False)
    semantic_digest: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("source snapshot semantic digest", self.source_snapshot_semantic_digest),
            ("candidate universe semantic digest", self.candidate_universe_semantic_digest),
            ("native repository semantic digest", self.native_stats_repository_semantic_digest),
            ("ground truth semantic digest", self.ground_truth_semantic_digest),
            ("singleton profile semantic digest", self.singleton_profile_semantic_digest),
        ):
            _digest(value, label)
        for label, value in (
            ("utility contract", self.utility_contract),
            ("loss contract", self.loss_contract),
        ):
            _token(value, label)
        if self.precedence_policy != SINGLETON_PRECEDENCE_POLICY:
            raise OptimizationPlanValidationError("unsupported singleton precedence policy")
        if self.screening_policy != SCREENING_POLICY:
            raise OptimizationPlanValidationError("unsupported screening policy")
        if not isinstance(self.budget, OptimizationBudget):
            raise OptimizationPlanValidationError("optimization budget is invalid")
        if not isinstance(self.screened_candidates, tuple) or any(
            not isinstance(record, ScreenedCandidate) for record in self.screened_candidates
        ):
            raise OptimizationPlanValidationError("screened candidate records are invalid")
        _count(self.actionable_candidate_count, "actionable candidate count")
        if not isinstance(self.runtime_metadata, Mapping):
            raise OptimizationPlanValidationError("optimization runtime_metadata must be an object")
        object.__setattr__(self, "runtime_metadata", MappingProxyType(dict(self.runtime_metadata)))
        if self.created_at is not None:
            _token(self.created_at, "optimization created_at")
        if self.semantic_digest is not None:
            _digest(self.semantic_digest, "optimization semantic digest")

        all_ids = (*self.screened_candidate_ids, *self.excluded_actionable_candidate_ids)
        for label, ids in (
            ("screened candidate IDs", self.screened_candidate_ids),
            ("excluded actionable candidate IDs", self.excluded_actionable_candidate_ids),
            ("absent native candidate IDs", self.absent_native_candidate_ids),
        ):
            if not isinstance(ids, tuple) or any(
                not isinstance(value, str) or not value for value in ids
            ):
                raise OptimizationPlanValidationError(f"{label} must contain candidate IDs")
            if len(ids) != len(set(ids)):
                raise OptimizationPlanValidationError(f"{label} must not contain duplicates")
        if len(all_ids) != len(set(all_ids)):
            raise OptimizationPlanValidationError("screened and excluded actionable IDs overlap")
        if set(all_ids) & set(self.absent_native_candidate_ids):
            raise OptimizationPlanValidationError("actionable and ABSENT_NATIVE IDs overlap")
        if len(all_ids) + len(self.absent_native_candidate_ids) != self.candidate_universe_count:
            raise OptimizationPlanValidationError("candidate counts are inconsistent")
        if len(self.screened_candidate_ids) != len(self.screened_candidates):
            raise OptimizationPlanValidationError("screened candidate records are inconsistent")
        if (
            tuple(record.candidate_id for record in self.screened_candidates)
            != self.screened_candidate_ids
        ):
            raise OptimizationPlanValidationError("screened candidate order is inconsistent")
        for position, record in enumerate(self.screened_candidates, start=1):
            if (
                record.search_space_position != position
                or record.frozen_precedence_rank != position
            ):
                raise OptimizationPlanValidationError(
                    "screened candidate positions are not prefix ranks"
                )
        if len(self.screened_candidate_ids) > self.budget.candidate_limit:
            raise OptimizationPlanValidationError(
                "screened candidate count exceeds candidate_limit"
            )
        if len(all_ids) != self.actionable_candidate_count:
            raise OptimizationPlanValidationError("actionable candidate count is inconsistent")

    @property
    def candidate_universe_count(self) -> int:
        return (
            len(self.screened_candidate_ids)
            + len(self.excluded_actionable_candidate_ids)
            + len(self.absent_native_candidate_ids)
        )

    @property
    def screened_candidate_count(self) -> int:
        return len(self.screened_candidate_ids)

    @property
    def budget_contract(self) -> str:
        return self.budget.contract

    @property
    def max_statistics_count(self) -> int:
        """Maximum selected candidate definitions, defaulting to the v1 limit."""

        return self.budget.effective_max_statistics_count

    @property
    def excluded_actionable_candidate_count(self) -> int:
        return len(self.excluded_actionable_candidate_ids)

    @property
    def absent_native_candidate_count(self) -> int:
        return len(self.absent_native_candidate_ids)

    @property
    def positive_singleton_count(self) -> int:
        return sum(record.singleton_improvement > 0 for record in self.screened_candidates)

    @property
    def neutral_singleton_count(self) -> int:
        return sum(record.singleton_improvement == 0 for record in self.screened_candidates)

    @property
    def negative_singleton_count(self) -> int:
        return sum(record.singleton_improvement < 0 for record in self.screened_candidates)

    @property
    def worst_case_add_configuration_evaluations_after_singletons(self) -> int:
        count = self.screened_candidate_count
        return count * (count - 1) // 2

    def ordered_configuration(self, membership: Iterable[str]) -> tuple[str, ...]:
        """Return membership in screened frozen order, never insertion or lexical order."""

        try:
            requested = tuple(membership)
        except TypeError as exc:
            raise OptimizationPlanningError("configuration membership must be iterable") from exc
        if any(not isinstance(candidate_id, str) or not candidate_id for candidate_id in requested):
            raise OptimizationPlanningError("configuration membership contains an invalid ID")
        if len(requested) != len(set(requested)):
            raise OptimizationPlanningError("configuration membership contains duplicates")
        screened = set(self.screened_candidate_ids)
        unknown = set(requested) - screened
        if unknown:
            raise OptimizationPlanningError(
                f"configuration membership is outside screened space: {sorted(unknown)}"
            )
        selected = set(requested)
        return tuple(
            candidate_id for candidate_id in self.screened_candidate_ids if candidate_id in selected
        )

    def semantic_manifest(self) -> dict[str, Any]:
        return {
            "format_version": (
                OPTIMIZATION_PLAN_FORMAT_VERSION
                if self.budget.max_statistics_count is None
                else OPTIMIZATION_PLAN_V2_FORMAT_VERSION
            ),
            "source_snapshot_semantic_digest": self.source_snapshot_semantic_digest,
            "candidate_universe_semantic_digest": self.candidate_universe_semantic_digest,
            "native_stats_repository_semantic_digest": self.native_stats_repository_semantic_digest,
            "ground_truth_semantic_digest": self.ground_truth_semantic_digest,
            "singleton_profile_semantic_digest": self.singleton_profile_semantic_digest,
            "utility": {
                "utility_contract": self.utility_contract,
                "loss_contract": self.loss_contract,
            },
            "precedence": {"policy": self.precedence_policy},
            "screening": {"policy": self.screening_policy},
            "budget": self.budget.to_dict(),
            "actionable_candidate_count": self.actionable_candidate_count,
            "screened_candidate_count": self.screened_candidate_count,
            "screened_candidate_ids": list(self.screened_candidate_ids),
            "screened_candidates": [record.to_dict() for record in self.screened_candidates],
            "excluded_actionable_candidate_ids": list(self.excluded_actionable_candidate_ids),
            "absent_native_candidate_ids": list(self.absent_native_candidate_ids),
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


def create_optimization_plan(
    singleton_profile: SingletonProfile,
    *,
    candidate_limit: int,
    max_statistics_count: int | None | object = _DEFAULT_MAX_STATISTICS_COUNT,
    wall_clock_seconds: float = 300.0,
) -> OptimizationPlan:
    """Create a pure budgeted prefix without planner or utility evaluation.

    Omitting ``max_statistics_count`` creates the canonical v2 plan with
    ``B == candidate_limit``.  Passing ``None`` explicitly retains the v1
    reference-plan compatibility path; loading an existing v1 artifact never
    rewrites it.
    """

    if not isinstance(singleton_profile, SingletonProfile):
        raise OptimizationPlanningError("singleton_profile must be a SingletonProfile")
    resolved_max_statistics_count = (
        candidate_limit
        if max_statistics_count is _DEFAULT_MAX_STATISTICS_COUNT
        else max_statistics_count
    )
    budget = OptimizationBudget(candidate_limit, wall_clock_seconds, resolved_max_statistics_count)
    actionable = tuple(singleton_profile.frozen_ordered_candidate_ids)
    limit = min(budget.candidate_limit, len(actionable))
    screened_ids = actionable[:limit]
    excluded_ids = actionable[limit:]
    profile_by_id = {
        profile.candidate_id: profile for profile in singleton_profile.candidate_profiles
    }
    screened_records = tuple(
        ScreenedCandidate(
            candidate_id,
            position,
            position,
            profile_by_id[candidate_id].singleton_objective,
            profile_by_id[candidate_id].improvement,
        )
        for position, candidate_id in enumerate(screened_ids, start=1)
    )
    return OptimizationPlan(
        singleton_profile.source_snapshot_semantic_digest,
        singleton_profile.candidate_universe_semantic_digest,
        singleton_profile.native_stats_repository_semantic_digest,
        singleton_profile.ground_truth_semantic_digest,
        singleton_profile.computed_semantic_digest,
        singleton_profile.utility_contract,
        singleton_profile.loss_contract,
        singleton_profile.precedence_policy,
        SCREENING_POLICY,
        budget,
        len(actionable),
        screened_ids,
        screened_records,
        excluded_ids,
        tuple(
            sorted(
                profile.candidate_id
                for profile in singleton_profile.candidate_profiles
                if profile.native_state == ABSENT_NATIVE
            )
        ),
    )
