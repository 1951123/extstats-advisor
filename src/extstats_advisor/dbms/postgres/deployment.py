"""Transactional PostgreSQL deployment of a validated Recommendation."""

from __future__ import annotations

import re
from contextlib import suppress
from typing import Any

from extstats_advisor.dbms.postgres.recommendation import build_postgres_recommendation
from extstats_advisor.deployment.model import DEPLOYMENT_POLICY, DeployedObject, DeploymentResult
from extstats_advisor.errors import (
    DeploymentCommitOutcomeUnknownError,
    DeploymentCommittedButUnverifiedError,
    DeploymentMutationError,
    DeploymentValidationError,
)
from extstats_advisor.native_stats.repository import validate_native_stats_repository_compatibility
from extstats_advisor.recommendation.model import (
    AnalyzeRelationAction,
    CreateStatisticsAction,
    Recommendation,
    SetStatisticsTargetAction,
)

DEFAULT_LOCK_TIMEOUT_MS = 5_000
DEFAULT_STATEMENT_TIMEOUT_MS = 120_000
_DDL_KINDS = {"postgresql.mcv": "mcv", "postgresql.dependencies": "dependencies"}
_STX_KIND_CODES = {"postgresql.mcv": "m", "postgresql.dependencies": "f"}


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise DeploymentMutationError(
            "PostgreSQL deployment requires extstats-advisor[postgres]"
        ) from exc
    return psycopg


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DeploymentValidationError(f"{label} must be a positive integer")
    return value


def _validate_source_chain(
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    ground_truth: Any,
    singleton_profile: Any,
    optimization_plan: Any,
    search_result: Any,
    recommendation: Recommendation,
) -> Recommendation:
    if snapshot.semantic_digest is None:
        raise DeploymentValidationError("deployment requires a sealed snapshot")
    try:
        ground_truth_digest = getattr(ground_truth, "semantic_digest", None)
        if ground_truth_digest is None:
            ground_truth_digest = ground_truth.computed_semantic_digest
        if recommendation.ground_truth_semantic_digest != ground_truth_digest:
            raise DeploymentValidationError(
                "deployment ground-truth digest does not match Recommendation"
            )
        if native_repository.server_version != recommendation.dbms_source_version:
            raise DeploymentValidationError(
                "native repository server version does not match Recommendation"
            )
        validate_native_stats_repository_compatibility(
            native_repository, snapshot, candidate_universe
        )
        expected = build_postgres_recommendation(
            snapshot,
            candidate_universe,
            native_repository,
            singleton_profile,
            optimization_plan,
            search_result,
        )
    except Exception as exc:
        if isinstance(exc, DeploymentValidationError):
            raise
        raise DeploymentValidationError("deployment source chain is invalid") from exc
    if recommendation.computed_semantic_digest != expected.computed_semantic_digest:
        raise DeploymentValidationError(
            "Recommendation does not recompute from the supplied source chain"
        )
    if recommendation.decision not in {"propose-change", "no-change"}:
        raise DeploymentValidationError("unsupported Recommendation decision")
    return recommendation


def _connect(dsn: str, *, application_name: str) -> Any:
    if not isinstance(dsn, str) or not dsn.strip():
        raise DeploymentValidationError("production PostgreSQL DSN is required")
    psycopg = _psycopg()
    try:
        return psycopg.connect(
            dsn,
            application_name=application_name,
            autocommit=True,
        )
    except psycopg.Error as exc:
        raise DeploymentMutationError("could not connect to production PostgreSQL") from exc


def _set_local_safety_parameters(
    connection: Any, lock_timeout_ms: int, statement_timeout_ms: int
) -> None:
    connection.execute(
        "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
        (f"{lock_timeout_ms}ms",),
    )
    connection.execute(
        "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
        (f"{statement_timeout_ms}ms",),
    )


def _relation_sql(sql: Any, schema: str, relation: str) -> Any:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(relation))


def _lock_relation(connection: Any, sql: Any, schema: str, relation: str) -> None:
    connection.execute(
        sql.SQL("LOCK TABLE {} IN SHARE UPDATE EXCLUSIVE MODE").format(
            _relation_sql(sql, schema, relation)
        )
    )


