"""Deterministic ordinary PostgreSQL statistics fingerprinting."""

from __future__ import annotations

from typing import Any

from extstats_advisor.canonical import digest_json
from extstats_advisor.errors import NativeStatsMaterializationError


def ordinary_stats_fingerprint(connection: Any, relation_oid: int) -> str:
    row = connection.execute(
        """
        SELECT n.nspname, c.relname
          FROM pg_catalog.pg_class AS c
          JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
         WHERE c.oid = %s
        """,
        (relation_oid,),
    ).fetchone()
    if row is None:
        raise NativeStatsMaterializationError(
            "scratch relation disappeared before stats extraction"
        )
    schema_name, actual_name = str(row[0]), str(row[1])
    rows = connection.execute(
        """
        SELECT a.attnum, a.attname,
               s.null_frac, s.avg_width, s.n_distinct,
               s.most_common_vals::text, s.most_common_freqs::text,
               s.histogram_bounds::text, s.correlation,
               s.most_common_elems::text, s.most_common_elem_freqs::text,
               s.elem_count_histogram::text
          FROM pg_catalog.pg_attribute AS a
          LEFT JOIN pg_catalog.pg_stats AS s
            ON s.schemaname = %s AND s.tablename = %s AND s.attname = a.attname
         WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
        """,
        (schema_name, actual_name, relation_oid),
    ).fetchall()
    if not rows:
        raise NativeStatsMaterializationError("ordinary statistics fingerprint has no columns")
    columns = []
    for row in rows:
        columns.append(
            {
                "ordinal": int(row[0]),
                "name": str(row[1]),
                "null_frac": row[2],
                "avg_width": row[3],
                "n_distinct": row[4],
                "most_common_vals": row[5],
                "most_common_freqs": row[6],
                "histogram_bounds": row[7],
                "correlation": row[8],
                "most_common_elems": row[9],
                "most_common_elem_freqs": row[10],
                "elem_count_histogram": row[11],
            }
        )
    return digest_json(
        {"contract": "postgresql-ordinary-stats-fingerprint-v1", "columns": columns}
    )
