"""PostgreSQL catalog extraction and native relation identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from extstats_advisor.canonical import digest_json
from extstats_advisor.dbms.postgres.errors import (
    PostgresPermissionError,
    RelationNotFoundError,
    UnsupportedPostgresTypeError,
    UnsupportedRelationError,
)
from extstats_advisor.dbms.postgres.types import PostgresTypeMapping, map_postgres_type
from extstats_advisor.snapshot.model import ColumnSchema, RelationName, RelationSchema


@dataclass(frozen=True, slots=True)
class ResolvedRelation:
    oid: int
    relation_name: RelationName
    relation_id: str
    reltuples: float
    columns: tuple[ColumnSchema, ...]
    mappings: tuple[PostgresTypeMapping, ...]

    @property
    def arrow_schema(self) -> pa.Schema:
        return pa.schema(
            [
                pa.field(column.name, mapping.arrow_type, nullable=column.nullable)
                for column, mapping in zip(self.columns, self.mappings, strict=True)
            ]
        )


def opaque_relation_id(relation_name: RelationName) -> str:
    return f"rel_{digest_json(relation_name.to_dict())[:24]}"


def relation_sql_name(relation_name: RelationName, sql_module: Any) -> Any:
    """Compose a qualified identifier from catalog output, never user text."""

    return sql_module.SQL("{}.{}").format(
        sql_module.Identifier(relation_name.schema), sql_module.Identifier(relation_name.name)
    )


def resolve_relation(
    connection: Any, selector: str
) -> tuple[int, RelationName, float, bool, bool, bool, bool]:
    query = """
        SELECT c.oid::bigint, current_database(), n.nspname, c.relname,
               c.relkind, c.relpersistence, c.relhassubclass,
               c.relrowsecurity, c.relforcerowsecurity, c.reltuples::double precision,
               has_table_privilege(current_user, c.oid, 'SELECT')
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid = c.relnamespace
         WHERE c.oid = to_regclass(%s)::oid
    """
    row = connection.execute(query, (selector,)).fetchone()
    if row is None:
        raise RelationNotFoundError(f"relation selector did not resolve: {selector!r}")
    (
        oid,
        catalog,
        schema,
        name,
        relkind,
        persistence,
        subclass,
        row_security,
        force_row_security,
        reltuples,
        has_select,
    ) = row
    relation_name = RelationName(name=name, schema=schema, catalog=catalog)
    if not has_select:
        raise PostgresPermissionError(f"SELECT privilege is required for relation {name!r}")
    if relkind != "r":
        labels = {
            "v": "view",
            "m": "materialized view",
            "f": "foreign table",
            "p": "partitioned table",
        }
        raise UnsupportedRelationError(
            f"unsupported relation semantics for {relation_name.to_dict()!r}: {labels.get(relkind, relkind)}"
        )
    if persistence == "t":
        raise UnsupportedRelationError(f"temporary relations are unsupported: {name!r}")
    if subclass:
        raise UnsupportedRelationError(
            f"inheritance or partition semantics are unsupported: {name!r}"
        )
    if row_security or force_row_security:
        raise UnsupportedRelationError(f"row-level security is enabled: {name!r}")
    return (
        int(oid),
        relation_name,
        float(reltuples),
        bool(row_security),
        bool(force_row_security),
        bool(has_select),
        persistence == "t",
    )


def extract_schema(
    connection: Any, relation: tuple[int, RelationName, float, bool, bool, bool, bool]
) -> ResolvedRelation:
    oid, relation_name, reltuples, *_ = relation
    rows = connection.execute(
        """
        SELECT a.attname, a.attnum, format_type(a.atttypid, a.atttypmod),
               NOT a.attnotnull,
               CASE WHEN a.attcollation = 0 THEN NULL
                    ELSE quote_ident(cn.nspname) || '.' || quote_ident(co.collname) END,
               t.typtype, t.typelem::bigint
          FROM pg_attribute AS a
          JOIN pg_type AS t ON t.oid = a.atttypid
          LEFT JOIN pg_collation AS co ON co.oid = a.attcollation
          LEFT JOIN pg_namespace AS cn ON cn.oid = co.collnamespace
         WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
        """,
        (oid,),
    ).fetchall()
    if not rows:
        raise UnsupportedRelationError(
            f"relation has no visible user columns: {relation_name.name!r}"
        )
    columns = []
    mappings = []
    for ordinal, row in enumerate(rows, start=1):
        name, attnum, native_type, nullable, collation, typtype, typelem = row
        try:
            mapping = map_postgres_type(native_type, typtype=typtype, typelem=int(typelem))
        except UnsupportedPostgresTypeError as exc:
            raise UnsupportedPostgresTypeError(
                f"unsupported PostgreSQL type for {relation_name.schema}.{relation_name.name}.{name}: {native_type}"
            ) from exc
        if int(attnum) != ordinal:
            raise UnsupportedRelationError(
                f"non-contiguous visible column ordinals: {relation_name.name!r}"
            )
        columns.append(
            ColumnSchema(
                name=name,
                ordinal=ordinal,
                arrow_type=mapping.arrow_type_name,
                nullable=bool(nullable),
                native_type=native_type,
                native_collation=collation,
            )
        )
        mappings.append(mapping)
    return ResolvedRelation(
        int(oid),
        relation_name,
        opaque_relation_id(relation_name),
        reltuples,
        tuple(columns),
        tuple(mappings),
    )


def to_relation_schema(relation: ResolvedRelation) -> RelationSchema:
    return RelationSchema(relation.relation_id, relation.relation_name, relation.columns)
