# Candidate generation

Candidate derivation is offline after a sealed `AdvisorSnapshot v1` exists:

```text
AdvisorSnapshot
    -> PostgreSQL workload analyzer
    -> portable predicate profiles
    -> core relevant column groups
    -> PostgreSQL capability expansion
    -> CandidateUniverse v1
```

PostgreSQL SQL parsing is intentionally backend-specific. The analyzer uses
`pglast` and records its parser version and the
`postgresql-simple-selection-v1` analysis contract. It emits only portable
query ID, relation ID, column ordinals/names, weight, status, and a concise
unsupported reason. The core group derivation never sees PostgreSQL AST nodes,
OIDs, `attnum`, or native statistic names.

## v1 query scope

The analyzer accepts one simple `SELECT` over the one relation in the
snapshot. The relation may be qualified by the exact structured snapshot
schema/name or be unqualified when its table name matches exactly. Aliases,
quoted identifiers, Unicode identifiers, and PostgreSQL `$1`-style parameters
are handled structurally.

Supported selection predicates are conjunctions of direct single-column
clauses: `=`, `<>`, `<`, `<=`, `>`, `>=` with a constant or parameter on the
other side; `BETWEEN`; `IN`; and `IS NULL`/`IS NOT NULL`. Column-to-column
comparisons, functions or arithmetic around columns, joins, multiple
relations, subqueries, CTEs, set operations, `OR`, and `NOT` are unsupported.
Positive-weight unsupported queries fail candidate derivation with the query
ID and reason. Zero-weight queries remain represented in the profiles but do
not create groups.

For each supported query with predicate column set `A(q)`, core generation
creates every unordered pair in `A(q)`. Pairs are canonicalized by portable
column ordinal and preserve supporting query IDs and total supporting weight.
Schema-wide pairs that never co-occur in a supported query are not generated.

The PostgreSQL capability currently expands every pair into exactly
`postgresql.mcv` and `postgresql.dependencies`. It does not advertise
`ndistinct`, expression statistics, or arity greater than two. The capability
version is `postgresql-extended-statistics-capability-v1`.

The static ordering policy is
`postgresql-static-precedence-v1`: MCV before dependencies, then column count,
column ordinals, column names, and candidate ID. This is only deterministic
artifact/tie-break ordering. It is not ranking, top-K screening, nor the final
planner-visible PostgreSQL statistics order. Those belong to later singleton
profiling and optimizer-budget stages.

`candidate-universe-v1.json` binds its source snapshot semantic digest,
analysis metadata, capability metadata, query profiles, relevant groups,
candidates, and structural query/candidate incidence. It contains no sample
values, utility score, native payload, deployment SQL, or search result.
