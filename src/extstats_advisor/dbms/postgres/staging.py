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


def scratch_relation_sql(name: str, sql: Any) -> Any:
    return sql.SQL("{}.{}").format(sql.Identifier("pg_temp"), sql.Identifier(name))


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
        raise NativeStatsMaterializationError(
            f"snapshot collation is unavailable: {collation!r}"
        )
    return str(row[0]), str(row[1])


def create_scratch_relation(
    connection: Any,
    name: str,
    schema: RelationSchema,
    sql: Any,
) -> None:
    columns = []
    for column in schema.columns:
        if column.native_type is None:
            raise UnsupportedPostgresTypeError(
                f"snapshot column lacks native PostgreSQL type: {column.name!r}"
            )
        try:
            native_type = validate_native_type_for_ddl(column.native_type)
        except UnsupportedPostgresTypeError:
            raise
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
    statement = sql.SQL("CREATE TEMPORARY TABLE {} ({}) ON COMMIT DROP").format(
        sql.Identifier(name), sql.SQL(", ").join(columns)
    )
    connection.execute(statement)


def copy_arrow_table(
    connection: Any,
    name: str,
    schema: RelationSchema,
    table: pa.Table,
    sql: Any,
    *,
    batch_size: int,
) -> None:
    columns = sql.SQL(", ").join(sql.Identifier(column.name) for column in schema.columns)
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        scratch_relation_sql(name, sql), columns
    )
    try:
        with connection.cursor().copy(statement) as copy:
            for batch in table.to_batches(max_chunksize=batch_size):
                arrays = [batch.column(index) for index in range(batch.num_columns)]
                for row in zip(*(array.to_pylist() for array in arrays), strict=True):
                    copy.write_row(row)
    except Exception as exc:
        raise NativeStatsMaterializationError(f"could not stage Arrow sample into {name}") from exc
