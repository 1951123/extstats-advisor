# Production-readiness checklist

This bootstrap is production-oriented architecture, not production-proof. The
following gates remain open:

- [ ] license decision;
- [x] PostgreSQL v1 source extractor for one ordinary base table;
- [x] production-safe bounded `SYSTEM` sampling with a candidate budget;
- [ ] credential handling;
- [ ] artifact encryption-at-rest story;
- [x] sample-only PostgreSQL planner sandbox isolation with backend-local
  catalogless native-statistics activation;
- [ ] failure recovery;
- [x] acquisition timeouts and bounded sampling resource limit;
- [x] offline workload-derived candidate universe v1;
- [x] explicit production exact-cardinality GroundTruthSet, q-error, and
  weighted utility reference contract;
- [ ] multi-instance lifecycle;
- [ ] PostgreSQL version compatibility policy;
- [ ] upgrade and migration policy;
- [ ] observability;
- [ ] large-scale soak testing;
- [ ] security review.

Exact truth is opt-in and is not part of `AdvisorSnapshot v1`. It executes
`SELECT count(*)` wrappers for positive-weight supported workload queries in
the same repeatable-read source transaction as schema, population, and sample
capture. This is read-only but may be expensive on large production tables;
operational alternatives and broader safety policy remain future work.
