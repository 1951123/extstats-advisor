# extstats-advisor invariants

This repository is the production-oriented system implementation. It is not a
research harness. The core input is a sealed `AdvisorSnapshot v1` containing
`(Σ, S, P, W)`: logical schema, one authoritative typed sample realization,
population metadata, and representative workload.

Mandatory invariants:

1. Exact cardinality truth, q-error oracles, benchmark protocols, RQ drivers,
   random-order experiments, and dataset-specific ETL belong in
   `pg-extstats-benchmarks`, not here.
2. Production acquisition is read-only and must not require exact `COUNT(*)`
   or execution of every workload query by default.
3. The realized sample is authoritative; a random seed is only provenance.
4. Sample row count and production population cardinality are distinct.
5. Ordinary and candidate statistics must eventually derive from the same
   verified sample realization.
6. The advisor core does not reimplement a DBMS cardinality estimator.
7. Workload-derived attribute groups and DBMS-supplied statistic kinds are
   separate concerns.
8. DBMS internals such as PostgreSQL OIDs and catalog relations stay inside a
   backend package and never enter the portable snapshot contract.
9. Planner-visible statistics ordering is a backend capability.
10. Optimization effort and deployed maintenance budgets are distinct:
    `B_opt != B_maint`.
11. Artifact validation fails closed.
12. Artifact contracts never use pickle or arbitrary-code serialization.
13. Portable artifact IDs are restricted tokens, but native database relation
    and column names are structured/lossless data and must not be parsed as
    `schema.table` strings.
14. The snapshot root semantic identity must be computed from one explicit
    canonical semantic-manifest object. Runtime metadata and creation time are
    non-semantic; DBMS identity, consistency, semantic provenance, and
    sensitivity are semantic.

Bootstrap scope intentionally excludes PostgreSQL extraction, planner
sandboxing, candidate generation, search, parallel pools, deployment, and
maintenance budgeting.
