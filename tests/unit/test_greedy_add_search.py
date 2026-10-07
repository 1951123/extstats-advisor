from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from extstats_advisor.errors import SearchBudgetExpired, SearchResultValidationError
from extstats_advisor.native_stats.model import PRESENT
from extstats_advisor.optimization.budget import OptimizationBudget
from extstats_advisor.optimization.plan import create_optimization_plan
from extstats_advisor.optimization.search import (
    GREEDY_ADD_SEARCH_POLICY,
    PlannerIdentity,
    SearchDeadline,
    greedy_add_search,
)
from extstats_advisor.optimization.search_artifact import (
    load_search_result,
    validate_search_result,
    write_search_result,
)
from extstats_advisor.optimization.singleton import (
    SINGLETON_PRECEDENCE_POLICY,
    BaselineProfile,
    CandidateSingletonProfile,
    SingletonProfile,
)
from extstats_advisor.snapshot.model import Workload, WorkloadQuery
from extstats_advisor.utility.model import PerQueryUtility, UtilityResult


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _Utility:
    utility_contract = "weighted-workload-mean-v1"
    loss_contract = "qerror-cardinality-floor-1-v1"

    def evaluate(self, estimates):
        objective = float(next(iter(estimates.values())))
        return UtilityResult(
            objective,
            self.loss_contract,
            1,
            1.0,
            (PerQueryUtility("q1", 1.0, objective, 1, objective),),
        )


def _profile(objectives: tuple[float, float, float] = (7.0, 8.0, 12.0)) -> SingletonProfile:
    candidate_ids = ("A", "B", "C")
    candidates = tuple(
        CandidateSingletonProfile(
            candidate_id,
            PRESENT,
            index,
            objective,
            10.0 - objective,
            index,
        )
        for index, (candidate_id, objective) in enumerate(zip(candidate_ids, objectives), start=1)
    )
    # Candidate records are intentionally in a different order from frozen order.
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
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
        tuple(by_id[candidate_id] for candidate_id in candidate_ids),
        tuple(
            sorted(
                candidate_ids, key=lambda candidate_id: by_id[candidate_id].frozen_precedence_rank
            )
        ),
    )


def _identity() -> PlannerIdentity:
    return PlannerIdentity("sandbox-v1", "backend-v1", "16.14", 160014, "e" * 64)


def _utility_result(objective: float) -> UtilityResult:
    return UtilityResult(
        objective,
        "qerror-cardinality-floor-1-v1",
        1,
        1.0,
        (PerQueryUtility("q1", 1.0, objective, 1, objective),),
    )


def test_greedy_add_reuses_singletons_and_accepts_interaction_gain() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=None)
    calls: list[tuple[str, ...]] = []
    objectives = {
        ("A", "B"): 6.0,
        ("A", "C"): 4.0,
        ("A", "B", "C"): 3.0,
    }

    def evaluate(membership, _deadline):
        ordered = plan.ordered_configuration(membership)
        calls.append(ordered)
        return _utility_result(objectives[ordered])

    result = greedy_add_search(profile, plan, _Utility(), evaluate, _identity())

    assert result.search_policy == GREEDY_ADD_SEARCH_POLICY
    assert calls == [("A", "B"), ("A", "C"), ("A", "B", "C")]
    assert [move.added_candidate_id for move in result.accepted_moves] == ["A", "C", "B"]
    assert result.final_ordered_candidate_ids == ("A", "B", "C")
    assert result.final_objective == 3.0
    assert result.termination_reason == "all-screened-candidates-selected"
    assert result.runtime_metadata["cached_singleton_configuration_count"] == 3
    assert result.runtime_metadata["live_configuration_evaluation_count"] == 3


def test_negative_singleton_can_be_accepted_by_later_interaction() -> None:
    profile = _profile((7.0, 8.0, 12.0))
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=None)

    def evaluate(membership, _deadline):
        return _utility_result(
            {("A", "B"): 6.0, ("A", "C"): 4.0, ("A", "B", "C"): 4.0}[
                plan.ordered_configuration(membership)
            ]
        )

    result = greedy_add_search(profile, plan, _Utility(), evaluate, _identity())
    assert [move.added_candidate_id for move in result.accepted_moves] == ["A", "C"]
    assert result.final_ordered_candidate_ids == ("A", "C")
    assert result.termination_reason == "local-optimum"


