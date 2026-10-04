# Singleton utility profiling

Singleton profiling is the production artifact stage after candidate generation
and native-statistics materialization. It measures the objective of the empty
configuration and of every candidate whose native repository state is
`present`. The objective is lower-is-better:

```text
J0 = J({})
Δ(c) = J0 - J({c})
```

`Δ` is recorded without clamping or percentage conversion. Every positive-
weight, supported workload query is estimated for every evaluated
configuration. Zero-weight queries do not enter the utility aggregation.

Candidates with repository state `absent-native` are retained in the profile
for auditability. They receive the baseline objective and zero improvement, do
not trigger a planner `EXPLAIN`, and are excluded from the frozen order.

The PostgreSQL implementation uses one `PostgresPlannerSession` for the whole
run: activate the empty configuration, evaluate it, activate each singleton in
candidate-universe order, evaluate it, then close the session. Session cleanup
rolls back the planner transaction. A failed PRESENT evaluation prevents an
artifact from being published.

The utility contract is `weighted-workload-mean-v1`; the current loss contract
is `qerror-cardinality-floor-1-v1`. The artifact is
`singleton-profile-v1` and binds the snapshot, candidate universe, native
repository, ground truth, planner sandbox/backend/server identity, utility and
loss contracts, and the frozen precedence policy
`singleton-utility-precedence-v1`.

The frozen precedence sorts PRESENT candidates by:

1. descending exact improvement;
2. ascending static candidate precedence rank;
3. ascending candidate ID.

No epsilon or rounding is used. If a later admitted set is `M`, its order is
the restriction of the frozen order to `M`:

```text
Order(M) = FrozenPrecedence restricted to M
```

The CLI provides `profiling singleton postgres`, `profiling validate`, and
`profiling inspect`. Profiling has no top-K, budget, worker, search, or
recommendation options; those are later stages.
