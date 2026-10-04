"""Read-only PostgreSQL acquisition into an AdvisorSnapshot v1."""

from __future__ import annotations

import json
import math
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa

from extstats_advisor.dbms.base import (
    AcquisitionInputs,
    AcquisitionRequest,
    DBMSAcquirer,
    SamplePolicy,
)
from extstats_advisor.dbms.postgres.errors import (
    InvalidPopulationEstimateError,
    PostgresAcquisitionError,
    PostgresConnectionError,
    PostgresPermissionError,
    RelationNotFoundError,
    SampleAcquisitionError,
)
from extstats_advisor.dbms.postgres.sampling import (
    SAMPLING_METHOD,
    SamplingAttempt,
    deterministic_reservoir,
    initial_percentage,
    next_percentage,
)
from extstats_advisor.dbms.postgres.schema import (
    ResolvedRelation,
    extract_schema,
    relation_sql_name,
    resolve_relation,
    to_relation_schema,
)
from extstats_advisor.dbms.postgres.types import TYPE_MAPPING_CONTRACT_VERSION, convert_row
from extstats_advisor.snapshot.model import (
    AdvisorSnapshot,
    DBMSIdentity,
    PopulationMetadata,
    SnapshotConsistency,
    Workload,
)


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise PostgresConnectionError(
            "PostgreSQL capture requires the optional dependency; install extstats-advisor[postgres]"
        ) from exc
    return psycopg


