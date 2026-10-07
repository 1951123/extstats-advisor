"""Validation and atomic persistence for SearchResult v1."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from extstats_advisor.canonical import canonical_json
from extstats_advisor.errors import (
    CandidateUniverseValidationError,
    NativeStatsRepositoryValidationError,
    OptimizationPlanningError,
    SearchResultValidationError,
)
from extstats_advisor.native_stats.model import PRESENT
from extstats_advisor.native_stats.repository import validate_native_stats_repository_compatibility
from extstats_advisor.optimization.budget import OPTIMIZATION_BUDGET_CONTRACT, OptimizationBudget
from extstats_advisor.optimization.plan import OptimizationPlan
from extstats_advisor.optimization.search import (
    SEARCH_RESULT_FORMAT_VERSION,
    SEARCH_RESULT_V2_FORMAT_VERSION,
    TERMINATION_ALL_SELECTED,
    TERMINATION_BUDGET_BEFORE_ROUND,
    TERMINATION_BUDGET_INCOMPLETE_ROUND,
    TERMINATION_LOCAL_OPTIMUM,
    TERMINATION_MAX_STATISTICS_COUNT,
    AcceptedMove,
    CompletedSearchRound,
    PlannerIdentity,
    SearchEvaluation,
    SearchResult,
)
from extstats_advisor.optimization.singleton import SingletonProfile

_TOP_LEVEL_FIELDS = {
    "format_version",
    "source_snapshot_semantic_digest",
    "candidate_universe_semantic_digest",
    "native_stats_repository_semantic_digest",
    "ground_truth_semantic_digest",
    "singleton_profile_semantic_digest",
    "optimization_plan_semantic_digest",
    "planner",
    "search_policy",
    "utility",
    "budget",
    "baseline_objective",
    "final_objective",
    "improvement",
    "final_ordered_candidate_ids",
    "termination_reason",
    "first_round_source",
    "first_round_evaluations",
    "completed_rounds",
    "accepted_moves",
    "semantic_digest",
    "created_at",
    "runtime_metadata",
}


def _read(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise SearchResultValidationError("search result must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SearchResultValidationError("invalid search result JSON") from exc
    if not isinstance(value, dict):
        raise SearchResultValidationError("search result must be an object")
    return value


def _from_manifest(value: dict[str, Any]) -> SearchResult:
    unknown = set(value) - _TOP_LEVEL_FIELDS
    if unknown:
        raise SearchResultValidationError("search result contains unknown fields")
    format_version = value.get("format_version")
    if format_version not in {SEARCH_RESULT_FORMAT_VERSION, SEARCH_RESULT_V2_FORMAT_VERSION}:
        raise SearchResultValidationError("unknown search result format")
    planner = value.get("planner")
    utility = value.get("utility")
    budget = value.get("budget")
    if not isinstance(planner, dict) or set(planner) != {
        "sandbox_contract",
        "backend_contract",
        "server_version",
        "server_version_num",
        "ordinary_stats_fingerprint",
    }:
        raise SearchResultValidationError("search planner manifest is invalid")
    if not isinstance(utility, dict) or set(utility) != {"utility_contract", "loss_contract"}:
        raise SearchResultValidationError("search utility manifest is invalid")
    expected_budget_fields = {"contract", "candidate_limit", "wall_clock_seconds"}
    if format_version == SEARCH_RESULT_V2_FORMAT_VERSION:
        expected_budget_fields.add("max_statistics_count")
    if not isinstance(budget, dict) or set(budget) != expected_budget_fields:
        raise SearchResultValidationError("search budget manifest is invalid")
    if budget["contract"] != OPTIMIZATION_BUDGET_CONTRACT:
        raise SearchResultValidationError("unsupported search budget contract")
    if value.get("first_round_source") != "singleton-profile-cache":
        raise SearchResultValidationError("search first-round source is invalid")
    for field in (
        "final_ordered_candidate_ids",
        "first_round_evaluations",
        "completed_rounds",
        "accepted_moves",
    ):
        if not isinstance(value.get(field), list):
            raise SearchResultValidationError(f"search {field} must be a list")
    try:
        first_round = tuple(
            SearchEvaluation(
                record["candidate_id"],
                tuple(record["ordered_candidate_ids"]),
                record["objective"],
            )
            for record in value["first_round_evaluations"]
            if isinstance(record, dict)
            and set(record) == {"candidate_id", "ordered_candidate_ids", "objective"}
        )
        if len(first_round) != len(value["first_round_evaluations"]):
            raise SearchResultValidationError("search first-round evaluation is malformed")
        completed_rounds = []
        for record in value["completed_rounds"]:
            if not isinstance(record, dict) or set(record) != {"round_index", "evaluations"}:
                raise SearchResultValidationError("search completed round is malformed")
            evaluations = []
            for evaluation in record["evaluations"]:
                if not isinstance(evaluation, dict) or set(evaluation) != {
                    "candidate_id",
                    "ordered_candidate_ids",
                    "objective",
                }:
                    raise SearchResultValidationError("search round evaluation is malformed")
                evaluations.append(
                    SearchEvaluation(
                        evaluation["candidate_id"],
                        tuple(evaluation["ordered_candidate_ids"]),
                        evaluation["objective"],
                    )
                )
            completed_rounds.append(CompletedSearchRound(record["round_index"], tuple(evaluations)))
        accepted_moves = []
        for record in value["accepted_moves"]:
            if not isinstance(record, dict) or set(record) != {
                "round_index",
                "added_candidate_id",
                "objective_before",
                "objective_after",
                "improvement",
                "ordered_candidate_ids_after",
            }:
                raise SearchResultValidationError("search accepted move is malformed")
            accepted_moves.append(
                AcceptedMove(
                    record["round_index"],
                    record["added_candidate_id"],
                    record["objective_before"],
                    record["objective_after"],
                    record["improvement"],
                    tuple(record["ordered_candidate_ids_after"]),
                )
            )
        result = SearchResult(
            value["source_snapshot_semantic_digest"],
            value["candidate_universe_semantic_digest"],
            value["native_stats_repository_semantic_digest"],
            value["ground_truth_semantic_digest"],
            value["singleton_profile_semantic_digest"],
            value["optimization_plan_semantic_digest"],
            PlannerIdentity(
                planner["sandbox_contract"],
                planner["backend_contract"],
                planner["server_version"],
                planner["server_version_num"],
                planner["ordinary_stats_fingerprint"],
            ),
            value["search_policy"],
            utility["utility_contract"],
            utility["loss_contract"],
            OptimizationBudget(
                budget["candidate_limit"],
                budget["wall_clock_seconds"],
                budget.get("max_statistics_count"),
            ),
            value["baseline_objective"],
            value["final_objective"],
            value["improvement"],
            tuple(value["final_ordered_candidate_ids"]),
            value["termination_reason"],
            first_round,
            tuple(completed_rounds),
            tuple(accepted_moves),
            value.get("runtime_metadata", {}),
            value.get("created_at"),
            value.get("semantic_digest"),
            format_version=format_version,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OptimizationPlanningError,
        SearchResultValidationError,
    ) as exc:
        if isinstance(exc, SearchResultValidationError):
            raise
        raise SearchResultValidationError("search result is incomplete or invalid") from exc
    if result.semantic_digest != result.computed_semantic_digest:
        raise SearchResultValidationError("search result semantic digest mismatch")
    return result


def _ground_truth_digest(ground_truth: Any) -> str:
    digest = getattr(ground_truth, "semantic_digest", None)
    return digest if isinstance(digest, str) else ground_truth.computed_semantic_digest


def _validate_bindings(
    result: SearchResult,
    snapshot: Any | None,
    candidate_universe: Any | None,
    native_repository: Any | None,
    ground_truth: Any | None,
    singleton_profile: SingletonProfile | None,
    optimization_plan: OptimizationPlan | None,
) -> None:
    for label, source, expected in (
        ("snapshot", snapshot, result.source_snapshot_semantic_digest),
        ("candidate universe", candidate_universe, result.candidate_universe_semantic_digest),
        ("native repository", native_repository, result.native_stats_repository_semantic_digest),
    ):
        if source is not None and getattr(source, "semantic_digest", None) != expected:
            raise SearchResultValidationError(f"search {label} digest mismatch")
    if ground_truth is not None and result.ground_truth_semantic_digest != _ground_truth_digest(
        ground_truth
    ):
        raise SearchResultValidationError("search ground truth digest mismatch")
    if singleton_profile is not None:
        if result.singleton_profile_semantic_digest != singleton_profile.computed_semantic_digest:
            raise SearchResultValidationError("search singleton profile digest mismatch")
        if result.baseline_objective != singleton_profile.baseline.objective:
            raise SearchResultValidationError("search baseline objective mismatch")
        if (
            result.planner_identity.sandbox_contract != singleton_profile.sandbox_contract
            or result.planner_identity.backend_contract != singleton_profile.backend_contract
            or result.planner_identity.server_version != singleton_profile.server_version
            or result.planner_identity.server_version_num != singleton_profile.server_version_num
            or result.planner_identity.ordinary_stats_fingerprint
            != singleton_profile.ordinary_stats_fingerprint
        ):
            raise SearchResultValidationError("search planner identity mismatch")
    if optimization_plan is not None:
        if result.optimization_plan_semantic_digest != optimization_plan.computed_semantic_digest:
            raise SearchResultValidationError("search optimization plan digest mismatch")
        expected_format = (
            SEARCH_RESULT_FORMAT_VERSION
            if optimization_plan.budget.max_statistics_count is None
            else SEARCH_RESULT_V2_FORMAT_VERSION
        )
        if result.format_version != expected_format:
            raise SearchResultValidationError("search result format does not match plan version")
        if result.budget != optimization_plan.budget:
            raise SearchResultValidationError("search budget mismatch")
        if result.utility_contract != optimization_plan.utility_contract:
            raise SearchResultValidationError("search utility contract mismatch")
        if result.loss_contract != optimization_plan.loss_contract:
            raise SearchResultValidationError("search loss contract mismatch")
        if singleton_profile is not None:
            _validate_search_history(result, singleton_profile, optimization_plan)
    if snapshot is not None and candidate_universe is not None and native_repository is not None:
        try:
            validate_native_stats_repository_compatibility(
                native_repository, snapshot, candidate_universe
            )
        except (NativeStatsRepositoryValidationError, CandidateUniverseValidationError) as exc:
            raise SearchResultValidationError("search source artifacts are incompatible") from exc


def _validate_search_history(
    result: SearchResult, profile: SingletonProfile, plan: OptimizationPlan
) -> None:
    expected_first = plan.screened_candidate_ids
    if tuple(item.candidate_id for item in result.first_round_evaluations) != expected_first:
        raise SearchResultValidationError("search first-round cache candidates mismatch")
    profile_by_id = {item.candidate_id: item for item in profile.candidate_profiles}
    for item in result.first_round_evaluations:
        cached = profile_by_id.get(item.candidate_id)
        if cached is None or cached.native_state != PRESENT:
            raise SearchResultValidationError("search first-round candidate is not PRESENT")
        if item.ordered_candidate_ids != plan.ordered_configuration((item.candidate_id,)):
            raise SearchResultValidationError("search first-round order mismatch")
        if item.objective != cached.singleton_objective:
            raise SearchResultValidationError("search first-round objective mismatch")

    current_membership: set[str] = set()
    current_objective = profile.baseline.objective
    move_index = 0
    if result.accepted_moves:
        first = result.accepted_moves[0]
        best = (
            min(
                enumerate(result.first_round_evaluations),
                key=lambda item: (item[1].objective, item[0]),
            )[1]
            if result.first_round_evaluations
            else None
        )
        if best is None or best.objective >= current_objective:
            raise SearchResultValidationError("first accepted move lacks strict cached improvement")
        if first.added_candidate_id != best.candidate_id:
            raise SearchResultValidationError("first accepted move is not cached singleton minimum")
        if (
            first.objective_before != current_objective
            or first.objective_after != best.objective
            or first.ordered_candidate_ids_after
            != plan.ordered_configuration((first.added_candidate_id,))
        ):
            raise SearchResultValidationError("first accepted move objective mismatch")
        current_membership.add(first.added_candidate_id)
        current_objective = first.objective_after
        move_index = 1
    elif result.completed_rounds:
        raise SearchResultValidationError("live rounds exist without a cached accepted move")

    for round_ in result.completed_rounds:
        remaining = tuple(
            candidate_id
            for candidate_id in plan.screened_candidate_ids
            if candidate_id not in current_membership
        )
        if tuple(item.candidate_id for item in round_.evaluations) != remaining:
            raise SearchResultValidationError("complete search round candidates mismatch")
        for item in round_.evaluations:
            expected_order = plan.ordered_configuration((*current_membership, item.candidate_id))
            if item.ordered_candidate_ids != expected_order:
                raise SearchResultValidationError("complete search round order mismatch")
        best = min(
            enumerate(round_.evaluations),
            key=lambda item: (item[1].objective, item[0]),
        )[1]
        improves = best.objective < current_objective
        if improves:
            if move_index >= len(result.accepted_moves):
                raise SearchResultValidationError("accepted move is missing for improving round")
            move = result.accepted_moves[move_index]
            if move.round_index != round_.round_index:
                raise SearchResultValidationError("accepted move round index mismatch")
            if (
                move.added_candidate_id != best.candidate_id
                or move.objective_before != current_objective
                or move.objective_after != best.objective
                or move.ordered_candidate_ids_after
                != plan.ordered_configuration((*current_membership, best.candidate_id))
            ):
                raise SearchResultValidationError("accepted move does not match round minimum")
            current_membership.add(best.candidate_id)
            current_objective = best.objective
            move_index += 1
        elif round_ is not result.completed_rounds[-1] or move_index != len(result.accepted_moves):
            raise SearchResultValidationError("non-improving round is not the final search round")
    if move_index != len(result.accepted_moves):
        raise SearchResultValidationError("accepted move history has extra moves")
    expected_final = plan.ordered_configuration(current_membership)
    if result.final_ordered_candidate_ids != expected_final:
        raise SearchResultValidationError("final order is inconsistent with accepted membership")
    if result.final_objective != current_objective:
        raise SearchResultValidationError("final objective is inconsistent with search history")
    remaining_at_finish = tuple(
        candidate_id
        for candidate_id in plan.screened_candidate_ids
        if candidate_id not in current_membership
    )
    partial_count = result.runtime_metadata.get("partial_final_round_evaluation_count")
    if partial_count is not None and (
        isinstance(partial_count, bool) or not isinstance(partial_count, int) or partial_count < 0
    ):
        raise SearchResultValidationError("partial final round count is invalid")
    if result.termination_reason == TERMINATION_LOCAL_OPTIMUM:
        if not result.accepted_moves:
            if (
                result.first_round_evaluations
                and min(item.objective for item in result.first_round_evaluations)
                < result.baseline_objective
            ):
                raise SearchResultValidationError("local optimum discarded cached improvement")
        elif not result.completed_rounds:
            raise SearchResultValidationError("local optimum has no completed post-singleton round")
        if partial_count not in (None, 0):
            raise SearchResultValidationError("local optimum has partial round diagnostics")
    elif result.termination_reason == TERMINATION_ALL_SELECTED:
        if not plan.screened_candidate_ids or len(current_membership) != len(
            plan.screened_candidate_ids
        ):
            raise SearchResultValidationError("all-selected termination is incomplete")
        if partial_count not in (None, 0):
            raise SearchResultValidationError(
                "all-selected termination has partial round diagnostics"
            )
    elif result.termination_reason == TERMINATION_MAX_STATISTICS_COUNT:
        if len(current_membership) != plan.max_statistics_count:
            raise SearchResultValidationError("max-statistics termination is incomplete")
        if plan.max_statistics_count >= plan.screened_candidate_count:
            raise SearchResultValidationError(
                "max-statistics termination is not distinct from all-selected"
            )
        if partial_count not in (None, 0):
            raise SearchResultValidationError(
                "max-statistics termination has partial round diagnostics"
            )
    elif result.termination_reason == TERMINATION_BUDGET_BEFORE_ROUND:
        if partial_count not in (None, 0):
            raise SearchResultValidationError("before-round expiry has partial evaluations")
        if not remaining_at_finish and result.accepted_moves:
            raise SearchResultValidationError("before-round expiry follows all-selected state")
    elif result.termination_reason == TERMINATION_BUDGET_INCOMPLETE_ROUND:
        if not result.accepted_moves or not remaining_at_finish:
            raise SearchResultValidationError("incomplete-round expiry has no pending round")
        if partial_count is not None and not 0 < partial_count <= len(remaining_at_finish):
            raise SearchResultValidationError("incomplete-round evaluation count is invalid")


def load_search_result(path: Path) -> SearchResult:
    return _from_manifest(_read(Path(path)))


def validate_search_result(
    path: Path,
    snapshot: Any | None = None,
    candidate_universe: Any | None = None,
    native_repository: Any | None = None,
    ground_truth: Any | None = None,
    singleton_profile: SingletonProfile | None = None,
    optimization_plan: OptimizationPlan | None = None,
) -> dict[str, Any]:
    result = load_search_result(path)
    _validate_bindings(
        result,
        snapshot,
        candidate_universe,
        native_repository,
        ground_truth,
        singleton_profile,
        optimization_plan,
    )
    return search_result_summary(result)


def write_search_result(result: SearchResult, destination: Path) -> str:
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"search result destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(canonical_json(result.to_manifest()) + b"\n")
        load_search_result(temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return result.computed_semantic_digest


def search_result_summary(result: SearchResult) -> dict[str, Any]:
    return {
        "format_version": result.format_version,
        "semantic_digest": result.computed_semantic_digest,
        "search_policy": result.search_policy,
        "termination_reason": result.termination_reason,
        "baseline_objective": result.baseline_objective,
        "final_objective": result.final_objective,
        "improvement": result.improvement,
        "screened_candidate_count": len(result.first_round_evaluations),
        "selected_candidate_count": result.selected_candidate_count,
        "final_ordered_candidate_ids": list(result.final_ordered_candidate_ids),
        "completed_round_count": len(result.completed_rounds),
        "accepted_move_count": len(result.accepted_moves),
        "budget_contract": OPTIMIZATION_BUDGET_CONTRACT,
        "candidate_limit": result.budget.candidate_limit,
        "max_statistics_count": result.budget.max_statistics_count,
        "wall_clock_seconds": result.budget.wall_clock_seconds,
        "utility_contract": result.utility_contract,
        "loss_contract": result.loss_contract,
        "cached_singleton_configuration_count": result.runtime_metadata.get(
            "cached_singleton_configuration_count"
        ),
        "live_configuration_evaluation_count": result.runtime_metadata.get(
            "live_configuration_evaluation_count"
        ),
        "planner_query_estimate_count": result.runtime_metadata.get("planner_query_estimate_count"),
        "partial_final_round_evaluation_count": result.runtime_metadata.get(
            "partial_final_round_evaluation_count"
        ),
        "elapsed_search_seconds": result.runtime_metadata.get("elapsed_search_seconds"),
    }


def inspect_search_result(path: Path) -> dict[str, Any]:
    return search_result_summary(load_search_result(path))
