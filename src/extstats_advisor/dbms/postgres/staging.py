"""Bounded, identifier-safe scratch relation staging for native stats."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from extstats_advisor.dbms.postgres.errors import UnsupportedPostgresTypeError
from extstats_advisor.dbms.postgres.types import (
    map_postgres_type,
    validate_native_type_for_ddl,
)
from extstats_advisor.errors import NativeStatsMaterializationError
from extstats_advisor.snapshot.model import RelationSchema


def relation_identifier(schema_name: str, relation_name: str, sql: Any) -> Any:
    return sql.SQL("{}.{}").format(sql.Identifier(schema_name), sql.Identifier(relation_name))


def scratch_relation_sql(name: str, sql: Any) -> Any:
    return relation_identifier("pg_temp", name, sql)


def _resolve_collation(connection: Any, collation: str) -> tuple[str, str]:
    row = connection.execute(
        """
        SELECT n.nspname, c.collname
          FROM pg_catalog.pg_collation AS c
          JOIN pg_catalog.pg_namespace AS n ON n.oid = c.collnamespace
         WHERE quote_ident(n.nspname) || '.' || quote_ident(c.collname) = %s
        """,
        (collation,),
    ).fetchone()
    if row is None:
        raise NativeStatsMaterializationError(f"snapshot collation is unavailable: {collation!r}")
    return str(row[0]), str(row[1])


def create_relation(
    connection: Any,
    name: str,
    schema: RelationSchema,
    sql: Any,
    *,
    schema_name: str | None = None,
    temporary: bool = False,
) -> None:
    columns = []
    for column in schema.columns:
        if column.native_type is None:
            raise UnsupportedPostgresTypeError(
                f"snapshot column lacks native PostgreSQL type: {column.name!r}"
            )
        native_type = validate_native_type_for_ddl(column.native_type)
        if map_postgres_type(native_type).arrow_type_name != column.arrow_type:
            raise NativeStatsMaterializationError(
                f"snapshot Arrow type does not match PostgreSQL type: {column.name!r}"
            )
        if column.native_collation is None:
            collation = sql.SQL("")
        else:
            coll_schema, coll_name = _resolve_collation(connection, column.native_collation)
            collation = sql.SQL(" COLLATE {}.{}").format(
                sql.Identifier(coll_schema), sql.Identifier(coll_name)
            )
        nullability = sql.SQL("") if column.nullable else sql.SQL(" NOT NULL")
        columns.append(
            sql.SQL("{} {}{}{}").format(
                sql.Identifier(column.name), sql.SQL(native_type), collation, nullability
            )
        )
    if temporary:
        table_name = sql.Identifier(name)
        prefix = sql.SQL("CREATE TEMPORARY TABLE")
        suffix = sql.SQL(" ON COMMIT DROP")
    else:
        if schema_name is None:
            raise ValueError("persistent relation creation requires schema_name")
        table_name = relation_identifier(schema_name, name, sql)
        prefix = sql.SQL("CREATE TABLE")
        suffix = sql.SQL("")
    statement = sql.SQL("{} {} ({}){}").format(
        prefix, table_name, sql.SQL(", ").join(columns), suffix
    )
    connection.execute(statement)


def create_scratch_relation(
    connection: Any,
    name: str,
    schema: RelationSchema,
    sql: Any,
) -> None:
    create_relation(connection, name, schema, sql, temporary=True)


def copy_arrow_table(
    connection: Any,
    name: str,
    schema: RelationSchema,
    table: pa.Table,
    sql: Any,
    *,
    batch_size: int,
) -> None:
    copy_arrow_table_to_relation(
        connection,
        scratch_relation_sql(name, sql),
        schema,
        table,
        sql,
        batch_size=batch_size,
    )


def copy_arrow_table_to_relation(
    connection: Any,
    relation: Any,
    schema: RelationSchema,
    table: pa.Table,
    sql: Any,
    *,
    batch_size: int,
) -> None:
    columns = sql.SQL(", ").join(sql.Identifier(column.name) for column in schema.columns)
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(relation, columns)
    try:
        with connection.cursor().copy(statement) as copy:
            for batch in table.to_batches(max_chunksize=batch_size):
                arrays = [batch.column(index) for index in range(batch.num_columns)]
                for row in zip(*(array.to_pylist() for array in arrays), strict=True):
                    copy.write_row(row)
    except Exception as exc:
        raise NativeStatsMaterializationError("could not stage Arrow sample into relation") from exc
