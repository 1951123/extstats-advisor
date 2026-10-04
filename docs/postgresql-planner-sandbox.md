# PostgreSQL native cardinality-estimation planner sandbox

The planner sandbox is a patched-PostgreSQL cardinality-estimation substrate. It
is not a replica of production and it does not claim production physical-plan
or cost fidelity.

The lifecycle is deliberately split:

```text
Production PostgreSQL -> AdvisorSnapshot
Snapshot + CandidateUniverse -> NativeStatsRepository
Snapshot + NativeStatsRepository -> persistent sample-only Planner Sandbox
Planner Session -> backend-local catalogless registration
Planner Session -> ordered activation -> native EXPLAIN estimate
```

The sandbox stores exactly the snapshot sample `S`. The managed target and the
internal frozen-sample relation receive the same typed Arrow rows, including
NULLs, duplicates, column order, collation, and nullability. The patched frozen
replay GUCs make `ANALYZE` publish the production population `P` as
`pg_class.reltuples`; the physical row count remains `|S|`.

Preparation runs one ordinary `ANALYZE` and persists ordinary statistics. It
does not run `CREATE STATISTICS` for candidates. A prepared target therefore
has zero physical `pg_statistic_ext` definitions. Candidate MCV and dependency
payloads are registered only in each planner backend through the patched
catalogless definition API. Registration and active order are backend-local.

The planner configuration is PostgreSQL-specific and ordered:

```text
PostgresStatisticsConfiguration(ordered_candidate_ids=(...))
```

An empty tuple is the ordinary-statistics baseline. Candidate membership is a
set, but planner-visible precedence is the exact tuple order supplied by the
caller. `ABSENT_NATIVE` candidates remain valid registrations and configurations
without inventing payloads.

Only workload queries already marked supported by `CandidateUniverse` may be
planned. Query text is retrieved by `query_id` from the sealed snapshot; ad-hoc
SQL is not accepted. The required analysis contract is
`postgresql-simple-selection-v2`: a row-preserving single-relation selection
with direct-column/star projection and supported `AND` predicates. Snapshot v1
has no representative bind values, so any query containing PostgreSQL
parameter references such as `$1` fails closed. The planner sandbox supports
self-contained SQL only. This scope makes the scan-node `Plan Rows` quantity
comparable to exact cardinality from the wrapped workload SELECT.

Before a planner session opens, sandbox verification checks the contract and
all semantic digests, the structured target identity, exact sample row counts,
population/reltuples scale, ordinary-statistics fingerprint, disabled
autovacuum, modification evidence, and zero physical extended statistics.
Verification is read-only. Preparation and destruction use a transaction-scoped
advisory lock. Destruction removes only objects proven by the internal metadata
contract to belong to this sandbox; it never cascades through a production-like
schema.

This stage virtualizes population cardinality, ordinary statistics, and native
extended statistics only. It does not reproduce production relpages, indexes,
partitioning, storage layout, cost GUCs, hardware, or other physical plan-cost
inputs. It should therefore be described as a **native cardinality-estimation
planner sandbox**, not a full production plan-cost simulator.
