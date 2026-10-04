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

The implementation includes the sealed snapshot contract, typed Arrow sample
serialization, validation, inspection, and a first PostgreSQL source
acquisition backend. That backend is deliberately limited to one read-only
ordinary base table: schema extraction, `pg_class.reltuples` population
metadata, and bounded typed sampling. Sample replay, native statistics
construction, hypothetical configuration activation, native planner estimates,
and backend configuration capabilities are not implemented here.

The research system is separate: `pg-extstats-benchmarks` owns datasets,
truth, experiment protocols, ablations, and paper evaluation. The production
core never requires exact truth.

For v1, the structured consistency declaration says that schema, population,
and samples came from one `consistent-source-view`, while workload is supplied
externally. The PostgreSQL backend maps this to one explicit read-only
repeatable-read transaction without exposing transaction syntax in the
portable artifact.