def _parse_keys(value: Any) -> tuple[int, ...]:
    return tuple(int(item) for item in re.findall(r"\d+", str(value)))


def _parse_kinds(value: Any) -> set[str]:
    return set(re.findall(r"[mfnd]", str(value)))


def _identity(connection: Any) -> dict[str, Any]:
    row = connection.execute(
        "SELECT current_database(), current_user, current_setting('server_version'), "
        "current_setting('server_version_num')::integer"
    ).fetchone()
    if row is None:
        raise DeploymentValidationError("PostgreSQL identity query returned no row")
    return {
        "database": str(row[0]),
        "role": str(row[1]),
        "server_version": str(row[2]),
        "server_version_num": int(row[3]),
    }


def _snapshot_relation(snapshot: Any, recommendation: Recommendation) -> Any:
    relation = snapshot.schema_by_id.get(recommendation.target_relation.relation_id)
    if relation is None:
        raise DeploymentValidationError("Recommendation target is absent from AdvisorSnapshot")
    if (
        relation.relation_name.schema != recommendation.target_relation.schema
        or relation.relation_name.name != recommendation.target_relation.name
    ):
        raise DeploymentValidationError("Recommendation target does not match AdvisorSnapshot")
    return relation


def _load_live_relation(connection: Any, recommendation: Recommendation) -> dict[str, Any]:
    target = recommendation.target_relation
    row = connection.execute(
        "SELECT c.oid::bigint, c.relkind, n.nspname, c.relname, "
        "pg_get_userbyid(c.relowner), pg_catalog.pg_has_role(current_user, c.relowner, 'USAGE'), "
        "pg_catalog.has_schema_privilege(current_user, n.oid, 'CREATE') "
        "FROM pg_catalog.pg_class AS c JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        (target.schema, target.name),
    ).fetchone()
    if row is None:
        raise DeploymentValidationError("target relation does not exist")
    result = {
        "relation_oid": int(row[0]),
        "relkind": str(row[1]),
        "schema": str(row[2]),
        "relation": str(row[3]),
        "owner": str(row[4]),
        "can_act_as_owner": bool(row[5]),
        "schema_create": bool(row[6]),
    }
    if result["relkind"] != "r":
        raise DeploymentValidationError("target relation is not an ordinary stored table")
    if not result["can_act_as_owner"]:
        raise DeploymentValidationError("current role cannot act as target relation owner")
    if not result["schema_create"]:
        raise DeploymentValidationError("current role lacks CREATE privilege in target schema")
    return result


def _live_columns(connection: Any, relation_oid: int) -> dict[int, dict[str, Any]]:
    rows = connection.execute(
        "SELECT a.attnum, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), "
        "a.attisdropped, "
        "CASE WHEN a.attcollation = 0 THEN NULL "
        "     ELSE quote_ident(cn.nspname) || '.' || quote_ident(co.collname) END "
        "FROM pg_catalog.pg_attribute AS a "
        "LEFT JOIN pg_catalog.pg_collation AS co ON co.oid = a.attcollation "
        "LEFT JOIN pg_catalog.pg_namespace AS cn ON cn.oid = co.collnamespace "
        "WHERE a.attrelid = %s AND a.attnum > 0 ORDER BY a.attnum",
        (relation_oid,),
    ).fetchall()
    return {
        int(row[0]): {
            "name": str(row[1]),
            "native_type": str(row[2]),
            "dropped": bool(row[3]),
            "native_collation": row[4] if row[4] is None else str(row[4]),
        }
        for row in rows
    }


def _validate_selected_columns(
    snapshot: Any,
    recommendation: Recommendation,
    relation: Any,
    live_columns: dict[int, dict[str, Any]],
) -> None:
    snapshot_columns = {column.ordinal: column for column in relation.columns}
    for selected in recommendation.selected_candidates:
        if selected.relation_id != relation.relation_id:
            raise DeploymentValidationError("selected candidate relation differs from target")
        for ordinal, name in zip(selected.column_ordinals, selected.column_names, strict=True):
            expected = snapshot_columns.get(ordinal)
            observed = live_columns.get(ordinal)
            if expected is None or observed is None or observed["dropped"]:
                raise DeploymentValidationError(f"selected column ordinal drift: {ordinal}")
            if expected.name != name or observed["name"] != name:
                raise DeploymentValidationError(f"selected column name drift: {name}")
            if expected.native_type is None or observed["native_type"] != expected.native_type:
                raise DeploymentValidationError(f"selected column type drift: {name}")
            if observed["native_collation"] != expected.native_collation:
                raise DeploymentValidationError(f"selected column collation drift: {name}")