def _workload_from_path(path: Path) -> Workload:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PostgresAcquisitionError(f"could not read workload JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise PostgresAcquisitionError("workload JSON must be an object")
    forbidden = {"truth", "truths", "exact_cardinality", "qerror", "benchmark"}
    provenance = value.get("provenance", {})
    if not isinstance(provenance, Mapping):
        raise PostgresAcquisitionError("workload provenance must be an object")
    if forbidden.intersection(value) or forbidden.intersection(provenance):
        raise PostgresAcquisitionError("workload contains research-only truth or evaluation fields")
    return Workload.from_dict(value)


class PostgresSnapshotAcquirer(DBMSAcquirer):
    """Acquire one relation through a bounded, repeatable-read PostgreSQL session."""

    def __init__(
        self,
        dsn: str,
        *,
        lock_timeout_ms: int = 5_000,
        statement_timeout_ms: int = 60_000,
        batch_size: int = 256,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise PostgresConnectionError("PostgreSQL DSN is required")
        if lock_timeout_ms < 1 or statement_timeout_ms < 1 or batch_size < 1:
            raise ValueError("PostgreSQL acquisition timeouts and batch_size must be positive")
        self._dsn = dsn
        self._lock_timeout_ms = lock_timeout_ms
        self._statement_timeout_ms = statement_timeout_ms
        self._batch_size = batch_size

    def acquire(self, request: AcquisitionRequest) -> AcquisitionInputs:
        psycopg = _psycopg()
        try:
            connection = psycopg.connect(
                self._dsn,
                application_name="extstats-advisor",
                autocommit=False,
            )
        except psycopg.Error as exc:
            raise PostgresConnectionError("could not connect to PostgreSQL") from exc
        try:
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SET LOCAL lock_timeout = %s", (f"{self._lock_timeout_ms}ms",))
            connection.execute(
                "SET LOCAL statement_timeout = %s", (f"{self._statement_timeout_ms}ms",)
            )
            connection.execute("SET LOCAL timezone = 'UTC'")
            connection.execute("SET LOCAL application_name = 'extstats-advisor'")
            self._verify_transaction(connection)
            server_version = str(connection.execute("SHOW server_version").fetchone()[0])
            server_version_num = int(connection.execute("SHOW server_version_num").fetchone()[0])
            relation_meta = resolve_relation(connection, request.relation_selector)
            relation = extract_schema(connection, relation_meta)
            if not math.isfinite(relation.reltuples) or relation.reltuples <= 0:
                raise InvalidPopulationEstimateError(
                    f"PostgreSQL pg_class.reltuples is unavailable or stale for "
                    f"{relation.relation_name.schema}.{relation.relation_name.name}"
                )
            seed = request.sampling.seed
            if seed is None:
                seed = secrets.randbits(63)
            samples, sampling_provenance = self._sample_relation(
                connection, relation, request.sampling, seed
            )
            population = PopulationMetadata(
                relation.relation_id,
                relation.reltuples,
                "estimate",
                "postgresql.pg_class.reltuples",
            )
            return AcquisitionInputs(
                (to_relation_schema(relation),),
                (population,),
                {relation.relation_id: samples},
                {
                    "backend": "postgresql",
                    "server_version": server_version,
                    "server_version_num": server_version_num,
                    "sampling": sampling_provenance,
                    "type_mapping_contract_version": TYPE_MAPPING_CONTRACT_VERSION,
                    "relation_selector_kind": "postgresql-regclass",
                },
            )
        except PostgresAcquisitionError:
            raise
        except (psycopg.errors.InvalidName, psycopg.errors.UndefinedTable) as exc:
            raise RelationNotFoundError(
                "invalid or unresolved PostgreSQL relation selector"
            ) from exc
        except psycopg.errors.InsufficientPrivilege as exc:
            raise PostgresPermissionError(
                "PostgreSQL denied access to the requested source"
            ) from exc
        except psycopg.errors.QueryCanceled as exc:
            raise SampleAcquisitionError(
                "PostgreSQL canceled the bounded acquisition query"
            ) from exc
        except psycopg.Error as exc:
            raise PostgresAcquisitionError("PostgreSQL acquisition query failed") from exc
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def capture(self, request: AcquisitionRequest, workload: Workload) -> AdvisorSnapshot:
        started = time.monotonic()
        inputs = self.acquire(request)
        provenance = dict(inputs.provenance)
        provenance["sampling"]["requested_rows"] = request.sampling.sample_rows
        provenance["sampling"]["seed"] = provenance["sampling"].get("seed", request.sampling.seed)
        return AdvisorSnapshot(
            inputs.schemas,
            inputs.populations,
            workload,
            inputs.samples,
            DBMSIdentity("postgresql", provenance["server_version"]),
            SnapshotConsistency(),
            provenance,
            {"capture_elapsed_seconds": round(time.monotonic() - started, 6)},
        )

    def _verify_transaction(self, connection: Any) -> None:
        read_only = str(connection.execute("SHOW transaction_read_only").fetchone()[0]).lower()
        isolation = str(connection.execute("SHOW transaction_isolation").fetchone()[0]).lower()
        if read_only != "on" or isolation != "repeatable read":
            raise PostgresConnectionError("PostgreSQL session is not repeatable-read and read-only")

    def _sample_relation(
        self, connection: Any, relation: ResolvedRelation, policy: SamplePolicy, seed: int
    ) -> tuple[pa.Table, dict[str, Any]]:
        psycopg = _psycopg()
        candidate_limit = max(
            policy.sample_rows, policy.sample_rows * policy.candidate_row_limit_multiplier
        )
        percentage = initial_percentage(policy.sample_rows, relation.reltuples)
        attempts: list[SamplingAttempt] = []
        while True:
            query = self._sample_query(relation, percentage, seed, psycopg.sql)
            try:
                with connection.cursor(name="extstats_advisor_sample") as cursor:
                    cursor.itersize = self._batch_size
                    cursor.execute(query)
                    rows, candidate_rows = deterministic_reservoir(
                        cursor, policy.sample_rows, seed, candidate_limit=candidate_limit
                    )
            except PostgresAcquisitionError:
                raise
            except psycopg.Error as exc:
                raise SampleAcquisitionError("could not stream PostgreSQL sample rows") from exc
            attempts.append(SamplingAttempt(percentage, candidate_rows, len(rows)))
            if len(rows) >= policy.sample_rows or percentage >= 100.0:
                if not rows:
                    raise SampleAcquisitionError("the relation contains no visible rows")
                try:
                    converted = [convert_row(row, relation.mappings) for row in rows]
                    arrays = [
                        pa.array([row[index] for row in converted], type=mapping.arrow_type)
                        for index, mapping in enumerate(relation.mappings)
                    ]
                    table = pa.Table.from_arrays(arrays, schema=relation.arrow_schema)
                except (pa.ArrowException, TypeError, ValueError) as exc:
                    raise SampleAcquisitionError(
                        "could not convert PostgreSQL rows to Arrow"
                    ) from exc
                return table, {
                    "method": SAMPLING_METHOD,
                    "seed": seed,
                    "successful_percentage": percentage,
                    "requested_rows": policy.sample_rows,
                    "actual_rows": table.num_rows,
                    "candidate_row_limit": candidate_limit,
                    "attempts": [
                        {
                            "percentage": item.percentage,
                            "candidate_rows": item.candidate_rows,
                            "retained_rows": item.retained_rows,
                        }
                        for item in attempts
                    ],
                }
            percentage = next_percentage(percentage)

    @staticmethod
    def _sample_query(
        relation: ResolvedRelation, percentage: float, seed: int, sql_module: Any
    ) -> Any:
        columns = sql_module.SQL(", ").join(
            sql_module.Identifier(column.name) for column in relation.columns
        )
        return sql_module.SQL(
            "SELECT {} FROM ONLY {} TABLESAMPLE SYSTEM ({}) REPEATABLE ({})"
        ).format(
            columns,
            relation_sql_name(relation.relation_name, sql_module),
            sql_module.Literal(percentage),
            sql_module.Literal(seed),
        )


def capture_snapshot(
    *,
    dsn: str,
    relation_selector: str,
    workload_path: Path,
    sample_rows: int,
    sample_seed: int | None = None,
    candidate_row_limit_multiplier: int = 20,
) -> AdvisorSnapshot:
    workload = _workload_from_path(Path(workload_path))
    request = AcquisitionRequest(
        relation_selector,
        SamplePolicy(
            sample_rows,
            seed=sample_seed,
            candidate_row_limit_multiplier=candidate_row_limit_multiplier,
        ),
    )
    return PostgresSnapshotAcquirer(dsn).capture(request, workload)
