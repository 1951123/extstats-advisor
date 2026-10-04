"""Backend-local ordered hypothetical registration and native EXPLAIN estimates."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from extstats_advisor.dbms.postgres.native_stats import _psycopg
from extstats_advisor.dbms.postgres.patch import probe_patched_postgres
from extstats_advisor.dbms.postgres.sandbox import (
    verify_postgres_planner_sandbox,
)
from extstats_advisor.errors import PlannerQueryError, PlannerSandboxError
from extstats_advisor.native_stats.model import ABSENT_NATIVE, NativeStatsRepository
from extstats_advisor.snapshot.model import AdvisorSnapshot, RelationName

if TYPE_CHECKING:
    from extstats_advisor.candidates.universe import CandidateUniverse

POSTGRES_NATIVE_KIND_CODES = {
    "postgresql.mcv": "m",
    "postgresql.dependencies": "f",
}
_PARAMETER_RE = re.compile(r"\$[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class PostgresStatisticsConfiguration:
    """Ordered PostgreSQL design; order is planner-visible and never normalized."""

    ordered_candidate_ids: tuple[str, ...] = ()

    @property
    def membership(self) -> frozenset[str]:
        return frozenset(self.ordered_candidate_ids)

    def validate(self, candidate_ids: Iterable[str]) -> None:
        known = set(candidate_ids)
        if len(self.ordered_candidate_ids) != len(set(self.ordered_candidate_ids)):
            raise PlannerSandboxError("planner configuration contains duplicate candidate IDs")
        unknown = set(self.ordered_candidate_ids) - known
        if unknown:
            raise PlannerSandboxError(
                f"planner configuration contains unknown candidate IDs: {sorted(unknown)}"
            )


@dataclass(frozen=True, slots=True)
class PlannerEstimate:
    query_id: str
    estimated_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {"query_id": self.query_id, "estimated_rows": self.estimated_rows}


def _validate_configuration(
    configuration: PostgresStatisticsConfiguration,
    repository: NativeStatsRepository,
) -> None:
    configuration.validate(candidate.candidate_id for candidate in repository.candidate_models)


def _contains_parameter(query: str) -> bool:
    if not _PARAMETER_RE.search(query):
        return False
    try:
        from pglast import ast, parse_sql

        class _ParameterVisitor:
            found = False

            def walk(self, node: Any) -> None:
                if isinstance(node, ast.ParamRef):
                    self.found = True
                    return
                if isinstance(node, ast.Node):
                    for field in node:
                        self.walk(getattr(node, field))
                elif isinstance(node, (list, tuple)):
                    for value in node:
                        self.walk(value)

        visitor = _ParameterVisitor()
        visitor.walk(parse_sql(query))
        return visitor.found
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return True


def _target_relation_name(snapshot: AdvisorSnapshot) -> RelationName:
    if len(snapshot.schemas) != 1:
        raise PlannerSandboxError("planner session v1 requires exactly one relation")
    relation = snapshot.schemas[0].relation_name
    if not relation.schema:
        raise PlannerSandboxError("planner session requires a target schema")
    return relation


class PostgresPlannerSession:
    """One read-only PostgreSQL backend with one local registered repository."""

    def __init__(
        self,
        dsn: str,
        snapshot: AdvisorSnapshot,
        candidate_universe: CandidateUniverse,
        native_repository: NativeStatsRepository,
    ) -> None:
        self._dsn = dsn
        self._snapshot = snapshot
        self._universe = candidate_universe
        self._repository = native_repository
        self._connection: Any = None
        self._registered = False
        self._oids: dict[str, int] = {}
        self._active: tuple[str, ...] = ()
        self._target_oid: int | None = None

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def connection(self) -> Any:
        if self._connection is None:
            raise PlannerSandboxError("planner session is not open")
        return self._connection

    @property
    def active_configuration(self) -> PostgresStatisticsConfiguration:
        return PostgresStatisticsConfiguration(self._active)

    @property
    def registered_oids(self) -> dict[str, int]:
        return dict(self._oids)

    def active_backend_oids(self) -> tuple[int, ...]:
        if not self._registered:
            raise PlannerSandboxError("planner repository is not registered")
        active = self.connection.execute(
            "SELECT pg_catalog.pg_hypothetical_extstats_active()"
        ).fetchone()[0]
        return () if active is None else tuple(int(value) for value in active)

    def open(self) -> None:
        if self._connection is not None:
            raise PlannerSandboxError("planner session is already open")
        psycopg = _psycopg()
        connection = None
        try:
            connection = psycopg.connect(
                self._dsn,
                application_name="extstats-advisor-planner",
                autocommit=True,
            )
            capabilities = probe_patched_postgres(connection)
            verification = verify_postgres_planner_sandbox(
                self._dsn,
                self._snapshot,
                self._universe,
                self._repository,
            )
            self._target_oid = int(verification["target_relation_oid"])
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            sql = psycopg.sql
            relation = _target_relation_name(self._snapshot)
            connection.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(relation.schema))
            )
            connection.execute("SELECT pg_catalog.pg_hypothetical_extstats_reset()")
            self._connection = connection
            self._register_repository()
            if capabilities.backend_contract != self._repository.backend_contract:
                raise PlannerSandboxError("planner backend contract differs from repository")
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()
            self._connection = None
            self._registered = False
            self._oids.clear()
            self._active = ()
            self._target_oid = None
            raise

    def _register_repository(self) -> None:
        if self._registered:
            raise PlannerSandboxError("native repository registration was attempted twice")
        if self._target_oid is None:
            raise PlannerSandboxError("planner target OID is unavailable")
        for candidate in self._repository.candidate_models:
            kind = POSTGRES_NATIVE_KIND_CODES[candidate.kind]
            keys = list(candidate.column_ordinals)
            if candidate.state == ABSENT_NATIVE:
                row = self.connection.execute(
                    """
                    SELECT pg_catalog.pg_hypothetical_extstats_register_definition_absent(
                        %s::text, %s::oid, %s::"char", %s::smallint[]
                    )
                    """,
                    (candidate.candidate_id, self._target_oid, kind, keys),
                ).fetchone()
            else:
                payload = self._repository.payloads.get(candidate.candidate_id)
                if payload is None:
                    raise PlannerSandboxError(
                        f"native repository payload is missing: {candidate.candidate_id}"
                    )
                row = self.connection.execute(
                    """
                    SELECT pg_catalog.pg_hypothetical_extstats_register_definition(
                        %s::text, %s::oid, %s::"char", %s::smallint[], %s::bytea
                    )
                    """,
                    (candidate.candidate_id, self._target_oid, kind, keys, payload),
                ).fetchone()
            if row is None or row[0] is None:
                raise PlannerSandboxError(
                    f"patched PostgreSQL did not return a virtual OID: {candidate.candidate_id}"
                )
            self._oids[candidate.candidate_id] = int(row[0])
        physical_count = self.connection.execute(
            "SELECT count(*) FROM pg_catalog.pg_statistic_ext WHERE stxrelid = %s",
            (self._target_oid,),
        ).fetchone()[0]
        if physical_count != 0:
            raise PlannerSandboxError("catalogless registration created physical statistics")
        self._registered = True

    def activate(
        self, configuration: PostgresStatisticsConfiguration
    ) -> PostgresStatisticsConfiguration:
        if not self._registered:
            raise PlannerSandboxError("planner repository is not registered")
        _validate_configuration(configuration, self._repository)
        ordered_oids = [
            self._oids[candidate_id] for candidate_id in configuration.ordered_candidate_ids
        ]
        self.connection.execute(
            "SELECT pg_catalog.pg_hypothetical_extstats_activate(%s::oid[])",
            (ordered_oids,),
        )
        active = self.connection.execute(
            "SELECT pg_catalog.pg_hypothetical_extstats_active()"
        ).fetchone()[0]
        observed_oids = () if active is None else tuple(int(value) for value in active)
        if observed_oids != tuple(ordered_oids):
            raise PlannerSandboxError(
                f"patched PostgreSQL did not preserve activation order: {observed_oids!r}"
            )
        self._active = configuration.ordered_candidate_ids
        return configuration

    def _query(self, query_id: str) -> str:
        query = next(
            (query for query in self._snapshot.workload.queries if query.query_id == query_id),
            None,
        )
        if query is None:
            raise PlannerQueryError(f"unknown workload query ID: {query_id}")
        profile = next(
            (profile for profile in self._universe.query_profiles if profile.query_id == query_id),
            None,
        )
        if profile is None or profile.analysis_status != "supported":
            raise PlannerQueryError(
                f"query {query_id!r} is outside the supported planner workload scope"
            )
        if _contains_parameter(query.sql):
            raise PlannerQueryError(
                "parameterized workload query requires representative bind values; "
                "planner-sandbox-v1 supports self-contained SQL only"
            )
        return query.sql

    def estimate_query(self, query_id: str) -> PlannerEstimate:
        query = self._query(query_id)
        relation = _target_relation_name(self._snapshot)
        sql = _psycopg().sql
        explain = sql.SQL("EXPLAIN (VERBOSE, FORMAT JSON) {} ").format(sql.SQL(query))
        row = self.connection.execute(explain).fetchone()
        if row is None or not row[0]:
            raise PlannerQueryError("PostgreSQL returned an empty EXPLAIN plan")
        document = row[0][0] if isinstance(row[0], list) else row[0]
        nodes: list[dict[str, Any]] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if (
                    value.get("Relation Name") == relation.name
                    and value.get("Schema") == relation.schema
                ):
                    nodes.append(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(document)
        if len(nodes) != 1 or "Plan Rows" not in nodes[0]:
            raise PlannerQueryError(
                "EXPLAIN plan does not contain exactly one managed base-relation node"
            )
        try:
            rows = int(nodes[0]["Plan Rows"])
        except (TypeError, ValueError) as exc:
            raise PlannerQueryError("managed plan node has an invalid Plan Rows value") from exc
        if rows < 0:
            raise PlannerQueryError("managed plan node has a negative Plan Rows value")
        return PlannerEstimate(query_id, rows)

    def estimate_queries(self, query_ids: Sequence[str]) -> tuple[PlannerEstimate, ...]:
        return tuple(self.estimate_query(query_id) for query_id in query_ids)

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.rollback()
            finally:
                self._connection.close()
            self._connection = None
            self._registered = False
            self._oids.clear()
            self._active = ()
            self._target_oid = None


def configuration_from_ids(candidate_ids: Iterable[str]) -> PostgresStatisticsConfiguration:
    return PostgresStatisticsConfiguration(tuple(candidate_ids))
