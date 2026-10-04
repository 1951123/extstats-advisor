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
DBMS-specific planner sandbox(s)
    | native ordinary statistics, candidate statistics, what-if planning
    v
Budget-bounded advisor
    v
Recommendation
```

The bootstrap implements only the sealed snapshot contract, typed Arrow sample
serialization, validation, inspection, and the DBMS acquisition boundary. The
first concrete backend is planned to be PostgreSQL. Its later responsibilities
include production extraction, sample replay, native statistics construction,
hypothetical configuration activation, native planner estimates, and backend
configuration capabilities. These are not implemented here.

The research system is separate: `pg-extstats-benchmarks` owns datasets,
truth, experiment protocols, ablations, and paper evaluation. The production
core never requires exact truth.

For v1, the structured consistency declaration says that schema, population,
and samples came from one `consistent-source-view`, while workload is supplied
externally. This is deliberately DBMS-neutral; a later backend may map a
read-only repeatable-read acquisition to it without exposing transaction
syntax.
