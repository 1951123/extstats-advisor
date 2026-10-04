"""Persistent, sample-only PostgreSQL cardinality-estimation sandbox."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from extstats_advisor.dbms.postgres.native_stats import _psycopg
from extstats_advisor.dbms.postgres.ordinary_stats import ordinary_stats_fingerprint
from extstats_advisor.dbms.postgres.patch import (
    PATCHED_BACKEND_CONTRACT,
    PatchCapabilities,
    probe_patched_postgres,
)
from extstats_advisor.dbms.postgres.staging import (
    copy_arrow_table_to_relation,
    create_relation,
    relation_identifier,
)
from extstats_advisor.errors import (
    NativeStatsRepositoryValidationError,
    PlannerSandboxError,
    PlannerSandboxValidationError,
)
from extstats_advisor.native_stats.model import NativeStatsRepository
from extstats_advisor.native_stats.repository import (
    validate_native_stats_repository_compatibility,
)
from extstats_advisor.snapshot.model import AdvisorSnapshot, RelationName, RelationSchema

if TYPE_CHECKING:
    from extstats_advisor.candidates.universe import CandidateUniverse

POSTGRES_PLANNER_SANDBOX_CONTRACT = "postgresql-planner-sandbox-v1"
INTERNAL_SCHEMA = "extstats_advisor_internal"
METADATA_TABLE = "sandbox_metadata"
FROZEN_RELATION_PREFIX = "frozen_sample_"
_ADVISORY_LOCK_KEY = "extstats-advisor:postgresql-planner-sandbox-v1"


@dataclass(frozen=True, slots=True)
class PostgresSandboxMetadata:
    sandbox_contract: str
    source_snapshot_semantic_digest: str
    candidate_universe_semantic_digest: str
    native_stats_repository_semantic_digest: str
    backend_contract: str
    server_version: str
    server_version_num: int
    statistics_target: int
    relation_id: str
    relation_name: RelationName
    frozen_relation_name: RelationName
    managed_schema_created: bool
    internal_schema_created: bool
    sample_row_count: int
    population_row_count: float
    population_quality: str
    ordinary_stats_fingerprint: str
    repository_candidate_count: int
    repository_present_count: int
    repository_absent_count: int
    prepared_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sandbox_contract": self.sandbox_contract,
            "source_snapshot_semantic_digest": self.source_snapshot_semantic_digest,
            "candidate_universe_semantic_digest": self.candidate_universe_semantic_digest,
            "native_stats_repository_semantic_digest": self.native_stats_repository_semantic_digest,
            "backend_contract": self.backend_contract,
            "server_version": self.server_version,
            "server_version_num": self.server_version_num,
            "statistics_target": self.statistics_target,
            "relation_id": self.relation_id,
            "relation_name": self.relation_name.to_dict(),
            "frozen_relation_name": self.frozen_relation_name.to_dict(),
            "managed_schema_created": self.managed_schema_created,
            "internal_schema_created": self.internal_schema_created,
            "sample_row_count": self.sample_row_count,
            "population_row_count": self.population_row_count,
            "population_quality": self.population_quality,
            "ordinary_stats_fingerprint": self.ordinary_stats_fingerprint,
            "repository_candidate_count": self.repository_candidate_count,
            "repository_present_count": self.repository_present_count,
            "repository_absent_count": self.repository_absent_count,
            "prepared_at": self.prepared_at,
        }


@dataclass(frozen=True, slots=True)
class PreparedPostgresPlannerSandbox:
    metadata: PostgresSandboxMetadata
    target_relation_oid: int


def _require_sandbox_schema(schema: RelationSchema) -> str:
    if not schema.relation_name.schema:
        raise PlannerSandboxError(
            "planner sandbox v1 requires a structured PostgreSQL relation schema"
        )
    if schema.relation_name.schema == INTERNAL_SCHEMA:
        raise PlannerSandboxError("source relation cannot use the advisor internal schema")
    return schema.relation_name.schema


def _qualified(relation: RelationName, sql: Any) -> Any:
    if not relation.schema:
        raise PlannerSandboxError("planner sandbox relation schema is required")
    return relation_identifier(relation.schema, relation.name, sql)


def _metadata_relation(sql: Any) -> Any:
    return relation_identifier(INTERNAL_SCHEMA, METADATA_TABLE, sql)


def _frozen_relation(snapshot: AdvisorSnapshot) -> RelationName:
    return RelationName(
        f"{FROZEN_RELATION_PREFIX}{snapshot.semantic_digest[:24]}",
        schema=INTERNAL_SCHEMA,
    )


def _relation_lookup(connection: Any, relation: RelationName) -> tuple[Any, ...] | None:
    return connection.execute(
        """
        SELECT c.oid::bigint, c.relkind, c.reltuples::double precision,
               c.n_mod_since_analyze::double precision, c.reloptions,
               pg_get_userbyid(c.relowner), n.nspname, c.relname
          FROM pg_catalog.pg_class AS c
          JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = %s
        """,
        (relation.schema, relation.name),
    ).fetchone()


def _schema_exists(connection: Any, schema_name: str) -> tuple[int, str] | None:
    row = connection.execute(
        """
        SELECT n.oid::bigint, pg_get_userbyid(n.nspowner)
          FROM pg_catalog.pg_namespace AS n
         WHERE n.nspname = %s
        """,
        (schema_name,),
    ).fetchone()
    return None if row is None else (int(row[0]), str(row[1]))


def _current_user(connection: Any) -> str:
    return str(connection.execute("SELECT current_user").fetchone()[0])


def _create_metadata_table(connection: Any, sql: Any) -> None:
    connection.execute(
        sql.SQL(
            """
            CREATE TABLE {} (
                sandbox_contract text PRIMARY KEY,
                source_snapshot_semantic_digest text NOT NULL,
                candidate_universe_semantic_digest text NOT NULL,
                native_stats_repository_semantic_digest text NOT NULL,
                backend_contract text NOT NULL,
                server_version text NOT NULL,
                server_version_num integer NOT NULL,
                statistics_target integer NOT NULL,
                relation_id text NOT NULL,
                relation_catalog text,
                relation_schema text NOT NULL,
                relation_name text NOT NULL,
                frozen_relation_schema text NOT NULL,
                frozen_relation_name text NOT NULL,
                managed_schema_created boolean NOT NULL,
                internal_schema_created boolean NOT NULL,
                sample_row_count bigint NOT NULL,
                population_row_count double precision NOT NULL,
                population_quality text NOT NULL,
                ordinary_stats_fingerprint text NOT NULL,
                repository_candidate_count integer NOT NULL,
                repository_present_count integer NOT NULL,
                repository_absent_count integer NOT NULL,
                prepared_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        ).format(_metadata_relation(sql))
    )