def _existing_external_statistics(connection: Any, relation_oid: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT e.oid::bigint, n.nspname, e.stxname, e.stxkind::text, e.stxkeys::text, e.stxstattarget "
        "FROM pg_catalog.pg_statistic_ext AS e "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = e.stxnamespace "
        "WHERE e.stxrelid = %s ORDER BY e.oid",
        (relation_oid,),
    ).fetchall()
    return [
        {
            "oid": int(row[0]),
            "schema": str(row[1]),
            "name": str(row[2]),
            "kinds": sorted(_parse_kinds(row[3])),
            "keys": list(_parse_keys(row[4])),
            "statistics_target": int(row[5]),
        }
        for row in rows
    ]


def _name_collisions(connection: Any, recommendation: Recommendation) -> list[str]:
    collisions = []
    for selected in recommendation.selected_candidates:
        row = connection.execute(
            "SELECT 1 FROM pg_catalog.pg_statistic_ext AS e "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = e.stxnamespace "
            "WHERE n.nspname = %s AND e.stxname = %s",
            (selected.statistics_object.schema, selected.statistics_object.name),
        ).fetchone()
        if row is not None:
            collisions.append(
                f"{selected.statistics_object.schema}.{selected.statistics_object.name}"
            )
    return collisions


def _preflight_locked(
    connection: Any,
    snapshot: Any,
    recommendation: Recommendation,
    *,
    expected_server_version_num: int,
    sql: Any,
    lock_timeout_ms: int,
    statement_timeout_ms: int,
) -> dict[str, Any]:
    _set_local_safety_parameters(connection, lock_timeout_ms, statement_timeout_ms)
    _lock_relation(
        connection,
        sql,
        recommendation.target_relation.schema,
        recommendation.target_relation.name,
    )
    identity = _identity(connection)
    target = recommendation.target_relation
    if target.catalog is not None and identity["database"] != target.catalog:
        raise DeploymentValidationError("current database does not match Recommendation catalog")
    if identity["server_version"] != recommendation.dbms_source_version:
        raise DeploymentValidationError("PostgreSQL server version drifted from Recommendation")
    if identity["server_version_num"] != expected_server_version_num:
        raise DeploymentValidationError("PostgreSQL server version number drifted from source")
    live = _load_live_relation(connection, recommendation)
    relation = _snapshot_relation(snapshot, recommendation)
    _validate_selected_columns(
        snapshot, recommendation, relation, _live_columns(connection, live["relation_oid"])
    )
    external = _existing_external_statistics(connection, live["relation_oid"])
    collisions = _name_collisions(connection, recommendation)
    if collisions:
        raise DeploymentValidationError(
            "deterministic Recommendation statistics name collision: " + ", ".join(collisions)
        )
    return {
        "status": "ready",
        "deployment_policy": DEPLOYMENT_POLICY,
        "database": identity["database"],
        "role": identity["role"],
        "server_version": identity["server_version"],
        "server_version_num": identity["server_version_num"],
        "relation_oid": live["relation_oid"],
        "relation_kind": live["relkind"],
        "relation_owner": live["owner"],
        "relation_owner_authority": live["can_act_as_owner"],
        "schema_create": live["schema_create"],
        "existing_external_statistics_count": len(external),
        "existing_external_statistics_names": [
            f"{item['schema']}.{item['name']}" for item in external
        ],
        "deterministic_name_collisions": [],
        "recommended_object_count": len(recommendation.selected_candidates),
    }


