"""PostgreSQL singleton profiling over one verified planner session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from extstats_advisor.dbms.postgres.planner import PostgresStatisticsConfiguration
from extstats_advisor.dbms.postgres.sandbox import POSTGRES_PLANNER_SANDBOX_CONTRACT
from extstats_advisor.optimization.singleton import SingletonProfile, profile_singletons


def profile_postgres_singletons(
    planner_session: Any,
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    utility_provider: Any,
    *,
    ground_truth_semantic_digest: str,
    runtime_metadata: Mapping[str, Any] | None = None,
) -> SingletonProfile:
    """Profile baseline and all PRESENT native candidates on one session."""

    return profile_singletons(
        planner_session,
        snapshot,
        candidate_universe,
        native_repository,
        utility_provider,
        lambda candidate_ids: PostgresStatisticsConfiguration(tuple(candidate_ids)),
        ground_truth_semantic_digest=ground_truth_semantic_digest,
        sandbox_contract=POSTGRES_PLANNER_SANDBOX_CONTRACT,
        runtime_metadata=runtime_metadata,
    )