def _ensure_internal_schema(connection: Any, sql: Any, user: str) -> bool:
    existing = _schema_exists(connection, INTERNAL_SCHEMA)
    created = existing is None
    if existing is None:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(INTERNAL_SCHEMA)))
    elif existing[1] != user:
        raise PlannerSandboxError(
            "existing advisor internal schema is not owned by the connected advisor role"
        )
    metadata = _relation_lookup(connection, RelationName(METADATA_TABLE, schema=INTERNAL_SCHEMA))
    if metadata is None:
        objects = connection.execute(
            """
            SELECT c.relname
              FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = %s
            """,
            (INTERNAL_SCHEMA,),
        ).fetchall()
        if objects:
            raise PlannerSandboxError(
                "existing advisor internal schema contains unrecognized objects"
            )
        _create_metadata_table(connection, sql)
    else:
        if metadata[5] != user:
            raise PlannerSandboxError("sandbox metadata table is not advisor-owned")
        other_objects = connection.execute(
            """
            SELECT c.relname
              FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = %s
               AND c.relname NOT IN (%s, %s)
            """,
            (INTERNAL_SCHEMA, METADATA_TABLE, f"{METADATA_TABLE}_pkey"),
        ).fetchall()
        if other_objects:
            raise PlannerSandboxError(
                "advisor internal schema contains objects outside the sandbox contract"
            )
        columns = {
            row[0]
            for row in connection.execute(
                """
                SELECT a.attname
                  FROM pg_catalog.pg_attribute AS a
                 WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
                """,
                (metadata[0],),
            ).fetchall()
        }
        required = {
            "sandbox_contract",
            "source_snapshot_semantic_digest",
            "candidate_universe_semantic_digest",
            "native_stats_repository_semantic_digest",
            "backend_contract",
            "server_version",
            "server_version_num",
            "statistics_target",
            "relation_id",
            "relation_catalog",
            "relation_schema",
            "relation_name",
            "frozen_relation_schema",
            "frozen_relation_name",
            "managed_schema_created",
            "internal_schema_created",
            "sample_row_count",
            "population_row_count",
            "population_quality",
            "ordinary_stats_fingerprint",
            "repository_candidate_count",
            "repository_present_count",
            "repository_absent_count",
            "prepared_at",
        }
        if columns != required:
            raise PlannerSandboxError("sandbox metadata table does not match the v1 contract")
        if connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(_metadata_relation(sql))
        ).fetchone()[0]:
            raise PlannerSandboxError("an advisor planner sandbox is already prepared")
    return created


