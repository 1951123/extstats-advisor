# Production architecture

The production advisor consumes a compact analysis snapshot:

```text
AdvisorSnapshot = (Σ, S, P, W)
```

`Σ` is portable logical schema, `S` is one fixed typed representative sample,
`P` is population metadata, and `W` is representative workload. The advisor
does not require a replica of the production database.

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