def test_tie_uses_earlier_screened_position_and_no_singleton_live_calls() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=None)
    calls = []

    def evaluate(membership, _deadline):
        ordered = plan.ordered_configuration(membership)
        calls.append(ordered)
        return _utility_result(7.0)

    result = greedy_add_search(profile, plan, _Utility(), evaluate, _identity())
    assert result.accepted_moves[0].added_candidate_id == "A"
    assert result.termination_reason == "local-optimum"
    assert result.runtime_metadata["live_configuration_evaluation_count"] == 2
    assert calls == [("A", "B"), ("A", "C")]


def test_no_improving_singleton_terminates_without_live_evaluation() -> None:
    profile = _profile((10.0, 10.0, 12.0))
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=None)

    def fail_evaluate(_membership, _deadline):
        raise AssertionError("singleton round must use the profile cache")

    result = greedy_add_search(profile, plan, _Utility(), fail_evaluate, _identity())
    assert result.final_ordered_candidate_ids == ()
    assert result.final_objective == 10.0
    assert result.termination_reason == "local-optimum"
    assert result.runtime_metadata["live_configuration_evaluation_count"] == 0


def test_budget_expiry_discards_incomplete_round() -> None:
    profile = _profile()
    plan = create_optimization_plan(
        profile, candidate_limit=3, max_statistics_count=None, wall_clock_seconds=1.0
    )
    clock = _Clock()
    calls = []

    def evaluate(membership, _deadline):
        ordered = plan.ordered_configuration(membership)
        calls.append(ordered)
        clock.value += 0.6
        return _utility_result(6.0)

    result = greedy_add_search(profile, plan, _Utility(), evaluate, _identity(), clock=clock)
    assert calls == [("A", "B"), ("A", "C")]
    assert result.final_ordered_candidate_ids == ("A",)
    assert result.final_objective == 7.0
    assert result.completed_rounds == ()
    assert result.accepted_moves[0].added_candidate_id == "A"
    assert result.termination_reason == "budget-expired-incomplete-round"
    assert result.runtime_metadata["partial_final_round_evaluation_count"] == 2


def test_budget_expiry_on_first_live_evaluation_is_incomplete_round(tmp_path) -> None:
    profile = _profile()
    plan = create_optimization_plan(
        profile, candidate_limit=3, max_statistics_count=None, wall_clock_seconds=1.0
    )
    calls = []

    def evaluate(membership, _deadline):
        calls.append(plan.ordered_configuration(membership))
        raise SearchBudgetExpired("test deadline")

    result = greedy_add_search(profile, plan, _Utility(), evaluate, _identity())
    assert calls == [("A", "B")]
    assert result.termination_reason == "budget-expired-incomplete-round"
    assert result.runtime_metadata["partial_final_round_evaluation_count"] == 1
    assert result.completed_rounds == ()
    assert result.final_ordered_candidate_ids == ("A",)

    path = tmp_path / "first-live-expiry.json"
    write_search_result(result, path)
    validate_search_result(path, singleton_profile=profile, optimization_plan=plan)


def test_budget_expiry_before_round_keeps_last_accepted_state(tmp_path) -> None:
    values = iter((0.0, 2.0, 2.0))
    profile = _profile()
    plan = create_optimization_plan(
        profile, candidate_limit=3, max_statistics_count=None, wall_clock_seconds=1.0
    )
    calls = []

    def clock():
        return next(values)

    def fail_evaluate(membership, _deadline):
        calls.append(membership)
        pytest.fail("no live round should start")

    result = greedy_add_search(
        profile,
        plan,
        _Utility(),
        fail_evaluate,
        _identity(),
        clock=clock,
    )
    assert result.final_ordered_candidate_ids == ()
    assert result.termination_reason == "budget-expired-before-round"
    assert result.runtime_metadata["partial_final_round_evaluation_count"] == 0
    assert calls == []

    path = tmp_path / "before-round-expiry.json"
    write_search_result(result, path)
    validate_search_result(path, singleton_profile=profile, optimization_plan=plan)


