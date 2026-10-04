# extstats-advisor invariants

This repository is the production-oriented system implementation. It is not a
research harness. The core input is a sealed `AdvisorSnapshot v1` containing
`(Σ, S, P, W)`: logical schema, one authoritative typed sample realization,
population metadata, and representative workload.

Mandatory invariants:

1. Benchmark exact truth, q-error oracles, benchmark protocols, RQ drivers,
   random-order experiments, and dataset-specific ETL belong in
   `pg-extstats-benchmarks`, not here. Production exact cardinality truth is a
   separate explicit opt-in artifact contract and must never enter
   `AdvisorSnapshot v1`.
2. Production acquisition is read-only and must not require exact `COUNT(*)`
   or execution of every workload query by default. If exact truth is
   explicitly requested, snapshot and truth queries share the same
   repeatable-read source transaction.
3. The realized sample is authoritative; a random seed is only provenance.
4. Sample row count and production population cardinality are distinct.
5. Ordinary and candidate statistics must eventually derive from the same
   verified sample realization.
6. The advisor core does not reimplement a DBMS cardinality estimator.
7. Workload-derived attribute groups and DBMS-supplied statistic kinds are
   separate concerns. PostgreSQL SQL is parsed in the PostgreSQL adapter into
   portable predicate profiles; core group derivation never sees PostgreSQL
   ASTs or catalog internals.
8. The PostgreSQL workload analysis contract is
   `postgresql-simple-selection-v2`: one row-preserving single-relation
   selection. Direct-column/star projection is required so planner scan-row
   estimates and exact wrapped truth count the same logical row set.
9. DBMS internals such as PostgreSQL OIDs and catalog relations stay inside a
   backend package and never enter the portable snapshot contract.
10. Planner-visible statistics ordering is a backend capability.
11. Optimization effort and deployed maintenance budgets are distinct:
    `B_opt != B_maint`.
12. Artifact validation fails closed, including old workload analysis contracts
    that do not prove v2 row-set semantics.
13. Artifact contracts never use pickle or arbitrary-code serialization.
14. Portable artifact IDs are restricted tokens, but native database relation
    and column names are structured/lossless data and must not be parsed as
    `schema.table` strings.
15. The snapshot root semantic identity must be computed from one explicit
    canonical semantic-manifest object. Runtime metadata and creation time are
    non-semantic; DBMS identity, consistency, semantic provenance, and
    sensitivity are semantic.

Candidate generation and singleton utility profiling are separate stages. Keep
these stages distinct:

- generation: workload-derived relevant groups and backend capability expansion;
- static catalog order: deterministic artifact/tie-break order only;
- singleton profiling: baseline and every PRESENT singleton utility;
- ranking: later use of the frozen singleton precedence;
- screening: later optimizer-budget restriction after ranking.

Static catalog precedence is not the final planner-visible PostgreSQL statistics
order. Singleton profiling freezes a deterministic utility precedence by exact
improvement, static rank, and candidate ID. It does not apply a budget or
choose a deployment. Candidate generation and singleton profiling contain no
top-K restriction, search, recommendation, or deployment.

Current scope includes PostgreSQL snapshot acquisition, an explicit
production-exact GroundTruthSet utility reference, offline candidate
generation, the sample-only planner sandbox, singleton utility profiling with
a frozen precedence artifact, and pure budgeted OptimizationPlan screening.
Greedy search, parallel pools, recommendation, deployment, and maintenance
budgeting remain excluded.
