# Production utility contract

`AdvisorSnapshot v1` remains the portable `(Σ, S, P, W)` artifact. Exact
cardinality truth is never added to its schema, population, workload, manifest,
or truth section. It is an explicit companion artifact:

```text
same PostgreSQL REPEATABLE READ READ ONLY source view
    +--> AdvisorSnapshot
    +--> GroundTruthSet (optional)

planner configuration -> native estimates
GroundTruthSet + estimates -> CardinalityLoss -> UtilityProvider -> objective
```

## GroundTruthSet v1

`ground-truth-v1.json` uses format `ground-truth-set-v1`. It binds the source
snapshot semantic digest, workload ID, PostgreSQL server identity, and a
`pg_current_snapshot()` source-view token. Each record contains a query ID, a
non-negative integer cardinality, and its source. The artifact is standalone,
canonical JSON, atomically published, and validated independently of the
snapshot. It requires truth for every positive-weight workload query when
validated against a snapshot; zero-weight queries need not be executed.

Production collection is opt-in:

```bash
extstats-advisor snapshot capture postgres \
  --dsn "$EXTSTATS_ADVISOR_POSTGRES_DSN" \
  --relation '"Reporting.Schema"."Order Facts"' \
  --sample-rows 10000 \
  --workload workload.json \
  --output snapshot-dir \
  --ground-truth-output ground-truth-v1.json
```

The schema, population, sample, and exact count wrappers execute in one
repeatable-read read-only transaction. Supported self-contained PostgreSQL
simple-selection queries are counted as:

```sql
SELECT count(*)
FROM (<sealed workload query>) AS extstats_advisor_truth
```

Only `postgresql-simple-selection-v2` queries are eligible: one base relation,
supported `AND` predicates, and row-preserving direct-column/star projection.
`DISTINCT`, grouping, aggregates, windows, `LIMIT`, `OFFSET`, `ORDER BY`,
locking clauses, set-returning expressions, arbitrary projection expressions,
and unresolved parameters (`$1`, `$2`, ...) fail closed before count
execution. This ensures the wrapped output cardinality is the same logical
post-filter row set represented by the planner scan estimate. The statement
timeout applies to these count queries; a failed query publishes no partial
truth artifact. Exact counts may be expensive on large production relations
despite being read-only. Normal capture without `--ground-truth-output`
executes no workload truth queries.

`GroundTruthProvider` is DBMS-neutral. The v1
`ProductionExactCardinalityProvider` is artifact-backed, so future runtime,
sample-derived, or external telemetry providers can replace it without
changing utility evaluation.

## Loss and utility

`CardinalityLoss` accepts numeric planner estimates and integer truth. The v1
`QErrorLoss` contract is `qerror-cardinality-floor-1-v1`:

```text
Q(estimate, truth) = max(
    max(estimate, 1) / max(truth, 1),
    max(truth, 1) / max(estimate, 1)
)
```

Thus `Q(0,0)=1`, `Q(1,0)=1`, `Q(10,0)=10`, and an estimate of `500` for
truth `1000` has loss `2`. Negative or non-finite values are rejected.

`WeightedWorkloadUtility` is independent of PostgreSQL. For every positive-
weight query it looks up truth, consumes one planner estimate, and returns an
immutable diagnostic `UtilityResult` with deterministic per-query records:

```text
J = sum(weight_q * Q_q) / sum(weight_q)
```

Lower is better. The diagnostic CLI evaluates one manually chosen planner
configuration only:

```bash
extstats-advisor utility evaluate postgres \
  snapshot-dir candidate-universe.json native-stats-repository \
  ground-truth-v1.json \
  --dsn "$EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN" \
  --candidate cand_a --candidate cand_b
```

Candidate order is preserved for the PostgreSQL planner. Omitting candidates
evaluates the ordinary-statistics baseline. This unit deliberately does not
implement singleton profiling, ranking, screening, search, optimization
budgets, recommendation, or deployment.
