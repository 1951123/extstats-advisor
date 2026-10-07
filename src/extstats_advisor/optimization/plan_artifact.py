"""Validation and atomic persistence for OptimizationPlan v1."""

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
    OptimizationPlanValidationError,
)
from extstats_advisor.native_stats.model import ABSENT_NATIVE, PRESENT
from extstats_advisor.native_stats.repository import validate_native_stats_repository_compatibility
from extstats_advisor.optimization.budget import (
    OPTIMIZATION_BUDGET_CONTRACT,
    OptimizationBudget,
)
from extstats_advisor.optimization.plan import (
    OPTIMIZATION_PLAN_FORMAT_VERSION,
    OPTIMIZATION_PLAN_V2_FORMAT_VERSION,
    OptimizationPlan,
    ScreenedCandidate,
)
from extstats_advisor.optimization.singleton import SingletonProfile

_TOP_LEVEL_FIELDS = {
    "format_version",
    "source_snapshot_semantic_digest",
    "candidate_universe_semantic_digest",
    "native_stats_repository_semantic_digest",
    "ground_truth_semantic_digest",
    "singleton_profile_semantic_digest",
    "utility",
    "precedence",
    "screening",
    "budget",
    "actionable_candidate_count",
    "screened_candidate_count",
    "screened_candidate_ids",
    "screened_candidates",
    "excluded_actionable_candidate_ids",
    "absent_native_candidate_ids",
    "semantic_digest",
    "created_at",
    "runtime_metadata",
}


