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


__all__ = ["GREEDY_ADD_SEARCH_POLICY", "PostgresSearchEvaluator", "search_postgres_greedy_add"]
