"""PostgreSQL adapter for deadline-aware greedy ADD search."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from extstats_advisor.dbms.postgres.planner import (
    PostgresPlannerSession,
    PostgresStatisticsConfiguration,
)
from extstats_advisor.dbms.postgres.sandbox import POSTGRES_PLANNER_SANDBOX_CONTRACT
from extstats_advisor.errors import SearchBudgetExpired, SearchError
from extstats_advisor.optimization.plan import OptimizationPlan
from extstats_advisor.optimization.search import (
    GREEDY_ADD_SEARCH_POLICY,
    MonotonicClock,
    PlannerIdentity,
    SearchDeadline,
    SearchResult,
    greedy_add_search,
    greedy_add_search_incremental,
)
from extstats_advisor.optimization.singleton import SingletonProfile
from extstats_advisor.utility.model import UtilityResult

_MAX_TIMEOUT_MS = 2_147_483_647


def _is_query_canceled(error: BaseException) -> bool:
    return getattr(error, "sqlstate", None) == "57014" or error.__class__.__name__ in {
        "QueryCanceled",
        "QueryCanceledError",
    }


class PostgresSearchEvaluator:
    """Serial full-workload evaluator with deadline-derived statement timeouts."""

    def __init__(
        self,
        planner_session: PostgresPlannerSession,
        snapshot: Any,
        candidate_universe: Any,
        plan: OptimizationPlan,
        utility_provider: Any,
    ) -> None:
        self._session = planner_session
        self._snapshot = snapshot
        self._universe = candidate_universe
        self._plan = plan
        self._utility = utility_provider
        profiles = {profile.query_id: profile for profile in candidate_universe.query_profiles}
        self._query_ids = []
        for query in snapshot.workload.queries:
            if query.weight <= 0:
                continue
            profile = profiles.get(query.query_id)
            if profile is None or profile.analysis_status != "supported":
                raise SearchError(
                    f"positive-weight query {query.query_id!r} is outside search scope"
                )
            self._query_ids.append(query.query_id)
        if not self._query_ids:
            raise SearchError("workload has no positive-weight supported queries")
        self.live_configuration_evaluation_count = 0
        self.planner_query_estimate_count = 0

    @staticmethod
    def _timeout_ms(deadline: SearchDeadline) -> int:
        remaining = deadline.remaining_seconds
        if not math.isfinite(remaining) or remaining <= 0:
            raise SearchBudgetExpired("optimization search wall-clock budget expired")
        return max(1, min(_MAX_TIMEOUT_MS, math.ceil(remaining * 1000)))

    def _set_timeout(self, deadline: SearchDeadline) -> None:
        deadline.ensure_available()
        self._session.set_statement_timeout_ms(self._timeout_ms(deadline))

    def __call__(self, membership: frozenset[str], deadline: SearchDeadline) -> UtilityResult:
        self.live_configuration_evaluation_count += 1
        ordered = self._plan.ordered_configuration(membership)
        try:
            self._set_timeout(deadline)
            self._session.activate(PostgresStatisticsConfiguration(ordered))
            estimates = {}
            for query_id in self._query_ids:
                self._set_timeout(deadline)
                estimate = self._session.estimate_query(query_id)
                self.planner_query_estimate_count += 1
                deadline.ensure_available()
                estimates[query_id] = estimate.estimated_rows
            self._set_timeout(deadline)
            result = self._utility.evaluate(estimates)
            deadline.ensure_available()
            return result
        except SearchBudgetExpired:
            raise
        except BaseException as error:
            if _is_query_canceled(error) and deadline.expired:
                raise SearchBudgetExpired(
                    "PostgreSQL statement_timeout expired the optimization search budget"
                ) from error
            raise

    def runtime_metadata(self) -> Mapping[str, Any]:
        return {
            "live_configuration_evaluation_count": self.live_configuration_evaluation_count,
            "planner_query_estimate_count": self.planner_query_estimate_count,
        }


class IncrementalPostgresSearchEvaluator:
    """Exact ADD evaluator that only re-plans queries incident on the ADD.

    A complete estimate map is retained for the incumbent.  Each proposal is
    evaluated from that immutable incumbent map and stores its own merged map;
    only the selected proposal is committed by the search driver.
    """

    def __init__(
        self,
        planner_session: PostgresPlannerSession,
        snapshot: Any,
        candidate_universe: Any,
        plan: OptimizationPlan,
        utility_provider: Any,
    ) -> None:
        self._session = planner_session
        self._snapshot = snapshot
        self._universe = candidate_universe
        self._plan = plan
        self._utility = utility_provider
        profiles = {profile.query_id: profile for profile in candidate_universe.query_profiles}
        self._query_ids = []
        for query in snapshot.workload.queries:
            if query.weight <= 0:
                continue
            profile = profiles.get(query.query_id)
            if profile is None or profile.analysis_status != "supported":
                raise SearchError(
                    f"positive-weight query {query.query_id!r} is outside search scope"
                )
            self._query_ids.append(query.query_id)
        if not self._query_ids:
            raise SearchError("workload has no positive-weight supported queries")
        self._query_id_set = frozenset(self._query_ids)
        self._current_membership: frozenset[str] | None = None
        self._current_estimates: dict[str, int] | None = None
        self._proposal_cache: dict[frozenset[str], tuple[UtilityResult, dict[str, int]]] = {}
        self._planner_query_estimate_count = 0
        self.baseline_materialization_planner_calls = 0
        self.first_winner_materialization_planner_calls = 0
        self.proposal_configuration_evaluations = 0
        self.proposal_planner_query_calls = 0
        self.audit_planner_query_calls = 0

    @staticmethod
    def _timeout_ms(deadline: SearchDeadline) -> int:
        remaining = deadline.remaining_seconds
        if not math.isfinite(remaining) or remaining <= 0:
            raise SearchBudgetExpired("optimization search wall-clock budget expired")
        return max(1, min(_MAX_TIMEOUT_MS, math.ceil(remaining * 1000)))

    def _set_timeout(self, deadline: SearchDeadline) -> None:
        deadline.ensure_available()
        self._session.set_statement_timeout_ms(self._timeout_ms(deadline))

    def _candidate_query_ids(self, candidate_id: str) -> tuple[str, ...]:
        if hasattr(self._universe, "query_ids_for_candidate"):
            query_ids = self._universe.query_ids_for_candidate(candidate_id)
        else:
            query_ids = tuple(
                item.query_id
                for item in self._universe.incidence
                if item.candidate_id == candidate_id
            )
        return tuple(query_id for query_id in query_ids if query_id in self._query_id_set)

    def _estimate_query_ids(
        self,
        ordered_candidate_ids: tuple[str, ...],
        query_ids: tuple[str, ...],
        deadline: SearchDeadline,
        *,
        accounting: str = "proposal",
    ) -> dict[str, int]:
        self._set_timeout(deadline)
        self._session.activate(PostgresStatisticsConfiguration(ordered_candidate_ids))
        estimates: dict[str, int] = {}
        for query_id in query_ids:
            self._set_timeout(deadline)
            estimate = self._session.estimate_query(query_id)
            self._planner_query_estimate_count += 1
            if accounting == "proposal":
                self.proposal_planner_query_calls += 1
            elif accounting == "audit":
                self.audit_planner_query_calls += 1
            elif accounting not in {"baseline", "winner"}:
                raise SearchError(f"unknown planner-query accounting class: {accounting}")
            deadline.ensure_available()
            estimates[query_id] = estimate.estimated_rows
        return estimates

    def prepare_initial_configuration(
        self, membership: frozenset[str], deadline: SearchDeadline
    ) -> None:
        """Materialize the empty baseline and the first accepted winner."""

        if self._current_membership is not None:
            if self._current_membership != membership:
                raise SearchError("incremental incumbent was prepared twice")
            return
        ordered = self._plan.ordered_configuration(membership)
        baseline = self._estimate_query_ids(
            (), tuple(self._query_ids), deadline, accounting="baseline"
        )
        self.baseline_materialization_planner_calls = len(baseline)
        winner = self._estimate_query_ids(
            ordered, tuple(self._query_ids), deadline, accounting="winner"
        )
        self.first_winner_materialization_planner_calls = len(winner)
        self._current_membership = membership
        self._current_estimates = winner

    def __call__(self, membership: frozenset[str], deadline: SearchDeadline) -> UtilityResult:
        if self._current_membership is None or self._current_estimates is None:
            raise SearchError("incremental evaluator has no committed incumbent")
        proposed = frozenset(membership)
        added = proposed - self._current_membership
        removed = self._current_membership - proposed
        if removed or len(added) != 1:
            raise SearchError("incremental evaluator requires one-candidate ADD proposals")
        candidate_id = next(iter(added))
        ordered = self._plan.ordered_configuration(proposed)
        affected = self._candidate_query_ids(candidate_id)
        changed = self._estimate_query_ids(ordered, affected, deadline)
        merged = dict(self._current_estimates)
        merged.update(changed)
        result = self._utility.evaluate(merged)
        if result.loss_contract != self._utility.loss_contract:
            raise SearchError("utility result loss contract changed during search")
        deadline.ensure_available()
        self.proposal_configuration_evaluations += 1
        self._proposal_cache[proposed] = (result, merged)
        return result

    def commit_configuration(self, membership: frozenset[str]) -> None:
        proposed = frozenset(membership)
        cached = self._proposal_cache.get(proposed)
        if cached is None:
            raise SearchError("accepted incremental proposal has no cached estimate map")
        self._current_membership = proposed
        self._current_estimates = dict(cached[1])

    def audit_configuration_transition(
        self,
        current_membership: frozenset[str],
        proposed_membership: frozenset[str],
        deadline: SearchDeadline,
    ) -> Mapping[str, Any]:
        """Audit the exact nonincident Plan Rows invariant for one ADD."""

        added = frozenset(proposed_membership) - frozenset(current_membership)
        if frozenset(current_membership) - frozenset(proposed_membership) or len(added) != 1:
            raise SearchError("nonincident audit requires one-candidate ADD")
        candidate_id = next(iter(added))
        query_ids = tuple(self._query_ids)
        before = self._estimate_query_ids(
            self._plan.ordered_configuration(current_membership),
            query_ids,
            deadline,
            accounting="audit",
        )
        after = self._estimate_query_ids(
            self._plan.ordered_configuration(proposed_membership),
            query_ids,
            deadline,
            accounting="audit",
        )
        changed = tuple(query_id for query_id in query_ids if before[query_id] != after[query_id])
        incident = frozenset(self._candidate_query_ids(candidate_id))
        nonincident = tuple(query_id for query_id in changed if query_id not in incident)
        if nonincident:
            raise SearchError(
                "incremental incidence invariant violated for nonincident queries: "
                + ", ".join(nonincident)
            )
        return {
            "candidate_id": candidate_id,
            "current_membership": list(self._plan.ordered_configuration(current_membership)),
            "proposed_membership": list(self._plan.ordered_configuration(proposed_membership)),
            "incident_query_count": len(incident),
            "changed_query_ids": list(changed),
            "nonincident_changed_query_ids": list(nonincident),
            "passed": True,
        }

    def runtime_metadata(self) -> Mapping[str, Any]:
        actual = self._planner_query_estimate_count
        # This is the old search's full-workload call count for the same
        # completed proposal evaluations.  Singleton objectives were already
        # cached by the profile and are therefore not part of this reference.
        reference = len(self._query_ids) * self.proposal_configuration_evaluations
        return {
            "baseline_materialization_planner_calls": self.baseline_materialization_planner_calls,
            "first_winner_materialization_planner_calls": self.first_winner_materialization_planner_calls,
            "proposal_configuration_evaluations": self.proposal_configuration_evaluations,
            "proposal_planner_query_calls": self.proposal_planner_query_calls,
            "planner_query_estimate_count": actual,
            "reference_full_workload_planner_calls": reference,
            "saved_planner_query_calls": reference - actual,
            "planner_query_reduction_fraction": (
                (reference - actual) / reference if reference else 0.0
            ),
            "audit_planner_query_calls": self.audit_planner_query_calls,
            "winner_cache_entry_count": len(self._proposal_cache),
        }


def search_postgres_greedy_add_reference(
    planner_session: PostgresPlannerSession,
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    singleton_profile: SingletonProfile,
    plan: OptimizationPlan,
    utility_provider: Any,
    *,
    clock: Callable[[], float] | MonotonicClock = time.monotonic,
) -> SearchResult:
    """Run one live PostgreSQL search in one already-open planner session."""

    evaluator = PostgresSearchEvaluator(
        planner_session, snapshot, candidate_universe, plan, utility_provider
    )
    return greedy_add_search(
        singleton_profile,
        plan,
        utility_provider,
        evaluator,
        PlannerIdentity(
            POSTGRES_PLANNER_SANDBOX_CONTRACT,
            native_repository.backend_contract,
            native_repository.server_version,
            native_repository.server_version_num,
            native_repository.ordinary_stats_fingerprint,
        ),
        clock=clock,
        runtime_metadata_provider=evaluator.runtime_metadata,
    )


def search_postgres_greedy_add(
    planner_session: PostgresPlannerSession,
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    singleton_profile: SingletonProfile,
    plan: OptimizationPlan,
    utility_provider: Any,
    *,
    clock: Callable[[], float] | MonotonicClock = time.monotonic,
) -> SearchResult:
    """Run v2 incremental search, retaining v1 as an explicit compatibility path."""

    if plan.budget.max_statistics_count is None:
        return search_postgres_greedy_add_reference(
            planner_session,
            snapshot,
            candidate_universe,
            native_repository,
            singleton_profile,
            plan,
            utility_provider,
            clock=clock,
        )
    evaluator = IncrementalPostgresSearchEvaluator(
        planner_session, snapshot, candidate_universe, plan, utility_provider
    )
    return greedy_add_search_incremental(
        singleton_profile,
        plan,
        utility_provider,
        evaluator,
        PlannerIdentity(
            POSTGRES_PLANNER_SANDBOX_CONTRACT,
            native_repository.backend_contract,
            native_repository.server_version,
            native_repository.server_version_num,
            native_repository.ordinary_stats_fingerprint,
        ),
        clock=clock,
        runtime_metadata_provider=evaluator.runtime_metadata,
        prepare_initial_configuration=evaluator.prepare_initial_configuration,
        commit_configuration=evaluator.commit_configuration,
    )


__all__ = [
    "GREEDY_ADD_SEARCH_POLICY",
    "IncrementalPostgresSearchEvaluator",
    "PostgresSearchEvaluator",
    "search_postgres_greedy_add",
    "search_postgres_greedy_add_reference",
]
