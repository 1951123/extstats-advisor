"""First production PostgreSQL native-statistics materializer."""

from __future__ import annotations

import math
import time
from typing import Any

import pyarrow as pa

from extstats_advisor.candidates.universe import CandidateUniverse
from extstats_advisor.canonical import digest_bytes
from extstats_advisor.dbms.postgres.ordinary_stats import ordinary_stats_fingerprint
from extstats_advisor.dbms.postgres.patch import PatchCapabilities, probe_patched_postgres
from extstats_advisor.dbms.postgres.staging import (
    copy_arrow_table,
    create_scratch_relation,
    scratch_relation_sql,
)
from extstats_advisor.errors import NativeStatsMaterializationError
from extstats_advisor.native_stats.model import (
    ABSENT_NATIVE,
    MATERIALIZATION_METHOD,
    PRESENT,
    NativeStatsCandidate,
    NativeStatsMaterialization,
)
from extstats_advisor.snapshot.model import AdvisorSnapshot, RelationSchema

MCV_SERIALIZATION = "postgresql.pg_mcv_list_send-v1"
DEPENDENCIES_SERIALIZATION = "postgresql.pg_dependencies_send-v1"


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise NativeStatsMaterializationError(
            "native PostgreSQL materialization requires extstats-advisor[postgres]"
        ) from exc
    return psycopg


def _check_inputs(
    snapshot: AdvisorSnapshot, universe: CandidateUniverse
) -> tuple[RelationSchema, pa.Table, float]:
    if len(snapshot.schemas) != 1 or len(snapshot.populations) != 1:
        raise NativeStatsMaterializationError("native materialization v1 requires one relation")
    if snapshot.semantic_digest is None:
        raise NativeStatsMaterializationError("native materialization requires a sealed snapshot")
    schema = snapshot.schemas[0]
    population = snapshot.populations[0]
    if (
        population.relation_id != schema.relation_id
        or universe.source_snapshot_semantic_digest != snapshot.semantic_digest
    ):
        raise NativeStatsMaterializationError(
            "snapshot, population, and candidate universe do not match"
        )
    table = snapshot.samples.get(schema.relation_id)
    if not isinstance(table, pa.Table) or table.num_rows < 1:
        raise NativeStatsMaterializationError("snapshot sample is missing or empty")
    if table.column_names != [column.name for column in schema.columns]:
        raise NativeStatsMaterializationError("snapshot Arrow columns do not match relation schema")
    return schema, table, float(population.row_count)


def _relation_oid(connection: Any, name: str) -> int:
    row = connection.execute("SELECT to_regclass(%s)::oid", (f"pg_temp.{name}",)).fetchone()
    if row is None or row[0] is None:
        raise NativeStatsMaterializationError(f"scratch relation was not created: {name}")
    return int(row[0])


def _statistic_name(candidate_id: str) -> str:
    return f"extstats_adv_stat_{candidate_id.removeprefix('cand_')}"


def _create_statistics(
    connection: Any,
    target_name: str,
    schema: RelationSchema,
    universe: CandidateUniverse,
    sql: Any,
    statistics_target: int,
) -> dict[str, str]:
    by_ordinal = {column.ordinal: column for column in schema.columns}
    names: dict[str, str] = {}
    for candidate in universe.candidates:
        if candidate.relation_id != schema.relation_id:
            raise NativeStatsMaterializationError("candidate references another relation")
        try:
            columns = [by_ordinal[ordinal] for ordinal in candidate.column_ordinals]
        except KeyError as exc:
            raise NativeStatsMaterializationError("candidate references an unknown column") from exc
        if tuple(column.name for column in columns) != candidate.column_names:
            raise NativeStatsMaterializationError("candidate column names do not match snapshot")
        statistic_name = _statistic_name(candidate.candidate_id)
        kind = "mcv" if candidate.kind == "postgresql.mcv" else "dependencies"
        column_sql = sql.SQL(", ").join(sql.Identifier(column.name) for column in columns)
        connection.execute(
            sql.SQL("CREATE STATISTICS {} ({}) ON {} FROM {}").format(
                sql.Identifier(statistic_name),
                sql.SQL(kind),
                column_sql,
                scratch_relation_sql(target_name, sql),
            )
        )
        connection.execute(
            sql.SQL("ALTER STATISTICS {} SET STATISTICS {}").format(
                sql.Identifier(statistic_name), sql.SQL(str(statistics_target))
            )
        )
        names[candidate.candidate_id] = statistic_name
    return names


def _extract_native_payload(
    connection: Any,
    relation_oid: int,
    statistic_name: str,
    candidate: Any,
) -> tuple[bytes | None, str | None]:
    if candidate.kind == "postgresql.mcv":
        expression = "pg_catalog.pg_mcv_list_send(d.stxdmcv)"
        serialization = MCV_SERIALIZATION
    else:
        expression = "pg_catalog.pg_dependencies_send(d.stxddependencies)"
        serialization = DEPENDENCIES_SERIALIZATION
    row = connection.execute(
        f"""
        SELECT e.stxkeys::text, e.stxkind::text, {expression}
          FROM pg_catalog.pg_statistic_ext AS e
          LEFT JOIN pg_catalog.pg_statistic_ext_data AS d ON d.stxoid = e.oid
         WHERE e.stxrelid = %s AND e.stxname = %s
        """,
        (relation_oid, statistic_name),
    ).fetchone()
    if row is None:
        raise NativeStatsMaterializationError(
            f"statistics object was not found: {candidate.candidate_id}"
        )
    expected_keys = " ".join(str(value) for value in candidate.column_ordinals)
    if str(row[0]).strip() != expected_keys:
        raise NativeStatsMaterializationError(
            f"statistics column keys mismatch: {candidate.candidate_id}"
        )
    payload = row[2]
    if payload is None:
        return None, None
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise NativeStatsMaterializationError(
            f"native statistics payload is not bytea: {candidate.candidate_id}"
        )
    return bytes(payload), serialization


