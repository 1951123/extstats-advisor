"""Capability probe for the reference pg-extstats PostgreSQL patch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from extstats_advisor.errors import NativeStatsMaterializationError

PATCHED_BACKEND_CONTRACT = "postgresql-pgextadv-16.14-v1"
PATCHED_REFERENCE_SOURCE_COMMIT = "6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6"
REQUIRED_GUCS = (
    "pg_extstats.frozen_sample_mode",
    "pg_extstats.frozen_sample_relation",
    "pg_extstats.frozen_totalrows",
)
REQUIRED_FUNCTIONS = (
    "pg_hypothetical_extstats_reset",
    "pg_hypothetical_extstats_register_definition",
    "pg_hypothetical_extstats_register_definition_absent",
    "pg_hypothetical_extstats_activate",
    "pg_hypothetical_extstats_active",
)


@dataclass(frozen=True, slots=True)
class PatchCapabilities:
    backend_contract: str
    reference_source_commit: str
    server_version: str
    server_version_num: int
    gucs: tuple[str, ...]
    functions: tuple[str, ...]


def probe_patched_postgres(connection: Any) -> PatchCapabilities:
    """Require the complete reference server surface, never a partial match."""

    try:
        server_version = str(connection.execute("SHOW server_version").fetchone()[0])
        server_version_num = int(connection.execute("SHOW server_version_num").fetchone()[0])
        if server_version_num != 160014 or not server_version.startswith("16.14"):
            raise NativeStatsMaterializationError(
                f"patched PostgreSQL 16.14 is required, observed {server_version!r}"
            )
        gucs = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM pg_catalog.pg_settings WHERE name = ANY(%s) ORDER BY name",
                (list(REQUIRED_GUCS),),
            ).fetchall()
        )
        functions = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT p.proname
                  FROM pg_catalog.pg_proc AS p
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'pg_catalog' AND p.proname = ANY(%s)
                 ORDER BY p.proname
                """,
                (list(REQUIRED_FUNCTIONS),),
            ).fetchall()
        )
    except NativeStatsMaterializationError:
        raise
    except Exception as exc:
        raise NativeStatsMaterializationError(
            "could not probe patched PostgreSQL capabilities"
        ) from exc
    if gucs != tuple(sorted(REQUIRED_GUCS)):
        missing = sorted(set(REQUIRED_GUCS) - set(gucs))
        raise NativeStatsMaterializationError(f"patched PostgreSQL GUCs are incomplete: {missing}")
    if functions != tuple(sorted(REQUIRED_FUNCTIONS)):
        missing = sorted(set(REQUIRED_FUNCTIONS) - set(functions))
        raise NativeStatsMaterializationError(
            f"patched PostgreSQL hypothetical-statistics functions are incomplete: {missing}"
        )
    return PatchCapabilities(
        PATCHED_BACKEND_CONTRACT,
        PATCHED_REFERENCE_SOURCE_COMMIT,
        server_version,
        server_version_num,
        gucs,
        functions,
    )