def _read_managed_objects(
    connection: Any, recommendation: Recommendation, relation_oid: int, *, require_payload: bool
) -> tuple[DeployedObject, ...]:
    names = [item.statistics_object.name for item in recommendation.selected_candidates]
    if not names:
        return ()
    rows = connection.execute(
        "SELECT e.oid::bigint, e.stxrelid::bigint, n.nspname, e.stxname, e.stxkind::text, "
        "e.stxkeys::text, e.stxstattarget, "
        "coalesce(bool_or(d.stxdmcv IS NOT NULL), false), "
        "coalesce(bool_or(d.stxddependencies IS NOT NULL), false) "
        "FROM pg_catalog.pg_statistic_ext AS e "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = e.stxnamespace "
        "LEFT JOIN pg_catalog.pg_statistic_ext_data AS d ON d.stxoid = e.oid "
        "WHERE n.nspname = %s AND e.stxname = ANY(%s) "
        "GROUP BY e.oid, e.stxrelid, n.nspname, e.stxname, e.stxkind, e.stxkeys, e.stxstattarget "
        "ORDER BY e.oid",
        (recommendation.target_relation.schema, names),
    ).fetchall()
    by_name = {str(row[3]): row for row in rows}
    result: list[DeployedObject] = []
    for position, selected in enumerate(recommendation.selected_candidates, start=1):
        row = by_name.get(selected.statistics_object.name)
        if row is None:
            raise DeploymentMutationError(
                f"Recommendation statistics object is missing: {selected.statistics_object.name}"
            )
        expected_kind = _STX_KIND_CODES[selected.kind]
        if (
            int(row[1]) != relation_oid
            or str(row[2]) != selected.statistics_object.schema
            or expected_kind not in _parse_kinds(row[4])
            or tuple(_parse_keys(row[5])) != selected.column_ordinals
            or int(row[6]) != selected.statistics_target
        ):
            raise DeploymentMutationError(
                f"Recommendation statistics definition mismatch: {selected.candidate_id}"
            )
        payload = bool(row[7] if expected_kind == "m" else row[8])
        if require_payload and not payload:
            raise DeploymentMutationError(
                f"expected native statistics payload is missing: {selected.candidate_id}"
            )
        result.append(
            DeployedObject(
                selected.candidate_id,
                selected.statistics_object.schema,
                selected.statistics_object.name,
                int(row[0]),
                selected.kind,
                selected.column_ordinals,
                selected.statistics_target,
                payload,
                position,
            )
        )
    oid_order = [item.candidate_id for item in sorted(result, key=lambda item: item.oid)]
    if oid_order != list(recommendation.deployment_ordered_candidate_ids):
        raise DeploymentMutationError("selected Recommendation objects do not preserve OID order")
    return tuple(result)


def _execute_actions(
    connection: Any, recommendation: Recommendation, sql: Any, *, include_analyze: bool
) -> None:
    for action in recommendation.ddl_plan:
        if isinstance(action, CreateStatisticsAction):
            kind = _DDL_KINDS[action.statistics_kind]
            columns = sql.SQL(", ").join(sql.Identifier(name) for name in action.column_names)
            connection.execute(
                sql.SQL("CREATE STATISTICS {}.{} ({}) ON {} FROM {}").format(
                    sql.Identifier(action.object_schema),
                    sql.Identifier(action.object_name),
                    sql.SQL(kind),
                    columns,
                    _relation_sql(
                        sql,
                        recommendation.target_relation.schema,
                        recommendation.target_relation.name,
                    ),
                )
            )
        elif isinstance(action, SetStatisticsTargetAction):
            connection.execute(
                sql.SQL("ALTER STATISTICS {}.{} SET STATISTICS {}").format(
                    sql.Identifier(action.object_schema),
                    sql.Identifier(action.object_name),
                    sql.SQL(str(action.statistics_target)),
                )
            )
        elif isinstance(action, AnalyzeRelationAction) and include_analyze:
            connection.execute(
                sql.SQL("ANALYZE {}").format(
                    _relation_sql(sql, action.relation.schema, action.relation.name)
                )
            )
        elif not isinstance(action, AnalyzeRelationAction):
            raise DeploymentValidationError("Recommendation contains an unsupported DDL action")


