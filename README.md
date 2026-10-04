# extstats-advisor

`extstats-advisor` is a production-oriented system implementation for advising
on database extended statistics. Its first artifact contract is a sealed,
portable `AdvisorSnapshot v1`:

```text
AdvisorSnapshot = (Σ, S, P, W)
```

where `Σ` is logical schema, `S` is one fixed typed sample realization, `P` is
population metadata, and `W` is representative workload. The advisor does not
require a replica of the production database. Native DBMS statistics and
planning will be added by later backend units.

The historical `/home/wqts/projects/pg-extstats-advisor` repository is the
research prototype. `pg-extstats-benchmarks` owns research datasets, exact
truth, experiment protocols, and paper evaluation. This repository does not
require exact cardinality truth and does not claim to be production-proven or
enterprise-ready.

## Bootstrap commands

```bash
python -m pip install -e '.[dev]'
extstats-advisor --version
extstats-advisor snapshot validate path/to/snapshot
extstats-advisor snapshot inspect path/to/snapshot
pytest
```

See [the architecture](docs/architecture.md) and the precise
[snapshot contract](docs/advisor-snapshot-v1.md).

Licensing is intentionally undecided; a production-readiness TODO records that
decision.