def _read(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise OptimizationPlanValidationError("optimization plan must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OptimizationPlanValidationError("invalid optimization plan JSON") from exc
    if not isinstance(value, dict):
        raise OptimizationPlanValidationError("optimization plan must be an object")
    return value


def _from_manifest(value: dict[str, Any]) -> OptimizationPlan:
    unknown = set(value) - _TOP_LEVEL_FIELDS
    if unknown:
        raise OptimizationPlanValidationError("optimization plan contains unknown fields")
    format_version = value.get("format_version")
    if format_version not in {
        OPTIMIZATION_PLAN_FORMAT_VERSION,
        OPTIMIZATION_PLAN_V2_FORMAT_VERSION,
    }:
        raise OptimizationPlanValidationError("unknown optimization plan format")
    utility = value.get("utility")
    precedence = value.get("precedence")
    screening = value.get("screening")
    budget = value.get("budget")
    records = value.get("screened_candidates")
    for label, section, fields in (
        ("utility", utility, {"utility_contract", "loss_contract"}),
        ("precedence", precedence, {"policy"}),
        ("screening", screening, {"policy"}),
        ("budget", budget, None),
    ):
        if not isinstance(section, dict) or (fields is not None and set(section) != fields):
            raise OptimizationPlanValidationError(f"optimization {label} section is invalid")
    expected_budget_fields = {"contract", "candidate_limit", "wall_clock_seconds"}
    if format_version == OPTIMIZATION_PLAN_V2_FORMAT_VERSION:
        expected_budget_fields.add("max_statistics_count")
    if set(budget) != expected_budget_fields:
        raise OptimizationPlanValidationError("optimization budget section is invalid")
    if budget["contract"] != OPTIMIZATION_BUDGET_CONTRACT:
        raise OptimizationPlanValidationError("unsupported optimization budget contract")
    for label in (
        "screened_candidate_ids",
        "excluded_actionable_candidate_ids",
        "absent_native_candidate_ids",
    ):
        if not isinstance(value.get(label), list):
            raise OptimizationPlanValidationError(f"optimization {label} must be a list")
    if not isinstance(records, list):
        raise OptimizationPlanValidationError("optimization screened_candidates must be a list")
    screened_candidates = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "candidate_id",
            "frozen_precedence_rank",
            "search_space_position",
            "singleton_objective",
            "singleton_improvement",
        }:
            raise OptimizationPlanValidationError("optimization screened candidate is malformed")
        try:
            screened_candidates.append(
                ScreenedCandidate(
                    record["candidate_id"],
                    record["frozen_precedence_rank"],
                    record["search_space_position"],
                    record["singleton_objective"],
                    record["singleton_improvement"],
                )
            )
        except (KeyError, TypeError, OptimizationPlanningError) as exc:
            raise OptimizationPlanValidationError(
                "optimization screened candidate is malformed"
            ) from exc
    try:
        plan = OptimizationPlan(
            value["source_snapshot_semantic_digest"],
            value["candidate_universe_semantic_digest"],
            value["native_stats_repository_semantic_digest"],
            value["ground_truth_semantic_digest"],
            value["singleton_profile_semantic_digest"],
            utility["utility_contract"],
            utility["loss_contract"],
            precedence["policy"],
            screening["policy"],
            OptimizationBudget(
                budget["candidate_limit"],
                budget["wall_clock_seconds"],
                budget.get("max_statistics_count"),
            ),
            value["actionable_candidate_count"],
            tuple(value["screened_candidate_ids"]),
            tuple(screened_candidates),
            tuple(value["excluded_actionable_candidate_ids"]),
            tuple(value["absent_native_candidate_ids"]),
            value.get("runtime_metadata", {}),
            value.get("created_at"),
            value.get("semantic_digest"),
        )
        stored_screened_count = value["screened_candidate_count"]
    except (KeyError, TypeError, OptimizationPlanningError) as exc:
        raise OptimizationPlanValidationError("optimization plan is incomplete or invalid") from exc
    if stored_screened_count != plan.screened_candidate_count:
        raise OptimizationPlanValidationError("screened candidate count is inconsistent")
    if plan.semantic_digest != plan.computed_semantic_digest:
        raise OptimizationPlanValidationError("optimization plan semantic digest mismatch")
    return plan


def _ground_truth_digest(ground_truth: Any) -> str:
    digest = getattr(ground_truth, "semantic_digest", None)
    return digest if isinstance(digest, str) else ground_truth.computed_semantic_digest


def _validate_bindings(
    plan: OptimizationPlan,
    snapshot: Any | None,
    candidate_universe: Any | None,
    native_repository: Any | None,
    ground_truth: Any | None,
    singleton_profile: SingletonProfile | None,
) -> None:
    sources = (
        ("snapshot", snapshot, plan.source_snapshot_semantic_digest, "semantic_digest"),
        (
            "candidate universe",
            candidate_universe,
            plan.candidate_universe_semantic_digest,
            "semantic_digest",
        ),
        (
            "native repository",
            native_repository,
            plan.native_stats_repository_semantic_digest,
            "semantic_digest",
        ),
    )
    for label, source, expected, attribute in sources:
        if source is not None and expected != getattr(source, attribute):
            raise OptimizationPlanValidationError(f"optimization {label} digest mismatch")
    if ground_truth is not None and plan.ground_truth_semantic_digest != _ground_truth_digest(
        ground_truth
    ):
        raise OptimizationPlanValidationError("optimization ground truth digest mismatch")
    if singleton_profile is not None:
        if plan.singleton_profile_semantic_digest != singleton_profile.computed_semantic_digest:
            raise OptimizationPlanValidationError("optimization singleton profile digest mismatch")
        for label, plan_value, profile_value in (
            ("utility contract", plan.utility_contract, singleton_profile.utility_contract),
            ("loss contract", plan.loss_contract, singleton_profile.loss_contract),
            ("precedence policy", plan.precedence_policy, singleton_profile.precedence_policy),
            (
                "snapshot digest",
                plan.source_snapshot_semantic_digest,
                singleton_profile.source_snapshot_semantic_digest,
            ),
            (
                "candidate universe digest",
                plan.candidate_universe_semantic_digest,
                singleton_profile.candidate_universe_semantic_digest,
            ),
            (
                "native repository digest",
                plan.native_stats_repository_semantic_digest,
                singleton_profile.native_stats_repository_semantic_digest,
            ),
            (
                "ground truth digest",
                plan.ground_truth_semantic_digest,
                singleton_profile.ground_truth_semantic_digest,
            ),
        ):
            if plan_value != profile_value:
                raise OptimizationPlanValidationError(f"optimization {label} mismatch")
        profile_by_id = {
            record.candidate_id: record for record in singleton_profile.candidate_profiles
        }
        profile_actionable = tuple(singleton_profile.frozen_ordered_candidate_ids)
        if (
            plan.screened_candidate_ids + plan.excluded_actionable_candidate_ids
            != profile_actionable
        ):
            raise OptimizationPlanValidationError(
                "optimization IDs are not the frozen prefix/suffix"
            )
        expected_absent = tuple(
            record.candidate_id
            for record in singleton_profile.candidate_profiles
            if record.native_state == ABSENT_NATIVE
        )
        if set(plan.absent_native_candidate_ids) != set(expected_absent):
            raise OptimizationPlanValidationError("optimization ABSENT_NATIVE IDs mismatch")
        if candidate_universe is not None:
            universe_by_id = {
                candidate.candidate_id: candidate for candidate in candidate_universe.candidates
            }
            if set(universe_by_id) != set(profile_by_id):
                raise OptimizationPlanValidationError(
                    "optimization candidate universe does not match singleton profile"
                )
            for candidate_id, profile in profile_by_id.items():
                if (
                    profile.static_precedence_rank
                    != universe_by_id[candidate_id].static_precedence_rank
                ):
                    raise OptimizationPlanValidationError(
                        f"optimization static precedence mismatch: {candidate_id}"
                    )
        if native_repository is not None:
            native_by_id = {
                candidate.candidate_id: candidate
                for candidate in native_repository.candidate_models
            }
            if set(native_by_id) != set(profile_by_id):
                raise OptimizationPlanValidationError(
                    "optimization native repository does not match singleton profile"
                )
            for candidate_id, native in native_by_id.items():
                if profile_by_id[candidate_id].native_state != native.state:
                    raise OptimizationPlanValidationError(
                        f"optimization native state mismatch: {candidate_id}"
                    )
        for screened in plan.screened_candidates:
            profile = profile_by_id.get(screened.candidate_id)
            if profile is None or profile.native_state != PRESENT:
                raise OptimizationPlanValidationError(
                    "optimization screened candidate is not PRESENT"
                )
            if (
                profile.frozen_precedence_rank != screened.frozen_precedence_rank
                or profile.singleton_objective != screened.singleton_objective
                or profile.improvement != screened.singleton_improvement
            ):
                raise OptimizationPlanValidationError(
                    f"optimization screened candidate metadata mismatch: {screened.candidate_id}"
                )
        if plan.actionable_candidate_count != singleton_profile.present_count:
            raise OptimizationPlanValidationError("optimization actionable count mismatch")
        if plan.candidate_universe_count != len(singleton_profile.candidate_profiles):
            raise OptimizationPlanValidationError("optimization candidate universe count mismatch")
    if snapshot is not None and candidate_universe is not None and native_repository is not None:
        try:
            validate_native_stats_repository_compatibility(
                native_repository, snapshot, candidate_universe
            )
        except (NativeStatsRepositoryValidationError, CandidateUniverseValidationError) as exc:
            raise OptimizationPlanValidationError(
                "optimization source artifacts are incompatible"
            ) from exc


def load_optimization_plan(path: Path) -> OptimizationPlan:
    return _from_manifest(_read(Path(path)))


def validate_optimization_plan(
    path: Path,
    snapshot: Any | None = None,
    candidate_universe: Any | None = None,
    native_repository: Any | None = None,
    ground_truth: Any | None = None,
    singleton_profile: SingletonProfile | None = None,
) -> dict[str, Any]:
    plan = load_optimization_plan(path)
    _validate_bindings(
        plan, snapshot, candidate_universe, native_repository, ground_truth, singleton_profile
    )
    return optimization_plan_summary(plan)


def write_optimization_plan(plan: OptimizationPlan, destination: Path) -> str:
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"optimization plan destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(canonical_json(plan.to_manifest()) + b"\n")
        load_optimization_plan(temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return plan.computed_semantic_digest


def optimization_plan_summary(plan: OptimizationPlan) -> dict[str, Any]:
    best = min(
        plan.screened_candidates,
        key=lambda record: (record.singleton_objective, record.candidate_id),
        default=None,
    )
    worst = max(
        plan.screened_candidates,
        key=lambda record: (record.singleton_objective, record.candidate_id),
        default=None,
    )
    return {
        "format_version": (
            OPTIMIZATION_PLAN_FORMAT_VERSION
            if plan.budget.max_statistics_count is None
            else OPTIMIZATION_PLAN_V2_FORMAT_VERSION
        ),
        "semantic_digest": plan.computed_semantic_digest,
        "budget_contract": OPTIMIZATION_BUDGET_CONTRACT,
        "candidate_limit": plan.budget.candidate_limit,
        "max_statistics_count": plan.max_statistics_count,
        "wall_clock_seconds": plan.budget.wall_clock_seconds,
        "screening_policy": plan.screening_policy,
        "precedence_policy": plan.precedence_policy,
        "candidate_universe_count": plan.candidate_universe_count,
        "actionable_candidate_count": plan.actionable_candidate_count,
        "screened_candidate_count": plan.screened_candidate_count,
        "excluded_actionable_candidate_count": plan.excluded_actionable_candidate_count,
        "absent_native_count": plan.absent_native_candidate_count,
        "screened_candidate_ids": list(plan.screened_candidate_ids),
        "excluded_actionable_candidate_ids": list(plan.excluded_actionable_candidate_ids),
        "absent_native_candidate_ids": list(plan.absent_native_candidate_ids),
        "positive_singleton_count": plan.positive_singleton_count,
        "neutral_singleton_count": plan.neutral_singleton_count,
        "negative_singleton_count": plan.negative_singleton_count,
        "best_screened_singleton_id": None if best is None else best.candidate_id,
        "worst_screened_singleton_id": None if worst is None else worst.candidate_id,
        "worst_case_add_configuration_evaluations_after_singletons": plan.worst_case_add_configuration_evaluations_after_singletons,
        "utility_contract": plan.utility_contract,
        "loss_contract": plan.loss_contract,
    }


def inspect_optimization_plan(path: Path) -> dict[str, Any]:
    return optimization_plan_summary(load_optimization_plan(path))