def _candidate_results(
    connection: Any,
    relation_oid: int,
    universe: CandidateUniverse,
    names: dict[str, str],
) -> tuple[tuple[NativeStatsCandidate, ...], dict[str, bytes]]:
    records = []
    payloads: dict[str, bytes] = {}
    for candidate in universe.candidates:
        payload, serialization = _extract_native_payload(
            connection, relation_oid, names[candidate.candidate_id], candidate
        )
        if payload is None:
            records.append(
                NativeStatsCandidate(
                    candidate.candidate_id,
                    candidate.relation_id,
                    candidate.kind,
                    candidate.column_ordinals,
                    candidate.column_names,
                    ABSENT_NATIVE,
                    None,
                    0,
                    None,
                    None,
                )
            )
            payloads[candidate.candidate_id] = b""
            continue
        payloads[candidate.candidate_id] = payload
        records.append(
            NativeStatsCandidate(
                candidate.candidate_id,
                candidate.relation_id,
                candidate.kind,
                candidate.column_ordinals,
                candidate.column_names,
                PRESENT,
                serialization,
                len(payload),
                digest_bytes(payload),
                f"payloads/{candidate.candidate_id}.bin",
            )
        )
    return tuple(records), payloads


def materialize_native_stats(
    dsn: str,
    snapshot: AdvisorSnapshot,
    universe: CandidateUniverse,
    *,
    statistics_target: int = 100,
    batch_size: int = 256,
) -> NativeStatsMaterialization:
    """Materialize exactly one fixed-sample ANALYZE in one rollback-only session."""

    if not isinstance(dsn, str) or not dsn.strip():
        raise NativeStatsMaterializationError("patched PostgreSQL DSN is required")
    if not 1 <= statistics_target <= 10_000 or batch_size < 1:
        raise ValueError("statistics_target must be 1..10000 and batch_size must be positive")
    schema, table, population = _check_inputs(snapshot, universe)
    psycopg = _psycopg()
    started = time.monotonic()
    connection = None
    analyze_count = 0
    target_name = f"extstats_adv_target_{snapshot.semantic_digest[:16]}"
    frozen_name = f"extstats_adv_frozen_{snapshot.semantic_digest[:16]}"
    try:
        connection = psycopg.connect(
            dsn,
            application_name="extstats-advisor-native-stats",
            autocommit=True,
        )
        capabilities: PatchCapabilities = probe_patched_postgres(connection)
        sql = psycopg.sql
        connection.execute("BEGIN ISOLATION LEVEL READ WRITE")
        create_scratch_relation(connection, target_name, schema, sql)
        create_scratch_relation(connection, frozen_name, schema, sql)
        copy_arrow_table(connection, target_name, schema, table, sql, batch_size=batch_size)
        copy_arrow_table(connection, frozen_name, schema, table, sql, batch_size=batch_size)
        connection.execute(
            "SELECT set_config('pg_extstats.frozen_sample_mode', %s, true)",
            ("replay",),
        )
        connection.execute(
            "SELECT set_config('pg_extstats.frozen_sample_relation', %s, true)",
            (f"pg_temp.{frozen_name}",),
        )
        connection.execute(
            "SELECT set_config('pg_extstats.frozen_totalrows', %s, true)",
            (str(population),),
        )
        connection.execute(
            "SELECT set_config('default_statistics_target', %s, true)",
            (str(statistics_target),),
        )
        connection.execute("SELECT set_config('timezone', %s, true)", ("UTC",))
        connection.execute("SELECT set_config('DateStyle', %s, true)", ("ISO, YMD",))
        names = _create_statistics(
            connection, target_name, schema, universe, sql, statistics_target
        )
        connection.execute(sql.SQL("ANALYZE {}").format(scratch_relation_sql(target_name, sql)))
        analyze_count += 1
        target_oid = _relation_oid(connection, target_name)
        reltuples = float(
            connection.execute(
                "SELECT reltuples::double precision FROM pg_catalog.pg_class WHERE oid = %s",
                (target_oid,),
            ).fetchone()[0]
        )
        if not math.isclose(
            reltuples,
            population,
            rel_tol=1e-6,
            abs_tol=max(1.0, abs(population) * 1e-9),
        ):
            raise NativeStatsMaterializationError(
                "patched ANALYZE did not preserve frozen population: "
                f"expected {population}, observed {reltuples}"
            )
        ordinary_fingerprint = ordinary_stats_fingerprint(connection, target_oid)
        candidates, payloads = _candidate_results(connection, target_oid, universe, names)
        return NativeStatsMaterialization(
            snapshot.semantic_digest,
            universe.semantic_digest,
            capabilities.backend_contract,
            capabilities.server_version,
            capabilities.server_version_num,
            capabilities.reference_source_commit,
            statistics_target,
            table.num_rows,
            population,
            reltuples,
            ordinary_fingerprint,
            candidates,
            payloads,
            analyze_count,
            {
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "reference_source_commit": capabilities.reference_source_commit,
                "fixed_sample_method": MATERIALIZATION_METHOD,
            },
        )
    except NativeStatsMaterializationError:
        raise
    except Exception as exc:
        raise NativeStatsMaterializationError(
            "patched PostgreSQL native-statistics materialization failed"
        ) from exc
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()


materialize_postgresql_native_stats = materialize_native_stats
