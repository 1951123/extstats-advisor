# Research boundary

This repository is not a benchmark harness. The following must not become core
dependencies or required artifact fields:

- benchmark exact cardinality truth or an oracle; production exact truth is
  permitted only through the separate explicit GroundTruthSet contract;
- q-error objectives or benchmark train/valid/test labels;
- RQ1/RQ2/RQ3/RQ4 drivers, random-order experiments, or dataset ETL;
- exact `COUNT(*)` population acquisition by default (opt-in workload truth is
  distinct and must share the snapshot source transaction);
- execution of every workload query to obtain truth by default; the explicit
  production contract only counts positive-weight supported queries in the
  shared source transaction;
- benchmark-specific candidate catalogs, incidence artifacts, or milestone APIs;
- research Bundle compatibility layers and historical experiment drivers.

Research evaluation belongs in `pg-extstats-benchmarks`, which invokes this
repository as a system under test. A production workload may contain sensitive
SQL and sample values; the snapshot sensitivity declaration records that fact,
but this bootstrap does not provide encryption or anonymization.

Artifact IDs are restricted portable tokens. Native database names are not
artifact IDs: relation names are structured `(catalog, schema, name)` values,
and column names preserve their exact non-empty spelling.
