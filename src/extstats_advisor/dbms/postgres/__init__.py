"""Optional PostgreSQL snapshot acquisition backend.

Importing this module does not import psycopg; install the ``postgres`` extra
only when live PostgreSQL capture is required.
"""

from extstats_advisor.dbms.postgres.acquisition import PostgresSnapshotAcquirer, capture_snapshot
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
from extstats_advisor.dbms.postgres.sandbox import (
    POSTGRES_PLANNER_SANDBOX_CONTRACT,
    PostgresSandboxMetadata,
    PreparedPostgresPlannerSandbox,
    destroy_postgres_planner_sandbox,
    prepare_postgres_planner_sandbox,
    verify_postgres_planner_sandbox,
)

__all__ = [
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
    "capture_snapshot",
    "destroy_postgres_planner_sandbox",
    "materialize_native_stats",
    "materialize_postgresql_native_stats",
    "prepare_postgres_planner_sandbox",
    "verify_postgres_planner_sandbox",
]
