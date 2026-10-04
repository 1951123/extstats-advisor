# Production architecture

The production advisor consumes a compact analysis snapshot:

```text
AdvisorSnapshot = (Σ, S, P, W)
```

`Σ` is portable logical schema, `S` is one fixed typed representative sample,
`P` is population metadata, and `W` is representative workload. The advisor
does not require a replica of the production database.

The core distinguishes artifact identity from database naming. A relation has
an opaque restricted `relation_id` for artifact references and a structured
`RelationName(catalog, schema, name)` for the native logical name. Catalog,
schema, table, and column strings are not reparsed or constrained by
PostgreSQL identifier rules.

```text
Production source view
    +--> read-only snapshot acquisition --> AdvisorSnapshot (Σ, S, P, W)
    +--> explicit exact truth acquisition --> GroundTruthSet (optional)
    | verify / deserialize
    v
PostgreSQL workload analyzer
    | portable predicate profiles
    v
Relevant attribute groups (core)
    | PostgreSQL capability expansion
    v
CandidateUniverse v1
    | native sample-only materialization / planner sandbox
    v
Native cardinality-estimation planner sandbox

Planner configuration estimates + GroundTruthSet
    v
CardinalityLoss -> UtilityProvider -> objective

Baseline + every PRESENT singleton
    v
SingletonProfile v1 -> frozen utility precedence

SingletonProfile v1 + OptimizationBudget v1
    v
OptimizationPlan v1 -> screened frozen prefix

CandidateUniverse
 -> NativeStatsRepository
 -> PlannerSandbox
 -> GroundTruth/Utility
 -> SingletonProfile
 -> OptimizationPlan
 -> Greedy ADD SearchResult
 -> Statistics Recommendation
```

Search v1 is serial and consumes one verified planner session. Parallel
workers, DROP/SWAP moves, deployment execution, and maintenance budgets remain
outside the search stage. Recommendation is a separate pure consuming stage:
it derives desired PostgreSQL state and review-only DDL without connecting to
the production database.

The upcoming deployment/preflight boundary is explicitly add-only under
`postgresql-add-only-deployment-v1`. The current Recommendation owns only the
statistics it names; all other production extended-statistics objects remain
external DBA/operator-owned objects. Their presence is not a deployment
blocker, and deployment must not reconcile, replace, rename, alter, drop, or
garbage-collect them. A deterministic name collision with a current
Recommendation is the exception and must fail closed; `IF NOT EXISTS` is not a
conflict policy.

The search and Recommendation stages evaluate `M*`, not necessarily
`E_existing ∪ M*`. Their objective therefore must not be described as a
guarantee for the combined production state when external statistics coexist.
The DBA/operator chooses the reconciliation policy. Physical verification will
preserve only the Recommendation-relative selected-object subsequence
`D = F|_(M*)`; external objects may appear anywhere between those selected
objects in the global OID sequence.

The implementation includes the sealed snapshot contract, typed Arrow sample
serialization, validation, inspection, and a first PostgreSQL source
acquisition backend. That backend is deliberately limited to one read-only
ordinary base table: schema extraction, `pg_class.reltuples` population
metadata, and bounded typed sampling. Sample replay, native statistics
materialization, and the PostgreSQL planner sandbox are separate downstream
stages. The sandbox reconstructs ordinary statistics from the sealed sample and
exposes backend-local catalogless native-statistics registration, ordered
activation, and native cardinality estimates. It does not claim production
physical-plan fidelity. Candidate generation is an offline derived artifact:
the PostgreSQL adapter parses its SQL dialect into portable predicate profiles,
core derives relevant column pairs, and the PostgreSQL capability advertises
native kinds without constructing payloads.

Generation, static precedence, singleton utility profiling, ranking, and
screening are separate stages. Singleton profiling evaluates the baseline and
each PRESENT native candidate in one planner session, records ABSENT_NATIVE
candidates as baseline-equivalent without planner EXPLAIN calls, and freezes
the order by descending exact improvement, static precedence rank, then
candidate ID. For a later admitted set M, the defined order is the restriction
of this frozen order to M; this stage does not perform screening, search, or
deployment. The next boundary is pure budgeted screening: it takes the exact
frozen PRESENT prefix under `candidate_limit`, preserves negative and neutral
singletons, excludes `ABSENT_NATIVE`, and performs no planner or utility calls.
The static PostgreSQL precedence in `CandidateUniverse v1` is deterministic
artifact ordering only, not final planner-visible statistics order.

The production exact-truth reference is deliberately separate from
`AdvisorSnapshot`: it is an opt-in `GroundTruthSet` captured from the same
PostgreSQL MVCC source view, with q-error and weighted workload utility
diagnostics. It is not a benchmark oracle, research label, or requirement for
normal capture. The research system remains separate:
`pg-extstats-benchmarks` owns benchmark datasets, truth, experiment protocols,
ablations, and paper evaluation.

For v1, the structured consistency declaration says that schema, population,
and samples came from one `consistent-source-view`, while workload is supplied
externally. The PostgreSQL backend maps this to one explicit read-only
repeatable-read transaction without exposing transaction syntax in the
portable artifact. When exact truth is explicitly requested, the source-view
token is recorded as backend-specific provenance in both artifacts and all
positive-weight supported workload counts run in that same transaction.
