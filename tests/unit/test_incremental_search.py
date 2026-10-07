from __future__ import annotations

from types import SimpleNamespace

from extstats_advisor.dbms.postgres.search import IncrementalPostgresSearchEvaluator
from extstats_advisor.errors import SearchBudgetExpired
from extstats_advisor.optimization.budget import OptimizationBudget
from extstats_advisor.optimization.plan import create_optimization_plan
from extstats_advisor.optimization.plan_artifact import (
    inspect_optimization_plan,
    load_optimization_plan,
    write_optimization_plan,
)
from extstats_advisor.optimization.search import (
    INCREMENTAL_GREEDY_ADD_SEARCH_POLICY,
    TERMINATION_MAX_STATISTICS_COUNT,
    PlannerIdentity,
    SearchDeadline,
    greedy_add_search,
    greedy_add_search_incremental,
)
from extstats_advisor.optimization.search_artifact import (
    load_search_result,
    validate_search_result,
    write_search_result,
)
from extstats_advisor.snapshot.model import Workload, WorkloadQuery
from extstats_advisor.utility.model import PerQueryUtility, UtilityResult
from tests.unit.test_greedy_add_search import _profile, _Utility, _utility_result


def _identity() -> PlannerIdentity:
    return PlannerIdentity("sandbox-v1", "backend-v1", "16.14", 160014, "e" * 64)


def test_incremental_search_respects_b_one_and_emits_v2(tmp_path) -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=1)

    result = greedy_add_search_incremental(
        profile,
        plan,
        _Utility(),
        lambda _membership, _deadline: _utility_result(6.0),
        _identity(),
    )

    assert result.search_policy == INCREMENTAL_GREEDY_ADD_SEARCH_POLICY
    assert result.final_ordered_candidate_ids == ("A",)
    assert result.termination_reason == TERMINATION_MAX_STATISTICS_COUNT
    assert result.runtime_metadata["max_statistics_count"] == 1
    path = tmp_path / "search-result-v2.json"
    write_search_result(result, path)
    loaded = load_search_result(path)
    assert loaded.format_version == "optimization-search-result-v2"
    assert (
        validate_search_result(path, singleton_profile=profile, optimization_plan=plan)[
            "max_statistics_count"
        ]
        == 1
    )


def test_incremental_search_stops_at_b_before_starting_another_round() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=2)
    calls = []
    objectives = {("A", "B"): 6.0, ("A", "C"): 4.0}

    def evaluate(membership, _deadline):
        ordered = plan.ordered_configuration(membership)
        calls.append(ordered)
        return _utility_result(objectives[ordered])

    result = greedy_add_search_incremental(profile, plan, _Utility(), evaluate, _identity())
    assert calls == [("A", "B"), ("A", "C")]
    assert result.final_ordered_candidate_ids == ("A", "C")
    assert result.termination_reason == TERMINATION_MAX_STATISTICS_COUNT
    assert result.runtime_metadata["completed_round_count"] == 1


def test_v2_plan_records_b_and_round_trips_without_changing_v1(tmp_path) -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=2)
    assert plan.max_statistics_count == 2
    path = tmp_path / "optimization-plan-v2.json"
    write_optimization_plan(plan, path)
    assert inspect_optimization_plan(path)["format_version"] == "optimization-plan-v2"
    loaded = load_optimization_plan(path)
    assert loaded.max_statistics_count == 2


def test_incremental_search_local_optimum_does_not_fill_b() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=3)

    result = greedy_add_search_incremental(
        profile,
        plan,
        _Utility(),
        lambda _membership, _deadline: _utility_result(7.0),
        _identity(),
    )
    assert result.final_ordered_candidate_ids == ("A",)
    assert result.termination_reason == "local-optimum"


