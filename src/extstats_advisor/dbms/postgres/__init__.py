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

__all__ = [
    "InvalidPopulationEstimateError",
    "PostgresAcquisitionError",
    "PostgresConnectionError",
    "PostgresPermissionError",
    "PostgresSnapshotAcquirer",
    "RelationNotFoundError",
    "SampleAcquisitionError",
    "SamplingResourceLimitError",
    "UnsupportedPostgresTypeError",
    "UnsupportedRelationError",
    "capture_snapshot",
    "materialize_native_stats",
    "materialize_postgresql_native_stats",
]