def _ensure_relation_absent(connection: Any, relation: RelationName) -> None:
    if _relation_lookup(connection, relation) is not None:
        raise PlannerSandboxError(
            f"managed sandbox relation already exists: {relation.schema}.{relation.name}"
        )


def _ensure_source_schema(connection: Any, schema_name: str, sql: Any) -> bool:
    if _schema_exists(connection, schema_name) is not None:
        return False
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    return True


def _set_autovacuum_disabled(connection: Any, relation: Any, sql: Any) -> None:
    connection.execute(sql.SQL("ALTER TABLE {} SET (autovacuum_enabled = false)").format(relation))


def _set_replay_config(connection: Any, frozen: RelationName, population: float) -> None:
    connection.execute(
        "SELECT set_config('pg_extstats.frozen_sample_mode', %s, true)",
        ("replay",),
    )
    connection.execute(
        "SELECT set_config('pg_extstats.frozen_sample_relation', %s, true)",
        (f'"{frozen.schema}"."{frozen.name}"',),
    )
    connection.execute(
        "SELECT set_config('pg_extstats.frozen_totalrows', %s, true)",
        (str(population),),
    )


def _metadata_from_row(row: tuple[Any, ...]) -> PostgresSandboxMetadata:
    return PostgresSandboxMetadata(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        str(row[5]),
        int(row[6]),
        int(row[7]),
        str(row[8]),
        RelationName(str(row[11]), row[10], row[9]),
        RelationName(str(row[13]), str(row[12])),
        bool(row[14]),
        bool(row[15]),
        int(row[16]),
        float(row[17]),
        str(row[18]),
        str(row[19]),
        int(row[20]),
        int(row[21]),
        int(row[22]),
        str(row[23]),
    )


def _read_metadata(connection: Any, sql: Any) -> PostgresSandboxMetadata:
    rows = connection.execute(
        sql.SQL(
            """
            SELECT sandbox_contract, source_snapshot_semantic_digest,
                   candidate_universe_semantic_digest,
                   native_stats_repository_semantic_digest, backend_contract,
                   server_version, server_version_num, statistics_target,
                   relation_id, relation_catalog, relation_schema, relation_name,
                   frozen_relation_schema, frozen_relation_name,
                   managed_schema_created, internal_schema_created, sample_row_count,
                   population_row_count, population_quality,
                   ordinary_stats_fingerprint, repository_candidate_count,
                   repository_present_count, repository_absent_count, prepared_at
              FROM {}
            """
        ).format(_metadata_relation(sql))
    ).fetchall()
    if len(rows) != 1:
        raise PlannerSandboxValidationError("sandbox metadata must contain exactly one row")
    return _metadata_from_row(rows[0])


def _relation_columns(connection: Any, oid: int) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        connection.execute(
            """
            SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod),
                   NOT a.attnotnull,
                   CASE WHEN a.attcollation = 0 THEN NULL
                        ELSE quote_ident(cn.nspname) || '.' || quote_ident(co.collname) END
              FROM pg_catalog.pg_attribute AS a
              JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
              LEFT JOIN pg_catalog.pg_collation AS co ON co.oid = a.attcollation
              LEFT JOIN pg_catalog.pg_namespace AS cn ON cn.oid = co.collnamespace
             WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum
            """,
            (oid,),
        ).fetchall()
    )


def _verify_relation_columns(connection: Any, oid: int, schema: RelationSchema) -> None:
    expected = tuple(
        (column.ordinal, column.name, column.native_type, column.nullable, column.native_collation)
        for column in schema.columns
    )
    if _relation_columns(connection, oid) != expected:
        raise PlannerSandboxValidationError("sandbox relation columns do not match snapshot schema")


def _reloptions_disabled(options: Any) -> bool:
    return options is not None and "autovacuum_enabled=false" in set(options)