def test_budget_expiry_after_all_evaluations_before_commit_discards_round(tmp_path) -> None:
    values = iter((0.0,) * 8 + (2.0, 2.0))
    profile = _profile()
    plan = create_optimization_plan(
        profile, candidate_limit=3, max_statistics_count=None, wall_clock_seconds=1.0
    )
    calls = []

    def clock():
        return next(values)

    def evaluate(membership, _deadline):
        ordered = plan.ordered_configuration(membership)
        calls.append(ordered)
        return _utility_result({("A", "B"): 6.0, ("A", "C"): 4.0}[ordered])

    result = greedy_add_search(profile, plan, _Utility(), evaluate, _identity(), clock=clock)
    assert calls == [("A", "B"), ("A", "C")]
    assert result.termination_reason == "budget-expired-incomplete-round"
    assert result.runtime_metadata["partial_final_round_evaluation_count"] == 2
    assert result.completed_rounds == ()
    assert result.final_ordered_candidate_ids == ("A",)
    assert [move.added_candidate_id for move in result.accepted_moves] == ["A"]

    path = tmp_path / "pre-commit-expiry.json"
    write_search_result(result, path)
    validate_search_result(path, singleton_profile=profile, optimization_plan=plan)


def test_budget_expiry_during_cached_first_round_does_not_accept_singleton() -> None:
    values = iter((0.0, 0.0, 0.0, 2.0, 2.0))
    profile = _profile()
    plan = create_optimization_plan(
        profile, candidate_limit=3, max_statistics_count=None, wall_clock_seconds=1.0
    )

    def clock():
        return next(values)

    result = greedy_add_search(
        profile,
        plan,
        _Utility(),
        lambda _membership, _deadline: pytest.fail("cached expiry must precede live search"),
        _identity(),
        clock=clock,
    )
    assert result.termination_reason == "budget-expired-before-round"
    assert result.runtime_metadata["partial_final_round_evaluation_count"] == 0
    assert result.accepted_moves == ()
    assert result.final_ordered_candidate_ids == ()
    assert result.final_objective == profile.baseline.objective


def test_search_result_artifact_is_deterministic_and_validated(tmp_path) -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=None)

    def evaluate(membership, _deadline):
        return _utility_result(
            {("A", "B"): 6.0, ("A", "C"): 4.0, ("A", "B", "C"): 3.0}[
                plan.ordered_configuration(membership)
            ]
        )

    first = greedy_add_search(profile, plan, _Utility(), evaluate, _identity())
    second = greedy_add_search(profile, plan, _Utility(), evaluate, _identity())
    assert first.computed_semantic_digest == second.computed_semantic_digest
    first_path = tmp_path / "search-result-v1.json"
    second_path = tmp_path / "search-result-v1-second.json"
    assert write_search_result(first, first_path) == write_search_result(second, second_path)
    assert validate_search_result(first_path, singleton_profile=profile, optimization_plan=plan)[
        "final_ordered_candidate_ids"
    ] == ["A", "B", "C"]
    value = json.loads(first_path.read_text())
    value["final_objective"] = 99.0
    first_path.write_text(json.dumps(value))
    with pytest.raises(SearchResultValidationError):
        load_search_result(first_path)


class _QueryCanceled(Exception):
    sqlstate = "57014"


def test_postgres_timeout_cancellation_maps_to_budget_expiry() -> None:
    from extstats_advisor.dbms.postgres.search import PostgresSearchEvaluator

    snapshot = SimpleNamespace(workload=Workload("search", (WorkloadQuery("q1", "SELECT 1"),)))
    universe = SimpleNamespace(
        query_profiles=(SimpleNamespace(query_id="q1", analysis_status="supported"),)
    )
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=None)
    session = SimpleNamespace(
        timeouts=[],
        set_statement_timeout_ms=lambda timeout: session.timeouts.append(timeout),
        activate=lambda _configuration: (_ for _ in ()).throw(_QueryCanceled()),
    )
    clock_values = iter((0.0, 0.0, 0.0, 2.0))
    deadline = SearchDeadline(OptimizationBudget(3, 1.0), clock_values.__next__)
    evaluator = PostgresSearchEvaluator(session, snapshot, universe, plan, _Utility())
    with pytest.raises(SearchBudgetExpired):
        evaluator(frozenset({"A", "B"}), deadline)
    assert session.timeouts == [1000]
