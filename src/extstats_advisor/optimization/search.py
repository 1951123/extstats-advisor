"""DBMS-neutral serial greedy ADD search over an OptimizationPlan."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from extstats_advisor.errors import (
    SearchBudgetExpired,
    SearchError,
    SearchResultValidationError,
)
from extstats_advisor.optimization.budget import OptimizationBudget
from extstats_advisor.optimization.plan import OptimizationPlan
from extstats_advisor.optimization.singleton import SingletonProfile
from extstats_advisor.utility.model import UtilityResult

SEARCH_RESULT_FORMAT_VERSION = "optimization-search-result-v1"
GREEDY_ADD_SEARCH_POLICY = "greedy-add-strict-improvement-v1"
TERMINATION_LOCAL_OPTIMUM = "local-optimum"
TERMINATION_ALL_SELECTED = "all-screened-candidates-selected"
TERMINATION_BUDGET_BEFORE_ROUND = "budget-expired-before-round"
TERMINATION_BUDGET_INCOMPLETE_ROUND = "budget-expired-incomplete-round"
TERMINATION_REASONS = frozenset(
    {
        TERMINATION_LOCAL_OPTIMUM,
        TERMINATION_ALL_SELECTED,
        TERMINATION_BUDGET_BEFORE_ROUND,
        TERMINATION_BUDGET_INCOMPLETE_ROUND,
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SearchResultValidationError(f"{label} must be a SHA-256 token")
    return value


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SearchResultValidationError(f"{label} must be a non-empty string")
    return value


def _finite(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchResultValidationError(f"{label} must be finite")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise SearchResultValidationError(f"{label} must be finite")
    return number


class MonotonicClock(Protocol):
    def monotonic(self) -> float:
        """Return a monotonic timestamp."""


def _now(clock: Callable[[], float] | MonotonicClock) -> float:
    value = clock() if callable(clock) else clock.monotonic()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchError("monotonic clock returned an invalid value")
    value = float(value)
    if not math.isfinite(value):
        raise SearchError("monotonic clock returned a non-finite value")
    return value


class SearchDeadline:
    """Injectable monotonic deadline used by generic and backend search code."""

    def __init__(
        self,
        budget: OptimizationBudget,
        clock: Callable[[], float] | MonotonicClock = time.monotonic,
    ) -> None:
        self.budget = budget
        self.clock = clock
        self.started_at = _now(clock)
        self.deadline = self.started_at + budget.wall_clock_seconds

    def now(self) -> float:
        return _now(self.clock)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - self.now())

    @property
    def expired(self) -> bool:
        return self.now() >= self.deadline

    def ensure_available(self) -> None:
        if self.expired:
            raise SearchBudgetExpired("optimization search wall-clock budget expired")


@dataclass(frozen=True, slots=True)
class PlannerIdentity:
    sandbox_contract: str
    backend_contract: str
    server_version: str
    server_version_num: int
    ordinary_stats_fingerprint: str

    def __post_init__(self) -> None:
        for label, value in (
            ("sandbox contract", self.sandbox_contract),
            ("backend contract", self.backend_contract),
            ("server version", self.server_version),
        ):
            _token(value, label)
        if (
            isinstance(self.server_version_num, bool)
            or not isinstance(self.server_version_num, int)
            or self.server_version_num < 1
        ):
            raise SearchResultValidationError("server_version_num must be positive")
        _digest(self.ordinary_stats_fingerprint, "ordinary statistics fingerprint")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sandbox_contract": self.sandbox_contract,
            "backend_contract": self.backend_contract,
            "server_version": self.server_version,
            "server_version_num": self.server_version_num,
            "ordinary_stats_fingerprint": self.ordinary_stats_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SearchEvaluation:
    candidate_id: str
    ordered_candidate_ids: tuple[str, ...]
    objective: float

    def __post_init__(self) -> None:
        _token(self.candidate_id, "search evaluation candidate_id")
        if not isinstance(self.ordered_candidate_ids, tuple) or any(
            not isinstance(value, str) or not value for value in self.ordered_candidate_ids
        ):
            raise SearchResultValidationError("search evaluation order is invalid")
        if len(self.ordered_candidate_ids) != len(set(self.ordered_candidate_ids)):
            raise SearchResultValidationError("search evaluation order contains duplicates")
        _finite(self.objective, "search evaluation objective", nonnegative=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "ordered_candidate_ids": list(self.ordered_candidate_ids),
            "objective": self.objective,
        }


@dataclass(frozen=True, slots=True)
class CompletedSearchRound:
    round_index: int
    evaluations: tuple[SearchEvaluation, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.round_index, bool)
            or not isinstance(self.round_index, int)
            or self.round_index < 2
        ):
            raise SearchResultValidationError("completed search round index is invalid")
        if not isinstance(self.evaluations, tuple) or not self.evaluations:
            raise SearchResultValidationError("completed search round must have evaluations")
        candidate_ids = [evaluation.candidate_id for evaluation in self.evaluations]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise SearchResultValidationError(
                "completed search round contains duplicate candidates"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "evaluations": [evaluation.to_dict() for evaluation in self.evaluations],
        }


@dataclass(frozen=True, slots=True)
class AcceptedMove:
    round_index: int
    added_candidate_id: str
    objective_before: float
    objective_after: float
    improvement: float
    ordered_candidate_ids_after: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.round_index, bool)
            or not isinstance(self.round_index, int)
            or self.round_index < 1
        ):
            raise SearchResultValidationError("accepted move round index is invalid")
        _token(self.added_candidate_id, "accepted move candidate_id")
        _finite(self.objective_before, "accepted move objective_before", nonnegative=True)
        _finite(self.objective_after, "accepted move objective_after", nonnegative=True)
        _finite(self.improvement, "accepted move improvement")
        if self.objective_after >= self.objective_before:
            raise SearchResultValidationError("accepted move is not a strict improvement")
        if self.improvement != self.objective_before - self.objective_after:
            raise SearchResultValidationError("accepted move improvement is inconsistent")
        if not isinstance(self.ordered_candidate_ids_after, tuple) or any(
            not isinstance(value, str) or not value for value in self.ordered_candidate_ids_after
        ):
            raise SearchResultValidationError("accepted move order is invalid")
        if len(self.ordered_candidate_ids_after) != len(set(self.ordered_candidate_ids_after)):
            raise SearchResultValidationError("accepted move order contains duplicates")
        if self.added_candidate_id not in self.ordered_candidate_ids_after:
            raise SearchResultValidationError("accepted move order omits added candidate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "added_candidate_id": self.added_candidate_id,
            "objective_before": self.objective_before,
            "objective_after": self.objective_after,
            "improvement": self.improvement,
            "ordered_candidate_ids_after": list(self.ordered_candidate_ids_after),
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    source_snapshot_semantic_digest: str
    candidate_universe_semantic_digest: str
    native_stats_repository_semantic_digest: str
    ground_truth_semantic_digest: str
    singleton_profile_semantic_digest: str
    optimization_plan_semantic_digest: str
    planner_identity: PlannerIdentity
    search_policy: str
    utility_contract: str
    loss_contract: str
    budget: OptimizationBudget
    baseline_objective: float
    final_objective: float
    improvement: float
    final_ordered_candidate_ids: tuple[str, ...]
    termination_reason: str
    first_round_evaluations: tuple[SearchEvaluation, ...]
    completed_rounds: tuple[CompletedSearchRound, ...]
    accepted_moves: tuple[AcceptedMove, ...]
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
            ("optimization plan semantic digest", self.optimization_plan_semantic_digest),
        ):
            _digest(value, label)
        if not isinstance(self.planner_identity, PlannerIdentity):
            raise SearchResultValidationError("planner identity is invalid")
        if self.search_policy != GREEDY_ADD_SEARCH_POLICY:
            raise SearchResultValidationError("unsupported search policy")
        _token(self.utility_contract, "utility contract")
        _token(self.loss_contract, "loss contract")
        if not isinstance(self.budget, OptimizationBudget):
            raise SearchResultValidationError("search budget is invalid")
        _finite(self.baseline_objective, "baseline objective", nonnegative=True)
        _finite(self.final_objective, "final objective", nonnegative=True)
        _finite(self.improvement, "search improvement")
        if self.improvement != self.baseline_objective - self.final_objective:
            raise SearchResultValidationError("search improvement is inconsistent")
        if self.termination_reason not in TERMINATION_REASONS:
            raise SearchResultValidationError("unknown search termination reason")
        for label, ids in (("final candidate order", self.final_ordered_candidate_ids),):
            if not isinstance(ids, tuple) or any(
                not isinstance(value, str) or not value for value in ids
            ):
                raise SearchResultValidationError(f"{label} is invalid")
            if len(ids) != len(set(ids)):
                raise SearchResultValidationError(f"{label} contains duplicates")
        for label, records in (
            ("first round evaluations", self.first_round_evaluations),
            ("completed rounds", self.completed_rounds),
            ("accepted moves", self.accepted_moves),
        ):
            if not isinstance(records, tuple):
                raise SearchResultValidationError(f"{label} is invalid")
        if any(not isinstance(item, SearchEvaluation) for item in self.first_round_evaluations):
            raise SearchResultValidationError("first round evaluations are invalid")
        if any(not isinstance(item, CompletedSearchRound) for item in self.completed_rounds):
            raise SearchResultValidationError("completed rounds are invalid")
        if any(not isinstance(item, AcceptedMove) for item in self.accepted_moves):
            raise SearchResultValidationError("accepted moves are invalid")
        if tuple(round_.round_index for round_ in self.completed_rounds) != tuple(
            range(2, len(self.completed_rounds) + 2)
        ):
            raise SearchResultValidationError("completed search round indexes are not contiguous")
        if tuple(move.round_index for move in self.accepted_moves) != tuple(
            range(1, len(self.accepted_moves) + 1)
        ):
            raise SearchResultValidationError("accepted move indexes are not contiguous")
        objective = self.baseline_objective
        for move in self.accepted_moves:
            if move.objective_before != objective:
                raise SearchResultValidationError("accepted move objective chain is inconsistent")
            objective = move.objective_after
        if self.accepted_moves and self.final_objective != objective:
            raise SearchResultValidationError("final objective does not match accepted moves")
        if not self.accepted_moves and self.final_objective != self.baseline_objective:
            raise SearchResultValidationError("search without accepted moves changed objective")
        if not isinstance(self.runtime_metadata, Mapping):
            raise SearchResultValidationError("search runtime_metadata must be an object")
        object.__setattr__(self, "runtime_metadata", MappingProxyType(dict(self.runtime_metadata)))
        if self.created_at is not None:
            _token(self.created_at, "search created_at")
        if self.semantic_digest is not None:
            _digest(self.semantic_digest, "search semantic digest")

    @property
    def selected_candidate_count(self) -> int:
        return len(self.final_ordered_candidate_ids)

    def semantic_manifest(self) -> dict[str, Any]:
        return {
            "format_version": SEARCH_RESULT_FORMAT_VERSION,
            "source_snapshot_semantic_digest": self.source_snapshot_semantic_digest,
            "candidate_universe_semantic_digest": self.candidate_universe_semantic_digest,
            "native_stats_repository_semantic_digest": self.native_stats_repository_semantic_digest,
            "ground_truth_semantic_digest": self.ground_truth_semantic_digest,
            "singleton_profile_semantic_digest": self.singleton_profile_semantic_digest,
            "optimization_plan_semantic_digest": self.optimization_plan_semantic_digest,
            "planner": self.planner_identity.to_dict(),
            "search_policy": self.search_policy,
            "utility": {
                "utility_contract": self.utility_contract,
                "loss_contract": self.loss_contract,
            },
            "budget": self.budget.to_dict(),
            "baseline_objective": self.baseline_objective,
            "final_objective": self.final_objective,
            "improvement": self.improvement,
            "final_ordered_candidate_ids": list(self.final_ordered_candidate_ids),
            "termination_reason": self.termination_reason,
            "first_round_source": "singleton-profile-cache",
            "first_round_evaluations": [
                evaluation.to_dict() for evaluation in self.first_round_evaluations
            ],
            "completed_rounds": [round_.to_dict() for round_ in self.completed_rounds],
            "accepted_moves": [move.to_dict() for move in self.accepted_moves],
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


ConfigurationEvaluator = Callable[[frozenset[str], SearchDeadline], UtilityResult]
RuntimeMetadataProvider = Callable[[], Mapping[str, Any]]


def _provider_contracts(utility_provider: Any) -> tuple[str, str]:
    utility_contract = getattr(utility_provider, "utility_contract", None)
    if not isinstance(utility_contract, str) or not utility_contract:
        utility_contract = getattr(utility_provider, "contract_version", None)
    loss_contract = getattr(utility_provider, "loss_contract", None)
    if not isinstance(utility_contract, str) or not utility_contract:
        raise SearchError("utility provider contract is required")
    if not isinstance(loss_contract, str) or not loss_contract:
        raise SearchError("utility provider loss contract is required")
    return utility_contract, loss_contract


def _validate_search_inputs(
    singleton_profile: SingletonProfile,
    plan: OptimizationPlan,
    utility_provider: Any,
) -> tuple[str, str]:
    if plan.singleton_profile_semantic_digest != singleton_profile.computed_semantic_digest:
        raise SearchError("optimization plan does not bind the supplied singleton profile")
    if plan.source_snapshot_semantic_digest != singleton_profile.source_snapshot_semantic_digest:
        raise SearchError("search snapshot binding is inconsistent")
    if (
        plan.candidate_universe_semantic_digest
        != singleton_profile.candidate_universe_semantic_digest
    ):
        raise SearchError("search candidate-universe binding is inconsistent")
    if (
        plan.native_stats_repository_semantic_digest
        != singleton_profile.native_stats_repository_semantic_digest
    ):
        raise SearchError("search native-repository binding is inconsistent")
    if plan.ground_truth_semantic_digest != singleton_profile.ground_truth_semantic_digest:
        raise SearchError("search ground-truth binding is inconsistent")
    utility_contract, loss_contract = _provider_contracts(utility_provider)
    if utility_contract != plan.utility_contract or loss_contract != plan.loss_contract:
        raise SearchError("utility/loss contract does not match optimization plan")
    if (
        plan.screened_candidate_ids + plan.excluded_actionable_candidate_ids
        != singleton_profile.frozen_ordered_candidate_ids
    ):
        raise SearchError("optimization plan order does not match singleton profile")
    return utility_contract, loss_contract


def greedy_add_search(
    singleton_profile: SingletonProfile,
    plan: OptimizationPlan,
    utility_provider: Any,
    evaluate_configuration: ConfigurationEvaluator,
    planner_identity: PlannerIdentity,
    *,
    clock: Callable[[], float] | MonotonicClock = time.monotonic,
    runtime_metadata_provider: RuntimeMetadataProvider | None = None,
) -> SearchResult:
    """Run serial strict-improvement ADD with cached singleton first round."""

    utility_contract, loss_contract = _validate_search_inputs(
        singleton_profile, plan, utility_provider
    )
    deadline = SearchDeadline(plan.budget, clock)
    screened = plan.screened_candidate_ids
    first_round = tuple(
        SearchEvaluation(
            record.candidate_id,
            plan.ordered_configuration((record.candidate_id,)),
            record.singleton_objective,
        )
        for record in plan.screened_candidates
    )
    live_configuration_evaluation_count = 0
    partial_final_round_evaluation_count = 0
    completed_rounds: list[CompletedSearchRound] = []
    accepted_moves: list[AcceptedMove] = []
    current_membership: set[str] = set()
    current_objective = singleton_profile.baseline.objective

    def finish(termination_reason: str) -> SearchResult:
        runtime = {
            "cached_singleton_configuration_count": len(screened),
            "live_configuration_evaluation_count": live_configuration_evaluation_count,
            "planner_query_estimate_count": None,
            "completed_round_count": len(completed_rounds),
            "accepted_move_count": len(accepted_moves),
            "partial_final_round_evaluation_count": partial_final_round_evaluation_count,
            "termination_reason": termination_reason,
            "elapsed_search_seconds": max(0.0, deadline.now() - deadline.started_at),
        }
        if runtime_metadata_provider is not None:
            runtime.update(runtime_metadata_provider())
        return SearchResult(
            singleton_profile.source_snapshot_semantic_digest,
            singleton_profile.candidate_universe_semantic_digest,
            singleton_profile.native_stats_repository_semantic_digest,
            singleton_profile.ground_truth_semantic_digest,
            singleton_profile.computed_semantic_digest,
            plan.computed_semantic_digest,
            planner_identity,
            GREEDY_ADD_SEARCH_POLICY,
            utility_contract,
            loss_contract,
            plan.budget,
            singleton_profile.baseline.objective,
            current_objective,
            singleton_profile.baseline.objective - current_objective,
            plan.ordered_configuration(current_membership),
            termination_reason,
            first_round,
            tuple(completed_rounds),
            tuple(accepted_moves),
            runtime,
        )

    if not screened:
        return finish(TERMINATION_LOCAL_OPTIMUM)
    try:
        deadline.ensure_available()
    except SearchBudgetExpired:
        return finish(TERMINATION_BUDGET_BEFORE_ROUND)
    best_singleton = min(
        enumerate(plan.screened_candidates),
        key=lambda item: (item[1].singleton_objective, item[0]),
    )[1]
    try:
        deadline.ensure_available()
    except SearchBudgetExpired:
        return finish(TERMINATION_BUDGET_BEFORE_ROUND)
    if best_singleton.singleton_objective >= current_objective:
        return finish(TERMINATION_LOCAL_OPTIMUM)
    try:
        deadline.ensure_available()
    except SearchBudgetExpired:
        return finish(TERMINATION_BUDGET_BEFORE_ROUND)
    current_membership.add(best_singleton.candidate_id)
    accepted_moves.append(
        AcceptedMove(
            1,
            best_singleton.candidate_id,
            current_objective,
            best_singleton.singleton_objective,
            current_objective - best_singleton.singleton_objective,
            plan.ordered_configuration(current_membership),
        )
    )
    current_objective = best_singleton.singleton_objective
    if len(current_membership) == len(screened):
        return finish(TERMINATION_ALL_SELECTED)

    while True:
        remaining = tuple(
            candidate_id for candidate_id in screened if candidate_id not in current_membership
        )
        if not remaining:
            return finish(TERMINATION_ALL_SELECTED)
        evaluations: list[SearchEvaluation] = []
        round_evaluation_attempted = False
        for candidate_id in remaining:
            attempted_configuration = False
            try:
                deadline.ensure_available()
                proposed_membership = frozenset((*current_membership, candidate_id))
                attempted_configuration = True
                round_evaluation_attempted = True
                live_configuration_evaluation_count += 1
                result = evaluate_configuration(proposed_membership, deadline)
                if result.loss_contract != loss_contract:
                    raise SearchError("utility result loss contract changed during search")
                deadline.ensure_available()
            except SearchBudgetExpired:
                partial_final_round_evaluation_count = len(evaluations) + int(
                    attempted_configuration
                )
                return finish(
                    TERMINATION_BUDGET_INCOMPLETE_ROUND
                    if round_evaluation_attempted
                    else TERMINATION_BUDGET_BEFORE_ROUND
                )
            evaluations.append(
                SearchEvaluation(
                    candidate_id,
                    plan.ordered_configuration(proposed_membership),
                    result.objective,
                )
            )
        try:
            deadline.ensure_available()
        except SearchBudgetExpired:
            partial_final_round_evaluation_count = len(evaluations)
            return finish(TERMINATION_BUDGET_INCOMPLETE_ROUND)
        completed_rounds.append(CompletedSearchRound(len(completed_rounds) + 2, tuple(evaluations)))
        best = min(
            enumerate(evaluations),
            key=lambda item: (item[1].objective, item[0]),
        )[1]
        if best.objective >= current_objective:
            return finish(TERMINATION_LOCAL_OPTIMUM)
        current_membership.add(best.candidate_id)
        accepted_moves.append(
            AcceptedMove(
                len(accepted_moves) + 1,
                best.candidate_id,
                current_objective,
                best.objective,
                current_objective - best.objective,
                best.ordered_candidate_ids,
            )
        )
        current_objective = best.objective
        if len(current_membership) == len(screened):
            return finish(TERMINATION_ALL_SELECTED)
