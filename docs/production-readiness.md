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
- [ ] multi-instance lifecycle;
- [ ] PostgreSQL version compatibility policy;
- [ ] upgrade and migration policy;
- [ ] observability;
- [ ] large-scale soak testing;
- [ ] security review.