def _verify_live(
    connection: Any,
    snapshot: AdvisorSnapshot,
    universe: CandidateUniverse,
    repository: NativeStatsRepository,
    capabilities: PatchCapabilities,
    sql: Any,
) -> tuple[PostgresSandboxMetadata, int, dict[str, Any]]:
    compatibility = validate_native_stats_repository_compatibility(repository, snapshot, universe)
    schema = snapshot.schemas[0]
    _require_sandbox_schema(schema)
    metadata = _read_metadata(connection, sql)
    if metadata.sandbox_contract != POSTGRES_PLANNER_SANDBOX_CONTRACT:
        raise PlannerSandboxValidationError("unsupported planner sandbox contract")
    if metadata.source_snapshot_semantic_digest != snapshot.semantic_digest:
        raise PlannerSandboxValidationError("sandbox snapshot digest mismatch")
    if metadata.candidate_universe_semantic_digest != universe.semantic_digest:
        raise PlannerSandboxValidationError("sandbox candidate-universe digest mismatch")
    if metadata.native_stats_repository_semantic_digest != repository.semantic_digest:
        raise PlannerSandboxValidationError("sandbox native repository digest mismatch")
    if metadata.backend_contract != capabilities.backend_contract:
        raise PlannerSandboxValidationError("sandbox backend contract mismatch")
    if repository.backend_contract != capabilities.backend_contract:
        raise PlannerSandboxValidationError("native repository backend contract mismatch")
    if repository.server_version_num != capabilities.server_version_num:
        raise PlannerSandboxValidationError("native repository server version mismatch")
    if repository.server_version != capabilities.server_version:
        raise PlannerSandboxValidationError("native repository server version string mismatch")
    if metadata.server_version_num != capabilities.server_version_num:
        raise PlannerSandboxValidationError("sandbox server version mismatch")
    if metadata.statistics_target != repository.statistics_target:
        raise PlannerSandboxValidationError("sandbox statistics target mismatch")
    if metadata.relation_id != schema.relation_id or metadata.relation_name != schema.relation_name:
        raise PlannerSandboxValidationError("sandbox relation identity mismatch")
    if metadata.sample_row_count != snapshot.samples[schema.relation_id].num_rows:
        raise PlannerSandboxValidationError("sandbox metadata sample row count mismatch")
    if metadata.repository_candidate_count != compatibility["candidate_count"]:
        raise PlannerSandboxValidationError("sandbox repository candidate count mismatch")
    target = _relation_lookup(connection, metadata.relation_name)
    frozen = _relation_lookup(connection, metadata.frozen_relation_name)
    if target is None or frozen is None:
        raise PlannerSandboxValidationError("sandbox target or frozen sample relation is missing")
    user = _current_user(connection)
    if target[1] != "r" or target[5] != user or frozen[1] != "r" or frozen[5] != user:
        raise PlannerSandboxValidationError("sandbox relation ownership or kind is invalid")
    _verify_relation_columns(connection, int(target[0]), schema)
    _verify_relation_columns(connection, int(frozen[0]), schema)
    target_count = int(
        connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(_qualified(metadata.relation_name, sql))
        ).fetchone()[0]
    )
    frozen_count = int(
        connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(
                _qualified(metadata.frozen_relation_name, sql)
            )
        ).fetchone()[0]
    )
    expected_count = snapshot.samples[schema.relation_id].num_rows
    if target_count != expected_count or frozen_count != expected_count:
        raise PlannerSandboxValidationError("sandbox sample row count drift")
    if not math.isclose(float(target[2]), metadata.population_row_count, rel_tol=1e-6, abs_tol=1.0):
        raise PlannerSandboxValidationError("sandbox reltuples population drift")
    if float(target[3]) > 0:
        raise PlannerSandboxValidationError(
            "sandbox managed relation has modifications since ANALYZE"
        )
    if not _reloptions_disabled(target[4]) or not _reloptions_disabled(frozen[4]):
        raise PlannerSandboxValidationError("sandbox autovacuum is not disabled")
    fingerprint = ordinary_stats_fingerprint(connection, int(target[0]))
    if fingerprint != metadata.ordinary_stats_fingerprint:
        raise PlannerSandboxValidationError("sandbox ordinary statistics fingerprint drift")
    if fingerprint != repository.ordinary_stats_fingerprint:
        raise PlannerSandboxValidationError("sandbox ordinary statistics differ from repository")
    extstats_count = int(
        connection.execute(
            "SELECT count(*) FROM pg_catalog.pg_statistic_ext WHERE stxrelid = %s",
            (int(target[0]),),
        ).fetchone()[0]
    )
    if extstats_count != 0:
        raise PlannerSandboxValidationError(
            "sandbox contains physical extended-statistics definitions"
        )
    return (
        metadata,
        int(target[0]),
        {
            "target_sample_row_count": target_count,
            "frozen_sample_row_count": frozen_count,
            "reltuples": float(target[2]),
            "ordinary_stats_fingerprint": fingerprint,
            "physical_extstats_count": extstats_count,
            "autovacuum_disabled": True,
        },
    )