def _execute_analyze(connection: Any, recommendation: Recommendation, sql: Any) -> None:
    analyze_actions = [
        action for action in recommendation.ddl_plan if isinstance(action, AnalyzeRelationAction)
    ]
    if len(analyze_actions) != 1:
        raise DeploymentValidationError("Recommendation must contain exactly one ANALYZE action")
    action = analyze_actions[0]
    connection.execute(
        sql.SQL("ANALYZE {}").format(
            _relation_sql(sql, action.relation.schema, action.relation.name)
        )
    )


def _post_commit_verify(
    dsn: str,
    snapshot: Any,
    recommendation: Recommendation,
    expected_objects: tuple[DeployedObject, ...],
    relation_oid: int,
) -> None:
    connection = _connect(dsn, application_name="extstats-advisor-deployment-verify")
    try:
        connection.execute("BEGIN READ ONLY")
        identity = _identity(connection)
        if (
            recommendation.target_relation.catalog is not None
            and identity["database"] != recommendation.target_relation.catalog
        ):
            raise DeploymentValidationError("post-commit database identity mismatch")
        live = _load_live_relation(connection, recommendation)
        if live["relation_oid"] != relation_oid:
            raise DeploymentValidationError("post-commit relation OID changed")
        observed = _read_managed_objects(
            connection, recommendation, relation_oid, require_payload=True
        )
        if observed != expected_objects:
            raise DeploymentValidationError(
                "post-commit deployed objects differ from pre-commit state"
            )
        connection.rollback()
    except Exception:
        try:
            connection.rollback()
        finally:
            connection.close()
        raise
    connection.close()


def _no_change_result(
    recommendation: Recommendation,
    native_repository: Any,
) -> DeploymentResult:
    return DeploymentResult(
        recommendation.computed_semantic_digest,
        recommendation.source_snapshot_semantic_digest,
        recommendation.candidate_universe_semantic_digest,
        recommendation.native_stats_repository_semantic_digest,
        recommendation.singleton_profile_semantic_digest,
        recommendation.optimization_plan_semantic_digest,
        recommendation.search_result_semantic_digest,
        recommendation.deployment_contract,
        DEPLOYMENT_POLICY,
        recommendation.target_relation.catalog,
        recommendation.target_relation.schema,
        recommendation.target_relation.name,
        None,
        recommendation.dbms_source_version,
        int(native_repository.server_version_num),
        "no-change",
        (),
        (),
        {
            "status": "not-run",
            "reason": "no-change Recommendation; no production connection opened",
            "deployment_policy": DEPLOYMENT_POLICY,
        },
        "not-required",
        True,
        {},
        {},
    )


def _close_after_commit(connection: Any) -> None:
    # COMMIT already returned successfully; close errors do not change that fact.
    with suppress(Exception):
        connection.close()


