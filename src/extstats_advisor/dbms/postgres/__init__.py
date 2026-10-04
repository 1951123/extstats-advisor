"""Optional PostgreSQL snapshot acquisition backend.

Importing this module does not import psycopg; install the ``postgres`` extra
only when live PostgreSQL capture is required.
"""

from extstats_advisor.dbms.postgres.acquisition import PostgresSnapshotAcquirer, capture_snapshot
from extstats_advisor.dbms.postgres.deployment import (
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    deploy_postgres_recommendation,
    preflight_postgres_recommendation,
)
from extstats_advisor.dbms.postgres.errors import (
    InvalidPopulationEstimateError,
    PostgresAcquisitionError,
    PostgresConnectionError,
    PostgresPermissionError,
    RelationNotFoundError,
    SampleAcquisitionError,
    SamplingResourceLimitError,
    UnsupportedPostgresTypeError,
    UnsupportedRelationError,
)
from extstats_advisor.dbms.postgres.native_stats import (
    materialize_native_stats,
    materialize_postgresql_native_stats,
)
from extstats_advisor.dbms.postgres.planner import (
    PlannerEstimate,
    PostgresPlannerSession,
    PostgresStatisticsConfiguration,
)
from extstats_advisor.dbms.postgres.profiling import profile_postgres_singletons
from extstats_advisor.dbms.postgres.recommendation import (
    build_postgres_recommendation,
    postgres_statistics_object_name,
    quote_postgresql_identifier,
    render_postgres_sql,
)
from extstats_advisor.dbms.postgres.sandbox import (
    POSTGRES_PLANNER_SANDBOX_CONTRACT,
    PostgresSandboxMetadata,
    PreparedPostgresPlannerSandbox,
    destroy_postgres_planner_sandbox,
    prepare_postgres_planner_sandbox,
    verify_postgres_planner_sandbox,
)
from extstats_advisor.dbms.postgres.search import search_postgres_greedy_add

__all__ = [
    "DEFAULT_LOCK_TIMEOUT_MS",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
    "POSTGRES_PLANNER_SANDBOX_CONTRACT",
    "InvalidPopulationEstimateError",
    "PlannerEstimate",
    "PostgresAcquisitionError",
    "PostgresConnectionError",
    "PostgresPermissionError",
    "PostgresPlannerSession",
    "PostgresSandboxMetadata",
    "PostgresSnapshotAcquirer",
    "PostgresStatisticsConfiguration",
    "PreparedPostgresPlannerSandbox",
    "RelationNotFoundError",
    "SampleAcquisitionError",
    "SamplingResourceLimitError",
    "UnsupportedPostgresTypeError",
    "UnsupportedRelationError",
    "build_postgres_recommendation",
    "capture_snapshot",
    "deploy_postgres_recommendation",
    "destroy_postgres_planner_sandbox",
    "materialize_native_stats",
    "materialize_postgresql_native_stats",
    "postgres_statistics_object_name",
    "preflight_postgres_recommendation",
    "prepare_postgres_planner_sandbox",
    "profile_postgres_singletons",
    "quote_postgresql_identifier",
    "render_postgres_sql",
    "search_postgres_greedy_add",
    "verify_postgres_planner_sandbox",
]
