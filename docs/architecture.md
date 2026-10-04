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
Production DB
    | read-only snapshot acquisition
    v
AdvisorSnapshot (Σ, S, P, W)
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
Native cardinality-estimation planner sandbox and later advisor stages
```

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

Generation, static precedence, ranking, and screening are separate stages.
The static PostgreSQL precedence in `CandidateUniverse v1` is deterministic
artifact ordering only, not final planner-visible statistics order.

The research system is separate: `pg-extstats-benchmarks` owns datasets,
truth, experiment protocols, ablations, and paper evaluation. The production
core never requires exact truth.

For v1, the structured consistency declaration says that schema, population,
and samples came from one `consistent-source-view`, while workload is supplied
externally. The PostgreSQL backend maps this to one explicit read-only
repeatable-read transaction without exposing transaction syntax in the
portable artifact.
