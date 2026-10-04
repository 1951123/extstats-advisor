# Budgeted greedy ADD search

`Greedy ADD SearchResult v1` consumes a validated `SingletonProfile` and its
`OptimizationPlan` through one planner session and one serial search. It is a
configuration search artifact, not a deployment recommendation.

The initial state is:

```text
M₀ = {}
J₀ = SingletonProfile.baseline.objective
```

The first ADD round reuses the plan's cached singleton objectives. It chooses
the lowest objective in `screened_candidate_ids` order and accepts it only
when the objective is strictly less than `J₀`. Singleton EXPLAIN work is never
repeated by search. If no singleton improves the baseline, the result is a
`local-optimum` with the empty configuration.

Every later round evaluates every remaining candidate against the full
positive-weight supported workload. For a candidate `c`, the planner receives
`OptimizationPlan.ordered_configuration(M | {c})`; insertion order is never
used as PostgreSQL statistics precedence. After a complete round:

```text
c* = argmin(c not in M) J(M | {c})
```

The frozen screened order breaks exact objective ties. The move is accepted
only when `J(M | {c*}) < J(M)`, with no epsilon or rounding. Equal and worse
round minima terminate as `local-optimum`.

The plan's default search wall budget is 300 seconds. The monotonic timer
starts only after the PostgreSQL session has opened, verified its sandbox, and
registered the native repository. It covers cached-round processing,
activation, EXPLAIN, utility evaluation, and bookkeeping. The PostgreSQL
adapter sets a bounded positive transaction-local `statement_timeout` before
each potentially waiting operation and checks the monotonic deadline before
activation, between workload queries, and after query execution.

A live round is committed only after every remaining candidate has returned
successfully **and** the deadline is still valid at the round decision
boundary. If expiry occurs before `evaluate_configuration` is entered for any
candidate in the required live round, the result uses
`budget-expired-before-round` and its partial count is zero. Once any live
candidate evaluation has been entered, including an evaluation that expires
before returning, the result uses `budget-expired-incomplete-round`. Expiry
after the final candidate returns but before the decision-boundary check has
the same incomplete-round meaning. All partial evaluations are
non-semantic diagnostics, the round is not committed, and the final membership
remains the last fully accepted configuration. Planner and utility errors that
are not deadline cancellations fail the search and publish no result.

The cached singleton round is also charged to the search budget, but has no
incomplete-cached state. If the deadline expires before its accepted move is
committed, the result is `budget-expired-before-round` with an empty final
membership and zero live evaluations.

The result records the source and plan digests, planner identity, utility and
loss contracts, cached first-round evaluations, complete later-round
evaluations, accepted moves, and runtime diagnostics. Its semantic digest
excludes elapsed time and other runtime metadata, so non-expired repeated
searches over frozen inputs have stable identity.

The CLI search budget comes only from the supplied `OptimizationPlan`:

```text
extstats-advisor optimization search postgres \
  <snapshot-dir> <candidate-universe.json> <native-stats-repository-dir> \
  <ground-truth-v1.json> <singleton-profile-v1.json> \
  <optimization-plan-v1.json> --dsn "$EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN" \
  --output search-result-v1.json
```

Use `optimization search validate` with all source artifacts to recompute the
cached singleton decision, frozen ordering, complete-round minima, move chain,
and final state. Use `optimization search inspect` for a concise audit summary.
