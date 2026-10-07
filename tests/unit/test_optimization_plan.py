from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from extstats_advisor.errors import OptimizationPlanningError, OptimizationPlanValidationError
from extstats_advisor.native_stats.model import ABSENT_NATIVE, PRESENT
from extstats_advisor.optimization.budget import (
    DEFAULT_WALL_CLOCK_SECONDS,
    OPTIMIZATION_BUDGET_CONTRACT,
    OptimizationBudget,
)
from extstats_advisor.optimization.plan import (
    OPTIMIZATION_PLAN_FORMAT_VERSION,
    SCREENING_POLICY,
    create_optimization_plan,
)
from extstats_advisor.optimization.plan_artifact import (
    inspect_optimization_plan,
    load_optimization_plan,
    validate_optimization_plan,
    write_optimization_plan,
)
from extstats_advisor.optimization.singleton import (
    SINGLETON_PRECEDENCE_POLICY,
    BaselineProfile,
    CandidateSingletonProfile,
    SingletonProfile,
)


def _profile(present_count: int = 3) -> SingletonProfile:
    values = (
        ("candidate_positive", 2, 7.0, 3.0),
        ("candidate_neutral", 1, 10.0, 0.0),
        ("candidate_negative", 3, 12.0, -2.0),
        ("candidate_extra", 4, 9.0, 1.0),
        ("candidate_extra_2", 5, 8.0, 2.0),
    )[:present_count]
    ordered = sorted(values, key=lambda item: (-item[3], item[1], item[0]))
    ranks = {candidate_id: rank for rank, (candidate_id, *_rest) in enumerate(ordered, 1)}
    profiles = tuple(
        CandidateSingletonProfile(
            candidate_id,
            PRESENT,
            static_rank,
            objective,
            improvement,
            ranks[candidate_id],
        )
        for candidate_id, static_rank, objective, improvement in values
    )
    profiles += (
        CandidateSingletonProfile(
            "candidate_absent", ABSENT_NATIVE, present_count + 1, 10.0, 0.0, None
        ),
    )
    return SingletonProfile(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "sandbox-v1",
        "backend-v1",
        "16.14",
        160014,
        "e" * 64,
        "weighted-workload-mean-v1",
        "qerror-cardinality-floor-1-v1",
        SINGLETON_PRECEDENCE_POLICY,
        BaselineProfile(10.0),
        profiles,
        tuple(candidate_id for candidate_id, *_rest in ordered),
    )


def test_budget_contract_and_screening_prefix_retain_negative_singletons() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3)

    assert plan.budget == OptimizationBudget(3, max_statistics_count=3)
    assert plan.budget.wall_clock_seconds == DEFAULT_WALL_CLOCK_SECONDS
    assert plan.screened_candidate_ids == (
        "candidate_positive",
        "candidate_neutral",
        "candidate_negative",
    )
    assert plan.excluded_actionable_candidate_ids == ()
    assert plan.absent_native_candidate_ids == ("candidate_absent",)
    assert plan.positive_singleton_count == 1
    assert plan.neutral_singleton_count == 1
    assert plan.negative_singleton_count == 1
    assert plan.worst_case_add_configuration_evaluations_after_singletons == 3


def test_explicit_none_retains_v1_reference_plan() -> None:
    plan = create_optimization_plan(_profile(), candidate_limit=3, max_statistics_count=None)
    assert plan.budget == OptimizationBudget(3)
    assert plan.semantic_manifest()["format_version"] == OPTIMIZATION_PLAN_FORMAT_VERSION


@pytest.mark.parametrize(
    ("candidate_limit", "expected_screened", "expected_excluded"),
    [
        (1, ("candidate_positive",), ("candidate_neutral", "candidate_negative")),
        (
            2,
            ("candidate_positive", "candidate_neutral"),
            ("candidate_negative",),
        ),
        (3, ("candidate_positive", "candidate_neutral", "candidate_negative"), ()),
        (99, ("candidate_positive", "candidate_neutral", "candidate_negative"), ()),
    ],
)
def test_candidate_limit_is_exact_frozen_prefix(
    candidate_limit: int,
    expected_screened: tuple[str, ...],
    expected_excluded: tuple[str, ...],
) -> None:
    plan = create_optimization_plan(_profile(), candidate_limit=candidate_limit)
    assert plan.screened_candidate_ids == expected_screened
    assert plan.excluded_actionable_candidate_ids == expected_excluded
    assert "candidate_absent" not in plan.screened_candidate_ids
    assert "candidate_absent" not in plan.excluded_actionable_candidate_ids