def test_incremental_search_and_reference_match_completed_semantics() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=3)
    objectives = {
        ("A", "B"): 6.0,
        ("A", "C"): 4.0,
        ("A", "B", "C"): 3.0,
    }

    def evaluate(membership, _deadline):
        ordered = plan.ordered_configuration(membership)
        return _utility_result(objectives[ordered])

    incremental = greedy_add_search_incremental(profile, plan, _Utility(), evaluate, _identity())
    reference = greedy_add_search(profile, plan, _Utility(), evaluate, _identity())
    assert incremental.accepted_moves == reference.accepted_moves
    assert incremental.completed_rounds == reference.completed_rounds
    assert incremental.final_ordered_candidate_ids == reference.final_ordered_candidate_ids
    assert incremental.final_objective == reference.final_objective
    assert incremental.termination_reason == reference.termination_reason


def test_bounded_full_workload_reference_honors_v2_budget() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=1)
    reference = greedy_add_search(
        profile,
        plan,
        _Utility(),
        lambda _membership, _deadline: _utility_result(6.0),
        _identity(),
    )
    assert reference.format_version == "optimization-search-result-v2"
    assert reference.final_ordered_candidate_ids == ("A",)
    assert reference.termination_reason == TERMINATION_MAX_STATISTICS_COUNT


def test_incremental_postgres_evaluator_only_replans_incident_queries() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=3)
    workload = Workload(
        "search",
        (
            WorkloadQuery("q1", "SELECT 1"),
            WorkloadQuery("q2", "SELECT 2"),
            WorkloadQuery("q3", "SELECT 3"),
        ),
    )
    snapshot = SimpleNamespace(workload=workload)
    universe = SimpleNamespace(
        query_profiles=tuple(
            SimpleNamespace(query_id=query_id, analysis_status="supported")
            for query_id in ("q1", "q2", "q3")
        ),
        query_ids_for_candidate=lambda candidate_id: {
            "A": ("q1",),
            "B": ("q2",),
            "C": (),
        }[candidate_id],
    )

    class Session:
        def __init__(self):
            self.active = ()
            self.calls = []

        def set_statement_timeout_ms(self, _timeout):
            return None

        def activate(self, configuration):
            self.active = configuration.ordered_candidate_ids

        def estimate_query(self, query_id):
            self.calls.append((self.active, query_id))
            base = {"q1": 10, "q2": 20, "q3": 30}[query_id]
            if "A" in self.active and query_id == "q1":
                base = 7
            if "B" in self.active and query_id == "q2":
                base = 56
            return SimpleNamespace(estimated_rows=base)

    class Utility:
        utility_contract = "weighted-workload-mean-v1"
        loss_contract = "qerror-cardinality-floor-1-v1"

        def evaluate(self, estimates):
            objective = float(sum(estimates.values()) - 50)
            return UtilityResult(
                objective,
                self.loss_contract,
                len(estimates),
                float(len(estimates)),
                tuple(
                    PerQueryUtility(query_id, 1.0, value, value, 1.0)
                    for query_id, value in sorted(estimates.items())
                ),
            )

    session = Session()
    evaluator = IncrementalPostgresSearchEvaluator(session, snapshot, universe, plan, Utility())
    deadline = SearchDeadline(OptimizationBudget(3, 30.0, 3))
    evaluator.prepare_initial_configuration(frozenset({"A"}), deadline)
    assert evaluator.baseline_materialization_planner_calls == 3
    assert evaluator.winner_materialization_planner_calls == 1
    before = len(session.calls)
    result = evaluator(frozenset({"A", "B"}), deadline)
    assert result.objective == 43.0
    assert len(session.calls) - before == 1
    assert session.calls[-1] == (("A", "B"), "q2")
    evaluator(frozenset({"A", "C"}), deadline)
    assert evaluator._proposal_cache[frozenset({"A", "B"})][1] == {"q2": 56}
    assert evaluator._proposal_cache[frozenset({"A", "C"})][1] == {}
    assert evaluator.peak_proposal_cache_entries == 2
    assert evaluator.peak_cached_estimate_entries == 1
    evaluator.commit_configuration(frozenset({"A", "B"}))
    assert evaluator.incumbent_estimate_count == 3
    assert evaluator._proposal_cache == {}
    audit = evaluator.audit_configuration_transition(
        frozenset({"A"}), frozenset({"A", "B"}), deadline
    )
    assert audit["passed"] is True
    assert audit["nonincident_changed_query_ids"] == []
    runtime = evaluator.runtime_metadata()
    assert runtime["actual_search_planner_calls"] == 5
    assert runtime["actual_search_planner_calls_including_audit"] == 11
    assert runtime["end_to_end_search_planner_calls"] == 5


