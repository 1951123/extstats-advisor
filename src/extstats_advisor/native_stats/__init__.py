"""Immutable PostgreSQL native-statistics repository v1."""

from extstats_advisor.native_stats.model import (
    NATIVE_STATS_REPOSITORY_FORMAT_VERSION,
    NativeStatsCandidate,
    NativeStatsRepository,
    NativeStatsMaterialization,
    PostgreSQLNativeStatsRepository,
)
from extstats_advisor.native_stats.repository import (
    load_native_stats_repository,
    load_postgresql_native_stats_repository,
    validate_native_stats_repository,
    validate_postgresql_native_stats_repository,
    write_native_stats_repository,
    write_postgresql_native_stats_repository,
)

__all__ = [
    "NATIVE_STATS_REPOSITORY_FORMAT_VERSION",
    "NativeStatsCandidate",
    "NativeStatsMaterialization",
    "NativeStatsRepository",
    "PostgreSQLNativeStatsRepository",
    "load_native_stats_repository",
    "load_postgresql_native_stats_repository",
    "validate_native_stats_repository",
    "validate_postgresql_native_stats_repository",
    "write_native_stats_repository",
    "write_postgresql_native_stats_repository",
]