def _lock(connection: Any) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_ADVISORY_LOCK_KEY,)
    )


def prepare_postgres_planner_sandbox(
    dsn: str,
    snapshot: AdvisorSnapshot,
    candidate_universe: CandidateUniverse,
    native_repository: NativeStatsRepository,
    *,
    batch_size: int = 256,
) -> PreparedPostgresPlannerSandbox:
    if len(snapshot.schemas) != 1 or len(snapshot.populations) != 1:
        raise PlannerSandboxError("planner sandbox v1 requires exactly one snapshot relation")
    if snapshot.semantic_digest is None:
        raise PlannerSandboxError("planner sandbox requires a sealed snapshot")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    compatibility = validate_native_stats_repository_compatibility(
        native_repository, snapshot, candidate_universe
    )
    schema = snapshot.schemas[0]
    source_schema = _require_sandbox_schema(schema)
    if schema.relation_name.catalog is None:
        raise PlannerSandboxError("planner sandbox v1 requires a snapshot database/catalog name")
    table = snapshot.samples[schema.relation_id]
    if not isinstance(table, pa.Table) or table.num_rows < 1:
        raise PlannerSandboxError("planner sandbox sample is missing or empty")
    psycopg = _psycopg()
    connection = None
    try:
        connection = psycopg.connect(
            dsn, application_name="extstats-advisor-sandbox-prepare", autocommit=True
        )
        capabilities = probe_patched_postgres(connection)
        if capabilities.backend_contract != PATCHED_BACKEND_CONTRACT:
            raise PlannerSandboxError("connected PostgreSQL backend contract is unsupported")
        if native_repository.backend_contract != capabilities.backend_contract:
            raise PlannerSandboxError("native repository backend contract mismatch")
        if native_repository.server_version_num != capabilities.server_version_num:
            raise PlannerSandboxError("native repository server version mismatch")
        if native_repository.server_version != capabilities.server_version:
            raise PlannerSandboxError("native repository server version string mismatch")
        database = str(connection.execute("SELECT current_database()").fetchone()[0])
        if database != schema.relation_name.catalog:
            raise PlannerSandboxError(
                f"sandbox database {database!r} does not match snapshot catalog {schema.relation_name.catalog!r}"
            )
        sql = psycopg.sql
        connection.execute("BEGIN")
        _lock(connection)
        _ensure_relation_absent(connection, schema.relation_name)
        user = _current_user(connection)
        source_schema_created = _ensure_source_schema(connection, source_schema, sql)
        _ensure_relation_absent(connection, schema.relation_name)
        internal_schema_created = _ensure_internal_schema(connection, sql, user)
        frozen = _frozen_relation(snapshot)
        _ensure_relation_absent(connection, frozen)
        create_relation(
            connection,
            schema.relation_name.name,
            schema,
            sql,
            schema_name=source_schema,
        )
        create_relation(
            connection,
            frozen.name,
            schema,
            sql,
            schema_name=INTERNAL_SCHEMA,
        )
        target_sql = _qualified(schema.relation_name, sql)
        frozen_sql = _qualified(frozen, sql)
        copy_arrow_table_to_relation(
            connection, target_sql, schema, table, sql, batch_size=batch_size
        )
        copy_arrow_table_to_relation(
            connection, frozen_sql, schema, table, sql, batch_size=batch_size
        )
        _set_autovacuum_disabled(connection, target_sql, sql)
        _set_autovacuum_disabled(connection, frozen_sql, sql)
        connection.execute(
            "SELECT set_config('default_statistics_target', %s, true)",
            (str(native_repository.statistics_target),),
        )
        _set_replay_config(connection, frozen, float(snapshot.populations[0].row_count))
        connection.execute(sql.SQL("ANALYZE {}").format(target_sql))
        target = _relation_lookup(connection, schema.relation_name)
        if target is None:
            raise PlannerSandboxError("managed target disappeared after ANALYZE")
        reltuples = float(target[2])
        population = float(snapshot.populations[0].row_count)
        if not math.isclose(reltuples, population, rel_tol=1e-6, abs_tol=1.0):
            raise PlannerSandboxError(
                f"sandbox ANALYZE did not establish population {population}, observed {reltuples}"
            )
        ordinary = ordinary_stats_fingerprint(connection, int(target[0]))
        if ordinary != native_repository.ordinary_stats_fingerprint:
            raise PlannerSandboxError(
                "sandbox ordinary statistics do not match native repository fingerprint"
            )
        if (
            int(
                connection.execute(
                    "SELECT count(*) FROM pg_catalog.pg_statistic_ext WHERE stxrelid = %s",
                    (int(target[0]),),
                ).fetchone()[0]
            )
            != 0
        ):
            raise PlannerSandboxError("sandbox preparation created physical extended statistics")
        metadata_row = connection.execute(
            sql.SQL(
                """
                INSERT INTO {} (
                    sandbox_contract, source_snapshot_semantic_digest,
                    candidate_universe_semantic_digest,
                    native_stats_repository_semantic_digest, backend_contract,
                    server_version, server_version_num, statistics_target,
                    relation_id, relation_catalog, relation_schema, relation_name,
                    frozen_relation_schema, frozen_relation_name,
                    managed_schema_created, internal_schema_created, sample_row_count,
                    population_row_count, population_quality,
                    ordinary_stats_fingerprint, repository_candidate_count,
                    repository_present_count, repository_absent_count
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) RETURNING prepared_at
                """
            ).format(_metadata_relation(sql)),
            (
                POSTGRES_PLANNER_SANDBOX_CONTRACT,
                snapshot.semantic_digest,
                candidate_universe.semantic_digest,
                native_repository.semantic_digest,
                capabilities.backend_contract,
                capabilities.server_version,
                capabilities.server_version_num,
                native_repository.statistics_target,
                schema.relation_id,
                schema.relation_name.catalog,
                source_schema,
                schema.relation_name.name,
                INTERNAL_SCHEMA,
                frozen.name,
                source_schema_created,
                internal_schema_created,
                table.num_rows,
                population,
                snapshot.populations[0].row_count_quality,
                ordinary,
                compatibility["candidate_count"],
                compatibility["present_count"],
                compatibility["absent_count"],
            ),
        ).fetchone()
        if metadata_row is None:
            raise PlannerSandboxError("sandbox metadata insert did not return preparation time")
        metadata = PostgresSandboxMetadata(
            POSTGRES_PLANNER_SANDBOX_CONTRACT,
            snapshot.semantic_digest,
            candidate_universe.semantic_digest,
            native_repository.semantic_digest,
            capabilities.backend_contract,
            capabilities.server_version,
            capabilities.server_version_num,
            native_repository.statistics_target,
            schema.relation_id,
            schema.relation_name,
            frozen,
            source_schema_created,
            internal_schema_created,
            table.num_rows,
            population,
            snapshot.populations[0].row_count_quality,
            ordinary,
            compatibility["candidate_count"],
            compatibility["present_count"],
            compatibility["absent_count"],
            str(metadata_row[0]),
        )
        connection.commit()
        return PreparedPostgresPlannerSandbox(metadata, int(target[0]))
    except (PlannerSandboxError, NativeStatsRepositoryValidationError):
        if connection is not None:
            connection.rollback()
        raise
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        raise PlannerSandboxError("could not prepare PostgreSQL planner sandbox") from exc
    finally:
        if connection is not None:
            connection.close()