def test_ordered_configuration_is_screened_frozen_subsequence() -> None:
    plan = create_optimization_plan(_profile(), candidate_limit=3)
    assert plan.ordered_configuration(("candidate_negative", "candidate_positive")) == (
        "candidate_positive",
        "candidate_negative",
    )
    assert plan.ordered_configuration(set()) == ()
    with pytest.raises(OptimizationPlanningError, match="outside screened"):
        plan.ordered_configuration(("candidate_absent",))
    with pytest.raises(OptimizationPlanningError, match="duplicates"):
        plan.ordered_configuration(("candidate_positive", "candidate_positive"))


@pytest.mark.parametrize("candidate_limit", [True, 0, -1])
def test_invalid_candidate_limit_rejected(candidate_limit) -> None:
    with pytest.raises(OptimizationPlanningError):
        create_optimization_plan(_profile(), candidate_limit=candidate_limit)


@pytest.mark.parametrize("wall_clock_seconds", [True, 0, -1, math.nan, math.inf, -math.inf])
def test_invalid_wall_clock_budget_rejected(wall_clock_seconds) -> None:
    with pytest.raises(OptimizationPlanningError):
        create_optimization_plan(
            _profile(), candidate_limit=1, wall_clock_seconds=wall_clock_seconds
        )


def test_worst_case_evaluation_formula() -> None:
    assert (
        create_optimization_plan(
            _profile(0), candidate_limit=1
        ).worst_case_add_configuration_evaluations_after_singletons
        == 0
    )
    profile = _profile(5)
    for screened_count in (1, 2, 3, 5):
        plan = create_optimization_plan(profile, candidate_limit=screened_count)
        assert plan.worst_case_add_configuration_evaluations_after_singletons == (
            screened_count * (screened_count - 1) // 2
        )


def test_plan_artifact_is_deterministic_bound_and_rejects_tampering(tmp_path) -> None:
    profile = _profile()
    plan = create_optimization_plan(
        profile, candidate_limit=2, max_statistics_count=None, wall_clock_seconds=42.5
    )
    runtime_variant = replace(plan, runtime_metadata={"elapsed_seconds": 9.0}, created_at="later")
    first_path = tmp_path / "optimization-plan-v1.json"
    second_path = tmp_path / "optimization-plan-v1-second.json"
    first_digest = write_optimization_plan(plan, first_path)
    second_digest = write_optimization_plan(plan, second_path)

    assert first_digest == second_digest == plan.computed_semantic_digest
    assert runtime_variant.computed_semantic_digest == plan.computed_semantic_digest
    assert load_optimization_plan(first_path).computed_semantic_digest == first_digest
    assert (
        validate_optimization_plan(first_path, singleton_profile=profile)[
            "screened_candidate_count"
        ]
        == 2
    )
    summary = inspect_optimization_plan(first_path)
    assert summary["format_version"] == OPTIMIZATION_PLAN_FORMAT_VERSION
    assert summary["budget_contract"] == OPTIMIZATION_BUDGET_CONTRACT
    assert summary["screening_policy"] == SCREENING_POLICY
    assert summary["wall_clock_seconds"] == 42.5
    with pytest.raises(FileExistsError):
        write_optimization_plan(plan, first_path)

    value = json.loads(first_path.read_text())
    value["screened_candidate_ids"][0] = "tampered"
    first_path.write_text(json.dumps(value))
    with pytest.raises(OptimizationPlanValidationError):
        load_optimization_plan(first_path)


def test_plan_validation_rejects_profile_binding_mismatch(tmp_path) -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=1)
    path = tmp_path / "optimization-plan-v1.json"
    write_optimization_plan(plan, path)
    changed_profile = _profile(4)
    with pytest.raises(OptimizationPlanValidationError, match="profile digest"):
        validate_optimization_plan(path, singleton_profile=changed_profile)


def test_empty_actionable_space_is_valid() -> None:
    profile = _profile(0)
    plan = create_optimization_plan(profile, candidate_limit=1)
    assert plan.actionable_candidate_count == 0
    assert plan.screened_candidate_ids == ()
    assert plan.excluded_actionable_candidate_ids == ()
    assert plan.absent_native_candidate_count == 1
    assert plan.worst_case_add_configuration_evaluations_after_singletons == 0
