# PostgreSQL snapshot acquisition

The PostgreSQL backend is an optional dependency and reads one native
`regclass`-compatible relation selector into an `AdvisorSnapshot v1`:

```bash
python -m pip install -e '.[postgres]'
extstats-advisor snapshot capture postgres \
  --dsn "$EXTSTATS_ADVISOR_POSTGRES_DSN" \
  --relation '"Reporting.Schema"."Order Facts"' \
  --sample-rows 10000 \
  --workload workload.json \
  --output snapshot-dir
```

The DSN can be supplied through `EXTSTATS_ADVISOR_POSTGRES_DSN`. It is used
only in memory for connection setup: it is not logged, serialized, or included
in any digest. The workload is external input and is not executed by capture.
An omitted `--sample-seed` generates a seed which is recorded in semantic
sampling provenance. The output contains the structured catalog, schema, and
relation names returned by PostgreSQL, including names with spaces, case, or
Unicode.

## Session and safety contract

Capture uses one PostgreSQL connection and begins an explicit
`REPEATABLE READ READ ONLY` transaction before reading catalog metadata,
`pg_class.reltuples`, or sample rows. It verifies the transaction mode, sets
UTC for timestamp-with-time-zone values, uses `application_name=extstats-advisor`,
and applies bounded local lock and statement timeouts. The connection is
rolled back and closed after acquisition.

Exact cardinality is an explicit opt-in. Add
`--ground-truth-output ground-truth-v1.json` to capture positive-weight
supported workload counts. The snapshot and the standalone GroundTruthSet are
then collected in this same transaction and share a PostgreSQL
`pg_current_snapshot()` source-view token. `--statement-timeout-ms` applies to
truth queries as well as acquisition reads; the default is 60 seconds. Exact
truth may be expensive on large production tables even though it is read-only.
If any required truth query fails, no partial truth artifact is published.

The normal backend path does not create objects, run `ANALYZE`, execute
workload queries for truth, request an exported snapshot, use exact `COUNT(*)`,
or require superuser,
`CREATE`, or `ANALYZE` privileges. It accepts only ordinary stored base tables;
views, materialized views, foreign tables, partitioned/inheritance semantics,
temporary tables, and row-level security are rejected. Sampling uses `ONLY`.

## Types and sampling

The v1 type contract explicitly maps PostgreSQL `boolean`, `smallint`,
`integer`, `bigint`, `real`, `double precision`, `numeric(p,s)`, text,
`varchar`, `char`, `bytea`, `date`, both timestamp variants, and `uuid` to
explicit Arrow types. `uuid` is represented as canonical lossless text;
timestamp-with-time-zone values are canonicalized to UTC. Arrays, ranges,
JSON, enums, domains, unconstrained or too-wide numeric values, and other
unsupported types fail closed with relation, column, and native type context.

Population is the positive finite `pg_class.reltuples` estimate, marked as an
estimate with source `postgresql.pg_class.reltuples`. Unknown (`-1`) or
non-positive estimates fail closed; capture does not silently substitute an
exact count.

Sampling uses `TABLESAMPLE SYSTEM (...) REPEATABLE (seed)` with an adaptive
initial percentage of approximately `100 * (2 * requested_rows / reltuples)`.
Candidate rows stream through a psycopg server-side cursor into a deterministic
client-side reservoir. If the candidate pool is short, the same transaction
snapshot is retried with monotonically larger percentages; earlier reservoirs
are discarded. At 100%, all visible rows are retained when fewer than the
requested number exists. A conservative default candidate limit of 20 times
the requested rows prevents unbounded memory or work; exceeding it aborts the
capture rather than truncating the sample.

The implementation intentionally stops at source acquisition and the explicit
truth handoff. It does not search configurations, recommend or deploy
statistics, or evaluate benchmark/research truth.
