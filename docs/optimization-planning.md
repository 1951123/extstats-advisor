# Budgeted optimization planning

`OptimizationPlan v1` is the deterministic boundary between the complete
`SingletonProfile` and a future configuration search. It performs screening,
not search:

```text
all structural candidates
    -> all PRESENT singleton profiling
    -> frozen utility precedence
    -> explicit candidate_limit
    -> screened frozen prefix
    -> future greedy ADD search
```

The immutable budget contract is `optimization-budget-v1`:

- `candidate_limit` is the maximum number of actionable `PRESENT` candidates
  exposed to search;
- `wall_clock_seconds` is the future search-phase hard elapsed-time ceiling,
  defaulting to `300.0` seconds.

This unit stores the wall-clock budget but does not enforce it because no
search is executed. Snapshot acquisition, native-statistics materialization,
sandbox preparation, singleton profiling, and plan construction are separate
artifact stages and are not charged to a later search invocation. The
optimization budget is `B_opt`; it is distinct from any future maintenance or
deployment budget.

The screening policy is `singleton-prefix-screening-v1`. If the singleton
profile frozen actionable order is `F`, screening produces exactly:

```text
S_K = F[:min(candidate_limit, len(F))]
```

No candidate is removed because its singleton improvement is neutral or
negative, and no incidence, utility evaluation, re-sort, or planner call is
performed. `ABSENT_NATIVE` candidates remain diagnostic members of the source
universe and profile, but do not consume `candidate_limit` or enter the plan's
search space.

For any membership selected from the screened space, the plan's pure
`ordered_configuration(membership)` helper returns the subsequence in screened
frozen order:

```text
Order(M) = ScreenedFrozenOrder restricted to M
```

It never sorts by candidate ID, static precedence, or insertion order. Once
created, an `OptimizationPlan` binds the candidate set, order, budget, utility
contract, and loss contract. Changing any of those requires a new plan digest.

The plan exposes deterministic diagnostics, including positive/neutral/
negative singleton counts in the screened prefix and:

```text
worst_case_add_configuration_evaluations_after_singletons
    = K * (K - 1) / 2
```

This is a capacity-planning upper bound only. Strict-improvement stopping or
future wall-clock expiry can reduce actual evaluations; it does not alter
`candidate_limit`.

The future greedy ADD contract is documented here but is not implemented in
this unit. It will reuse singleton objectives for the first round, accept only
strictly improving candidates, preserve the plan's frozen order, and retain
the last fully accepted configuration if an ADD round expires halfway through.
Here, “top-K” means screened search-space width only—not candidate generation,
recommendation, or the final selected configuration.

The production CLI provides `optimization plan`, `optimization validate`, and
`optimization inspect`. None requires a DSN or starts PostgreSQL.
