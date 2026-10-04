# extstats-advisor

`extstats-advisor` is a production-oriented system implementation for advising
on database extended statistics. Its first artifact contract is a sealed,
portable `AdvisorSnapshot v1`, with an optional separate production utility
contract:

```text
AdvisorSnapshot = (Σ, S, P, W)
```

where `Σ` is logical schema, `S` is one fixed typed sample realization, `P` is
population metadata, and `W` is representative workload. The advisor does not
require a replica of the production database. The PostgreSQL planner sandbox
and the explicit exact-truth utility reference are documented downstream;
search and recommendation remain future stages.

Portable artifact IDs such as relation IDs, workload IDs, and query IDs are
restricted stable tokens. Database object names are different: relation names
are structured `(catalog, schema, name)` values and column names are losslessly
preserved non-empty strings, including spaces, punctuation, case, and Unicode.

The sealed root identity binds the format version, DBMS identity, component
digests, sample inventory and payload hashes, structured snapshot consistency,
semantic provenance, and sensitivity declaration. Creation time and explicitly
named runtime metadata are non-semantic.

The historical `/home/wqts/projects/pg-extstats-advisor` repository is the
research prototype. `pg-extstats-benchmarks` owns research datasets, benchmark
truth, experiment protocols, and paper evaluation. This repository has an
explicit opt-in production exact-cardinality artifact for the first utility
reference, but it never places truth inside `AdvisorSnapshot v1` and does not
claim to be production-proven or enterprise-ready.

## Bootstrap commands

```bash
python -m pip install -e '.[dev]'
extstats-advisor --version
extstats-advisor snapshot validate path/to/snapshot
extstats-advisor snapshot inspect path/to/snapshot
pytest
```

Install the optional PostgreSQL backend with `python -m pip install -e
'.[postgres]'`. Its read-only capture contract, supported types, bounded
sampling, and credential handling are documented in
[PostgreSQL acquisition](docs/postgresql-acquisition.md).

See [the architecture](docs/architecture.md) and the precise
[snapshot contract](docs/advisor-snapshot-v1.md).

The optional [production utility contract](docs/production-utility.md) explains
same-source-view exact truth, q-error, and weighted workload utility. Normal
snapshot capture does not execute workload queries for truth.

Offline candidate generation is available after snapshot creation. See
[candidate generation](docs/candidate-generation.md) for the deliberately
narrow PostgreSQL SQL scope and structural generation boundary.

Licensing is intentionally undecided; a production-readiness TODO records that
decision.