def test_first_winner_materialization_deadline_keeps_empty_incumbent() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=3)
    discarded = []

    def prepare(_membership, _deadline):
        raise SearchBudgetExpired("winner materialization deadline")

    result = greedy_add_search_incremental(
        profile,
        plan,
        _Utility(),
        lambda _membership, _deadline: _utility_result(6.0),
        _identity(),
        prepare_initial_configuration=prepare,
        discard_proposals=lambda: discarded.append(True),
    )

    assert result.termination_reason == "budget-expired-before-round"
    assert result.accepted_moves == ()
    assert result.final_ordered_candidate_ids == ()
    assert result.final_objective == profile.baseline.objective
    assert discarded == [True]


def test_first_winner_baseline_and_winner_deadlines_are_atomic() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, max_statistics_count=3)
    workload = Workload(
        "search",
        tuple(
            WorkloadQuery(query_id, f"SELECT {index}")
            for index, query_id in enumerate(("q1", "q2", "q3"), 1)
        ),
    )
    snapshot = SimpleNamespace(workload=workload)
    universe = SimpleNamespace(
        query_profiles=tuple(
            SimpleNamespace(query_id=query_id, analysis_status="supported")
            for query_id in ("q1", "q2", "q3")
        ),
        query_ids_for_candidate=lambda candidate_id: {
            "A": ("q1",),
            "B": ("q2",),
            "C": (),
        }[candidate_id],
    )

    class Utility:
        utility_contract = "weighted-workload-mean-v1"
        loss_contract = "qerror-cardinality-floor-1-v1"

        def evaluate(self, estimates):
            return UtilityResult(
                float(sum(estimates.values()) - 50),
                self.loss_contract,
                len(estimates),
                float(len(estimates)),
                tuple(
                    PerQueryUtility(query_id, 1.0, value, value, 1.0)
                    for query_id, value in sorted(estimates.items())
                ),
            )

    class Clock:
        expired = False

        def __call__(self):
            return 2.0 if self.expired else 0.0

    class Session:
        def __init__(self, clock, expire_on):
            self.clock = clock
            self.expire_on = expire_on
            self.active = ()

        def set_statement_timeout_ms(self, _timeout):
            return None

        def activate(self, configuration):
            self.active = configuration.ordered_candidate_ids
            if self.active == self.expire_on:
                self.clock.expired = True

        def estimate_query(self, query_id):
            return SimpleNamespace(estimated_rows={"q1": 10, "q2": 20, "q3": 30}[query_id])

    for expire_on, expected_baseline, expected_winner in (((), 0, 0), (("A",), 3, 0)):
        clock = Clock()
        evaluator = IncrementalPostgresSearchEvaluator(
            Session(clock, expire_on), snapshot, universe, plan, Utility()
        )
        deadline = SearchDeadline(OptimizationBudget(3, 1.0, 3), clock)
        try:
            evaluator.prepare_initial_configuration(frozenset({"A"}), deadline)
        except SearchBudgetExpired:
            pass
        else:
            raise AssertionError("deadline must expire before first-winner commit")
        assert evaluator._current_membership is None
        assert evaluator._current_estimates is None
        assert evaluator.baseline_materialization_planner_calls == expected_baseline
        assert evaluator.winner_materialization_planner_calls == expected_winner