def verify_postgres_planner_sandbox(
    dsn: str,
    snapshot: AdvisorSnapshot,
    candidate_universe: CandidateUniverse,
    native_repository: NativeStatsRepository,
) -> dict[str, Any]:
    if len(snapshot.schemas) != 1 or len(snapshot.populations) != 1:
        raise PlannerSandboxValidationError("planner sandbox v1 requires one relation")
    compatibility = validate_native_stats_repository_compatibility(
        native_repository, snapshot, candidate_universe
    )
    psycopg = _psycopg()
    connection = None
    try:
        connection = psycopg.connect(
            dsn, application_name="extstats-advisor-sandbox-verify", autocommit=True
        )
        capabilities = probe_patched_postgres(connection)
        database = str(connection.execute("SELECT current_database()").fetchone()[0])
        catalog = snapshot.schemas[0].relation_name.catalog
        if catalog is None or database != catalog:
            raise PlannerSandboxValidationError("sandbox database does not match snapshot catalog")
        metadata, target_oid, checks = _verify_live(
            connection,
            snapshot,
            candidate_universe,
            native_repository,
            capabilities,
            psycopg.sql,
        )
        return {
            "sandbox_contract": metadata.sandbox_contract,
            "metadata": metadata.to_dict(),
            "target_relation_oid": target_oid,
            "repository": compatibility,
            **checks,
        }
    except (PlannerSandboxError, NativeStatsRepositoryValidationError):
        raise
    except Exception as exc:
        raise PlannerSandboxValidationError("could not verify PostgreSQL planner sandbox") from exc
    finally:
        if connection is not None:
            connection.close()