def deploy_postgres_recommendation(
    dsn: str | None,
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    ground_truth: Any,
    singleton_profile: Any,
    optimization_plan: Any,
    search_result: Any,
    recommendation: Recommendation,
    *,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> DeploymentResult:
    """Apply one Recommendation transactionally and verify it after COMMIT."""

    lock_timeout_ms = _positive_int(lock_timeout_ms, "lock_timeout_ms")
    statement_timeout_ms = _positive_int(statement_timeout_ms, "statement_timeout_ms")
    _validate_source_chain(
        snapshot,
        candidate_universe,
        native_repository,
        ground_truth,
        singleton_profile,
        optimization_plan,
        search_result,
        recommendation,
    )
    if recommendation.decision == "no-change":
        return _no_change_result(recommendation, native_repository)
    if not dsn:
        raise DeploymentValidationError("production PostgreSQL DSN is required for propose-change")
    connection = _connect(dsn, application_name="extstats-advisor-deployment")
    sql = _psycopg().sql
    preflight_summary: dict[str, Any]
    relation_oid: int
    deployed_objects: tuple[DeployedObject, ...]
    try:
        connection.execute("BEGIN")
    except Exception as exc:
        try:
            connection.rollback()
        finally:
            connection.close()
        raise DeploymentMutationError(
            "could not begin transactional PostgreSQL deployment"
        ) from exc
    try:
        preflight_summary = _preflight_locked(
            connection,
            snapshot,
            recommendation,
            expected_server_version_num=native_repository.server_version_num,
            sql=sql,
            lock_timeout_ms=lock_timeout_ms,
            statement_timeout_ms=statement_timeout_ms,
        )
        relation_oid = int(preflight_summary["relation_oid"])
        _execute_actions(connection, recommendation, sql, include_analyze=False)
        _read_managed_objects(connection, recommendation, relation_oid, require_payload=False)
        _execute_analyze(connection, recommendation, sql)
        deployed_objects = _read_managed_objects(
            connection, recommendation, relation_oid, require_payload=True
        )
        if deployed_objects != _read_managed_objects(
            connection, recommendation, relation_oid, require_payload=True
        ):
            raise DeploymentMutationError("pre-commit deployed object verification was unstable")
    except Exception as exc:
        try:
            connection.rollback()
        finally:
            connection.close()
        if isinstance(exc, DeploymentValidationError):
            raise
        if isinstance(exc, DeploymentMutationError):
            raise
        raise DeploymentMutationError("transactional PostgreSQL deployment failed") from exc
    try:
        connection.commit()
    except Exception as exc:
        _close_after_commit(connection)
        raise DeploymentCommitOutcomeUnknownError(
            "COMMIT did not return successful confirmation; deployment outcome is unknown and "
            "manual/live verification is required"
        ) from exc
    _close_after_commit(connection)
    try:
        _post_commit_verify(dsn, snapshot, recommendation, deployed_objects, relation_oid)
    except Exception as exc:
        raise DeploymentCommittedButUnverifiedError(
            "deployment committed but post-commit verification failed; manual DBA inspection required"
        ) from exc
    return DeploymentResult(
        recommendation.computed_semantic_digest,
        recommendation.source_snapshot_semantic_digest,
        recommendation.candidate_universe_semantic_digest,
        recommendation.native_stats_repository_semantic_digest,
        recommendation.singleton_profile_semantic_digest,
        recommendation.optimization_plan_semantic_digest,
        recommendation.search_result_semantic_digest,
        recommendation.deployment_contract,
        DEPLOYMENT_POLICY,
        recommendation.target_relation.catalog,
        recommendation.target_relation.schema,
        recommendation.target_relation.name,
        relation_oid,
        preflight_summary["server_version"],
        preflight_summary["server_version_num"],
        recommendation.decision,
        recommendation.deployment_ordered_candidate_ids,
        deployed_objects,
        preflight_summary,
        "committed",
        True,
        {
            "lock_timeout_ms": lock_timeout_ms,
            "statement_timeout_ms": statement_timeout_ms,
        },
    )


def preflight_postgres_recommendation(
    dsn: str | None,
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    ground_truth: Any,
    singleton_profile: Any,
    optimization_plan: Any,
    search_result: Any,
    recommendation: Recommendation,
    *,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Run the same locked, read-only deployment-sensitive preflight without mutation."""

    lock_timeout_ms = _positive_int(lock_timeout_ms, "lock_timeout_ms")
    statement_timeout_ms = _positive_int(statement_timeout_ms, "statement_timeout_ms")
    _validate_source_chain(
        snapshot,
        candidate_universe,
        native_repository,
        ground_truth,
        singleton_profile,
        optimization_plan,
        search_result,
        recommendation,
    )
    if recommendation.decision == "no-change":
        return {
            "status": "ready",
            "decision": "no-change",
            "deployment_policy": DEPLOYMENT_POLICY,
            "reason": "no-change Recommendation; no production connection opened",
        }
    if not dsn:
        raise DeploymentValidationError("production PostgreSQL DSN is required for preflight")
    connection = _connect(dsn, application_name="extstats-advisor-deployment-preflight")
    sql = _psycopg().sql
    try:
        connection.execute("BEGIN")
        report = _preflight_locked(
            connection,
            snapshot,
            recommendation,
            expected_server_version_num=native_repository.server_version_num,
            sql=sql,
            lock_timeout_ms=lock_timeout_ms,
            statement_timeout_ms=statement_timeout_ms,
        )
        connection.rollback()
        return report
    except Exception:
        try:
            connection.rollback()
        finally:
            connection.close()
        raise
    finally:
        connection.close()


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_MS",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
    "deploy_postgres_recommendation",
    "preflight_postgres_recommendation",
]