def destroy_postgres_planner_sandbox(dsn: str) -> dict[str, Any]:
    psycopg = _psycopg()
    connection = None
    try:
        connection = psycopg.connect(
            dsn, application_name="extstats-advisor-sandbox-destroy", autocommit=True
        )
        probe_patched_postgres(connection)
        sql = psycopg.sql
        connection.execute("BEGIN")
        _lock(connection)
        metadata = _read_metadata(connection, sql)
        user = _current_user(connection)
        target = _relation_lookup(connection, metadata.relation_name)
        frozen = _relation_lookup(connection, metadata.frozen_relation_name)
        metadata_table = _relation_lookup(
            connection, RelationName(METADATA_TABLE, schema=INTERNAL_SCHEMA)
        )
        if target is None or frozen is None or metadata_table is None:
            raise PlannerSandboxError("sandbox ownership proof is incomplete; refusing destroy")
        if (
            target[1] != "r"
            or frozen[1] != "r"
            or metadata_table[1] != "r"
            or target[5] != user
            or frozen[5] != user
            or metadata_table[5] != user
        ):
            raise PlannerSandboxError("sandbox objects are not owned tables of the advisor role")
        connection.execute(
            sql.SQL("DROP TABLE {}").format(_qualified(metadata.frozen_relation_name, sql))
        )
        connection.execute(sql.SQL("DROP TABLE {}").format(_qualified(metadata.relation_name, sql)))
        connection.execute(sql.SQL("DROP TABLE {}").format(_metadata_relation(sql)))
        internal_objects = connection.execute(
            """
            SELECT count(*) FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = %s
            """,
            (INTERNAL_SCHEMA,),
        ).fetchone()[0]
        if internal_objects:
            raise PlannerSandboxError("advisor internal schema contains unrecognized objects")
        internal_schema = _schema_exists(connection, INTERNAL_SCHEMA)
        if internal_schema is not None and internal_schema[1] != user:
            raise PlannerSandboxError("advisor internal schema ownership changed")
        if metadata.internal_schema_created and internal_schema is not None:
            connection.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(INTERNAL_SCHEMA)))
        if metadata.managed_schema_created:
            source_schema = metadata.relation_name.schema
            if source_schema is None:
                raise PlannerSandboxError("managed source schema identity is missing")
            objects = connection.execute(
                """
                SELECT count(*) FROM pg_catalog.pg_class AS c
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s
                """,
                (source_schema,),
            ).fetchone()[0]
            if objects:
                raise PlannerSandboxError(
                    "advisor-created source schema contains unrelated objects"
                )
            source_schema_info = _schema_exists(connection, source_schema)
            if source_schema_info is None or source_schema_info[1] != user:
                raise PlannerSandboxError("advisor-created source schema ownership changed")
            connection.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(source_schema)))
        connection.commit()
        return {
            "destroyed": True,
            "sandbox_contract": metadata.sandbox_contract,
            "relation_name": metadata.relation_name.to_dict(),
            "frozen_relation_name": metadata.frozen_relation_name.to_dict(),
        }
    except PlannerSandboxError:
        if connection is not None:
            connection.rollback()
        raise
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        raise PlannerSandboxError("could not destroy PostgreSQL planner sandbox") from exc
    finally:
        if connection is not None:
            connection.close()
